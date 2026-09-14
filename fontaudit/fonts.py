"""字体封装：fontTools 读取 cmap / cmap14，uharfbuzz 负责塑形。"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import IO, Union

import uharfbuzz as hb
from fontTools.ttLib import TTFont

PathLike = Union[str, Path]


def inspect_font(file: Union[PathLike, IO[bytes], bytes]) -> dict:
    """解析字体元数据；非法字体抛异常（供上传校验）。"""
    tt = TTFont(file)
    names: dict[str, str] = {}
    if "name" in tt:
        for nid, key in ((16, "family"), (1, "family"), (17, "subfamily"), (2, "subfamily"), (4, "full_name")):
            if key not in names:
                val = tt["name"].getDebugName(nid)
                if val:
                    names[key] = val
    best = tt.getBestCmap() or {}
    has14 = "cmap" in tt and any(t.format == 14 for t in tt["cmap"].tables)
    fmt = {"\x00\x01\x00\x00": "TTF", "OTTO": "OTF/CFF", "true": "TTF", "ttcf": "TTC"}.get(
        tt.sfntVersion, str(tt.sfntVersion)
    )
    return {
        "family": names.get("family", ""),
        "subfamily": names.get("subfamily", ""),
        "full_name": names.get("full_name", ""),
        "num_glyphs": tt["maxp"].numGlyphs if "maxp" in tt else 0,
        "num_cmap": len(best),
        "has_cmap14": has14,
        "format": fmt,
    }


class FontFace:
    """一个字体的审计视图：cmap 覆盖、cmap14 变体映射、HarfBuzz 塑形。"""

    def __init__(self, font_id: int, path: PathLike):
        self.id = font_id
        self.path = str(path)
        tt = TTFont(self.path)
        self.cmap: dict[int, str] = tt.getBestCmap() or {}
        # (base_cp, vs_cp) -> glyphName；glyphName 为 None 表示 default UVS（等同无变体）
        self.uvs: dict[tuple[int, int], str | None] = {}
        if "cmap" in tt:
            for table in tt["cmap"].tables:
                if table.format == 14 and getattr(table, "uvsDict", None):
                    for vs, pairs in table.uvsDict.items():
                        for base, gname in pairs:
                            self.uvs[(base, vs)] = gname
        blob = hb.Blob(Path(self.path).read_bytes())
        self._hb_font = hb.Font(hb.Face(blob))

    def covers(self, cp: int) -> bool:
        return cp in self.cmap

    def uvs_mapping(self, base: int, vs: int) -> str | None:
        """'specific' = 有专门变体字形；'default' = 仅默认 UVS；None = 不支持。"""
        if (base, vs) not in self.uvs:
            return None
        return "default" if self.uvs[(base, vs)] is None else "specific"

    def shape(self, text: str, lang: str | None = None) -> list[tuple[int, int]]:
        """塑形文本，返回 [(glyph_id, cluster_char_index), ...]。"""
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        if lang:
            try:
                buf.language = lang
            except Exception:
                pass  # 非法语言标签不影响审计
        hb.shape(self._hb_font, buf)
        return [(g.codepoint, g.cluster) for g in buf.glyph_infos]


class FaceCache:
    """按 (font_id, path) 缓存 FontFace，避免重复解析。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[int, str], FontFace] = {}

    def get(self, font_id: int, path: str) -> FontFace:
        key = (font_id, path)
        with self._lock:
            face = self._cache.get(key)
            if face is None:
                face = FontFace(font_id, path)
                self._cache[key] = face
            return face

    def evict(self, font_id: int) -> None:
        with self._lock:
            for key in [k for k in self._cache if k[0] == font_id]:
                del self._cache[key]


FACE_CACHE = FaceCache()
