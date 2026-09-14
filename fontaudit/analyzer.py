"""核心分析：字素簇 × 回退链 × HarfBuzz 塑形。

对每个字素簇：
1. 按回退顺序选出实际采用字体（首个覆盖簇基字的字体）；
2. 校验簇内其余码点在该字体中的覆盖——被后续字体接住记 cluster_split，
   全链无字体覆盖记 missing_glyph；
3. 用 HarfBuzz 实际塑形，检查 .notdef、ZWJ 序列是否断裂；
4. 变体选择符查 cmap format 14，无专门变体字形记 variation_lost；
5. 若任务指定了某脚本的必用字体，核对实际采用字体。

两条回退链对比时，按簇对齐，输出采用字体与问题集合的差异及最小复现片段。
"""
from __future__ import annotations

import json
import traceback
import unicodedata
from bisect import bisect_right
from typing import Any

from .clusters import segment_clusters
from .db import Database, utcnow
from .emoji_data import is_extended_pictographic
from .fonts import FACE_CACHE, FontFace
from .scripts import script_of

# 问题类型
MISSING_GLYPH = "missing_glyph"            # 全链无字体覆盖该码点
NOTDEF = "notdef"                          # cmap 声称覆盖但塑形产出 .notdef
CLUSTER_SPLIT = "cluster_split"            # 簇内码点被拆到后续字体（组合符/emoji 部件）
ZWJ_BROKEN = "zwj_broken"                  # ZWJ 序列未被塑形为联合字形
VARIATION_LOST = "variation_lost"          # 变体选择符无专门字形，被静默丢弃
REQUIRED_MISMATCH = "required_font_mismatch"  # 指定脚本的必用字体未被采用
NORMALIZATION = "normalization"            # 规范化改变了文本（信息项）

PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))
_PUA_STARTS = [a for a, _ in PUA_RANGES]

# Default_Ignorable：塑形时允许不可见，不参与缺字判定
_DI_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0),
    (0xE0001, 0xE0001), (0xE0020, 0xE007F), (0xE0100, 0xE01EF),
    (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
)
_DI_STARTS = [a for a, _ in _DI_RANGES]


def is_pua(cp: int) -> bool:
    i = bisect_right(_PUA_STARTS, cp) - 1
    return i >= 0 and PUA_RANGES[i][0] <= cp <= PUA_RANGES[i][1]


def is_default_ignorable(cp: int) -> bool:
    i = bisect_right(_DI_STARTS, cp) - 1
    return i >= 0 and _DI_RANGES[i][0] <= cp <= _DI_RANGES[i][1]


def is_vs(cp: int) -> bool:
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF


def is_mark(cp: int) -> bool:
    return unicodedata.category(chr(cp)) in ("Mn", "Mc", "Me")


def fmt_cp(cp: int) -> str:
    return f"U+{cp:04X}"


class AuditConfig:
    def __init__(self, params: dict[str, Any]):
        self.normalization: str = params.get("normalization", "none")
        self.ignore_pua: bool = bool(params.get("ignore_pua", False))
        self.pua_allowlist: set[int] = set(params.get("pua_allowlist", []))
        self.required_fonts: dict[str, int] = params.get("required_fonts", {})


def _issue(kind: str, cp: int | None = None, font_id: int | None = None,
           related_font_id: int | None = None, **detail: Any) -> dict:
    return {"kind": kind, "cp": cp, "font_id": font_id,
            "related_font_id": related_font_id, "detail": detail}


def analyze_cluster(cluster: str, faces: list[FontFace], font_ids: list[int],
                    cfg: AuditConfig, lang: str | None) -> tuple[int | None, list[dict], str]:
    """分析单个字素簇，返回 (采用字体下标, 问题列表, 脚本)。"""
    cps = [ord(c) for c in cluster]
    # 需要字体覆盖的码点：排除 VS/ZWJ/默认可忽略；按配置豁免 PUA
    checkable: list[int] = []
    for cp in cps:
        if is_vs(cp) or cp == 0x200D or is_default_ignorable(cp):
            continue
        if cfg.ignore_pua and is_pua(cp) and cp not in cfg.pua_allowlist:
            continue
        checkable.append(cp)
    # 簇基字（非组合符）决定字体选择；纯组合符簇退化为全部可检码点
    primary = [cp for cp in checkable if not is_mark(cp)] or checkable
    script = script_of(primary[0]) if primary else "Zyyy"

    issues: list[dict] = []
    chosen: int | None = None
    if primary:
        for i, face in enumerate(faces):
            if face.covers(primary[0]):
                chosen = i
                break

    if primary and chosen is None:
        issues.append(_issue(MISSING_GLYPH, primary[0],
                             reason="no font in chain covers the cluster base"))
        for cp in checkable[1:]:
            if not any(f.covers(cp) for f in faces):
                issues.append(_issue(MISSING_GLYPH, cp,
                                     reason="no font in chain covers this codepoint"))
    elif chosen is not None:
        face = faces[chosen]
        fid = font_ids[chosen]
        # 1) 簇内覆盖：缺失码点由后续字体接住 -> 簇拆分；全链都没有 -> 缺字
        for cp in checkable:
            if face.covers(cp):
                continue
            later = next((j for j in range(chosen + 1, len(faces)) if faces[j].covers(cp)), None)
            if later is None:
                issues.append(_issue(MISSING_GLYPH, cp, font_id=fid,
                                     reason="chosen font lacks codepoint, no fallback covers it"))
            else:
                issues.append(_issue(CLUSTER_SPLIT, cp, font_id=fid,
                                     related_font_id=font_ids[later],
                                     reason="codepoint falls back to a later font, cluster is split"))
        # 2) HarfBuzz 塑形：.notdef 与 ZWJ 断裂
        glyphs = face.shape(cluster, lang)
        # 组合符并入基字簇后，gid 0 的簇值指向基字，无法逐码点回溯源字符；
        # 改为计数比较：未被覆盖的码点本就会产出 gid 0，超出部分才是真 .notdef
        n_gid0 = sum(1 for gid, _ in glyphs if gid == 0)
        n_uncovered = sum(1 for cp in checkable if not face.covers(cp))
        if n_gid0 > n_uncovered:
            issues.append(_issue(NOTDEF, primary[0] if primary else None, font_id=fid,
                                 reason="shaping produced .notdef despite cmap coverage",
                                 notdef_count=n_gid0))
        if 0x200D in cps:
            # HarfBuzz 会把 ZWJ 序列合并为单一簇，无法靠簇值判断断裂；
            # 若字体有 ZWJ 联合字形（GSUB 连字），输出应为 1 个字形，
            # 否则序列被渲染为多个独立字形 -> zwj_broken。
            parts = [cp for cp in cps if is_extended_pictographic(cp)]
            if len(parts) >= 2 and all(face.covers(cp) for cp in parts) and len(glyphs) > 1:
                issues.append(_issue(ZWJ_BROKEN, None, font_id=fid,
                                     reason="ZWJ sequence shaped as separate glyphs",
                                     emoji_parts=len(parts), out_glyphs=len(glyphs)))
        # 3) 变体选择符：查 cmap format 14
        for pos, cp in enumerate(cps):
            if not is_vs(cp):
                continue
            base = next((cps[k] for k in range(pos - 1, -1, -1) if not is_vs(cps[k])), None)
            if base is None:
                continue
            if face.uvs_mapping(base, cp) != "specific":
                supporters = [font_ids[j] for j, f in enumerate(faces)
                              if f.uvs_mapping(base, cp) == "specific"]
                issues.append(_issue(VARIATION_LOST, cp, font_id=fid,
                                     related_font_id=supporters[0] if supporters else None,
                                     reason="variation selector has no specific glyph in chosen font",
                                     base=fmt_cp(base), supported_by=supporters))

    # 4) 指定脚本的必用字体
    required = cfg.required_fonts.get(script)
    if required is not None:
        actual = font_ids[chosen] if chosen is not None else None
        if actual != required:
            issues.append(_issue(REQUIRED_MISMATCH, primary[0] if primary else None,
                                 font_id=actual, related_font_id=required,
                                 reason="required font for script not used", script=script))

    # 去重（塑形与覆盖检查可能重复报告同一码点）
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for iss in issues:
        key = (iss["kind"], iss["cp"], iss["font_id"], iss["related_font_id"])
        if key not in seen:
            seen.add(key)
            uniq.append(iss)
    return chosen, uniq, script


def analyze_text(item: dict, faces: list[FontFace], font_ids: list[int],
                 cfg: AuditConfig) -> dict:
    """分析一条语料，返回簇级结果与合并后的字体分段。"""
    original = item["text"]
    lang = item.get("lang")
    text = original
    if cfg.normalization != "none":
        text = unicodedata.normalize(cfg.normalization, original)
    clusters = []
    for start, end, cl in segment_clusters(text):
        chosen, issues, script = analyze_cluster(cl, faces, font_ids, cfg, lang)
        clusters.append({
            "start": start, "end": end, "text": cl, "script": script,
            "font_id": font_ids[chosen] if chosen is not None else None,
            "issues": issues,
        })
    # 连续同字体的簇合并为分段
    segments: list[dict] = []
    for c in clusters:
        if segments and segments[-1]["font_id"] == c["font_id"] and segments[-1]["end"] == c["start"]:
            segments[-1]["end"] = c["end"]
            segments[-1]["text"] += c["text"]
        else:
            segments.append({"start": c["start"], "end": c["end"],
                             "text": c["text"], "font_id": c["font_id"]})
    return {"normalized": text, "norm_changed": text != original,
            "clusters": clusters, "segments": segments}


def _codepoints_json(text: str) -> str:
    return json.dumps([fmt_cp(ord(c)) for c in text], ensure_ascii=False)


def _compute_diffs(task_id: int, text_index: int, res_a: dict, res_b: dict) -> list[tuple]:
    """按簇对齐两条链的结果，输出差异行（含码点、脚本、位置、最小复现片段）。"""
    rows = []
    normalized = res_a["normalized"]
    for a, b in zip(res_a["clusters"], res_b["clusters"]):
        kinds_a = sorted(i["kind"] for i in a["issues"])
        kinds_b = sorted(i["kind"] for i in b["issues"])
        if a["font_id"] == b["font_id"] and kinds_a == kinds_b:
            continue
        if a["font_id"] != b["font_id"] and kinds_a != kinds_b:
            kind = "font_and_issues_changed"
        elif a["font_id"] != b["font_id"]:
            kind = "font_changed"
        else:
            kind = "issues_changed"
        context = normalized[max(0, a["start"] - 8):a["end"] + 8]
        rows.append((
            task_id, text_index, a["start"], a["end"], a["text"],
            _codepoints_json(a["text"]), a["script"], kind,
            json.dumps({"font_id": a["font_id"], "issues": kinds_a}, ensure_ascii=False),
            json.dumps({"font_id": b["font_id"], "issues": kinds_b}, ensure_ascii=False),
            a["text"],  # 最小复现片段：触发差异的最小字素簇
            context,
        ))
    return rows


_INSERT_FINDING = ('INSERT INTO findings(task_id, chain, kind, text_index, lang, start, "end",'
                   ' cluster, codepoints, script, font_id, related_font_id, detail)'
                   ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)')
_INSERT_SEGMENT = ('INSERT INTO segments(task_id, chain, text_index, start, "end", text, font_id)'
                   ' VALUES (?,?,?,?,?,?,?)')
_INSERT_DIFF = ('INSERT INTO diffs(task_id, text_index, start, "end", cluster, codepoints, script,'
                ' kind, chain_a, chain_b, repro, context) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)')


def _load_chain(db: Database, font_ids: list[int]) -> tuple[list[FontFace], list[int]]:
    faces = []
    for fid in font_ids:
        row = db.query_one("SELECT id, path FROM fonts WHERE id=?", (fid,))
        if not row:
            raise ValueError(f"字体 id={fid} 不存在（可能已被删除）")
        faces.append(FACE_CACHE.get(row["id"], row["path"]))
    return faces, font_ids


def run_task(db: Database, task_id: int) -> None:
    """执行审查任务（后台线程调用）；结果批量写库。"""
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row or row["status"] != "pending":
        return
    db.execute("UPDATE tasks SET status='running', started_at=? WHERE id=?", (utcnow(), task_id))
    try:
        params = json.loads(row["params"])
        cfg = AuditConfig(params)
        chains: dict[str, tuple[list[FontFace], list[int]]] = {"A": _load_chain(db, params["chain"])}
        if params.get("chain_b"):
            chains["B"] = _load_chain(db, params["chain_b"])

        finding_rows: list[tuple] = []
        segment_rows: list[tuple] = []
        diff_rows: list[tuple] = []

        for ti, item in enumerate(params["corpus"]):
            lang = item.get("lang")
            per_chain: dict[str, dict] = {}
            for chain_key, (faces, font_ids) in chains.items():
                res = analyze_text(item, faces, font_ids, cfg)
                per_chain[chain_key] = res
                if res["norm_changed"]:
                    finding_rows.append((
                        task_id, chain_key, NORMALIZATION, ti, lang, 0, len(item["text"]), "",
                        _codepoints_json(item["text"]), "Zyyy", None, None,
                        json.dumps({"original": item["text"], "normalized": res["normalized"]},
                                   ensure_ascii=False)))
                for cl in res["clusters"]:
                    for iss in cl["issues"]:
                        finding_rows.append((
                            task_id, chain_key, iss["kind"], ti, lang, cl["start"], cl["end"],
                            cl["text"], _codepoints_json(cl["text"]), cl["script"],
                            iss["font_id"] if iss["font_id"] is not None else cl["font_id"],
                            iss["related_font_id"],
                            json.dumps({"cp": fmt_cp(iss["cp"]) if iss["cp"] is not None else None,
                                        **iss["detail"]}, ensure_ascii=False)))
                for seg in res["segments"]:
                    segment_rows.append((task_id, chain_key, ti, seg["start"], seg["end"],
                                         seg["text"], seg["font_id"]))
            if "B" in chains:
                diff_rows.extend(_compute_diffs(task_id, ti, per_chain["A"], per_chain["B"]))

        db.executemany(_INSERT_FINDING, finding_rows)
        db.executemany(_INSERT_SEGMENT, segment_rows)
        db.executemany(_INSERT_DIFF, diff_rows)
        db.execute("UPDATE tasks SET status='done', finished_at=?, error=NULL WHERE id=?",
                   (utcnow(), task_id))
    except Exception:
        db.execute("UPDATE tasks SET status='failed', error=?, finished_at=? WHERE id=?",
                   (traceback.format_exc(limit=10), utcnow(), task_id))
