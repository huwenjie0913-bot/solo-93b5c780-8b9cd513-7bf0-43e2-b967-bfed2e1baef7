"""端到端 API 测试：覆盖缺字、簇拆分、ZWJ 断裂、变体丢失、规范化、
PUA 规则、必用字体、双链对比、分页、导出与重启恢复。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from fontaudit.main import create_app
from tests.conftest import findings, run_task, wait_done


# ---------- 字体管理 ----------

def test_font_upload_list_delete(client, fonts):
    with open(fonts["alpha"], "rb") as fh:
        resp = client.post("/fonts", files={"file": ("alpha.ttf", fh, "font/ttf")})
    assert resp.status_code == 201, resp.text
    font = resp.json()
    assert font["family"] == "Alpha"
    assert font["format"] == "TTF"
    assert font["num_cmap"] > 0

    listed = client.get("/fonts").json()["items"]
    assert [f["id"] for f in listed] == [font["id"]]
    assert client.get(f"/fonts/{font['id']}").json()["family"] == "Alpha"

    assert client.delete(f"/fonts/{font['id']}").json() == {"deleted": font["id"]}
    assert client.get("/fonts").json()["items"] == []


def test_font_upload_rejects_garbage(client):
    resp = client.post("/fonts", files={"file": ("bad.ttf", b"not a font", "font/ttf")})
    assert resp.status_code == 400


def test_cmap14_detected(client, fonts):
    with open(fonts["vs"], "rb") as fh:
        resp = client.post("/fonts", files={"file": ("vs.ttf", fh, "font/ttf")})
    assert resp.json()["has_cmap14"] == 1


# ---------- 基本审计与分段 ----------

def test_segments_record_actual_font(client, font_ids):
    task = run_task(client, name="basic",
                    corpus=[{"text": "Hello🙂x", "lang": "en"}],
                    chain=[font_ids["alpha"], font_ids["emoji"]])
    segs = client.get(f"/tasks/{task['id']}/segments").json()
    assert segs["total"] == 3
    assert [(s["text"], s["font_id"]) for s in segs["items"]] == [
        ("Hello", font_ids["alpha"]),
        ("🙂", font_ids["emoji"]),   # 🙂 不在 alpha，回退到 emoji 字体
        ("x", font_ids["alpha"]),
    ]
    assert findings(client, task["id"])["total"] == 0


def test_missing_glyph(client, font_ids):
    task = run_task(client, corpus=[{"text": "a😀b"}], chain=[font_ids["alpha"]])
    res = findings(client, task["id"], kind="missing_glyph")
    assert res["total"] == 1
    item = res["items"][0]
    assert item["cluster"] == "😀"
    assert json.loads(item["codepoints"]) == ["U+1F600"]
    assert item["start"] == 1 and item["end"] == 2
    assert item["script"] == "Zyyy"


# ---------- 组合附标：缺字 vs 簇拆分 ----------

def test_combining_mark_missing_and_split(client, font_ids):
    text = "Café"  # e + U+0301 组合尖音符
    # 链上只有 beta（无 U+0301）-> 缺字
    t1 = run_task(client, corpus=[{"text": text}], chain=[font_ids["beta"]])
    res = findings(client, t1["id"], kind="missing_glyph")
    assert res["total"] == 1
    assert json.loads(res["items"][0]["detail"])["cp"] == "U+0301"

    # beta 在前、alpha 在后 -> 附标被拆到 alpha，记 cluster_split
    t2 = run_task(client, corpus=[{"text": text}],
                  chain=[font_ids["beta"], font_ids["alpha"]])
    res = findings(client, t2["id"], kind="cluster_split")
    assert res["total"] == 1
    item = res["items"][0]
    assert item["cluster"] == "é"
    assert item["font_id"] == font_ids["beta"]
    assert item["related_font_id"] == font_ids["alpha"]
    assert item["script"] == "Latn"


# ---------- emoji ZWJ 序列 ----------

def test_zwj_broken(client, font_ids):
    # emoji 字体有 👨👩👧 单字但没有 ZWJ 联合字形 -> 序列被拆开
    task = run_task(client, corpus=[{"text": "👨‍👩‍👧 family"}],
                    chain=[font_ids["alpha"], font_ids["emoji"]])
    res = findings(client, task["id"], kind="zwj_broken")
    assert res["total"] == 1
    item = res["items"][0]
    assert item["cluster"] == "👨‍👩‍👧"
    assert item["font_id"] == font_ids["emoji"]
    detail = json.loads(item["detail"])
    assert detail["emoji_parts"] == 3


def test_zwj_part_missing_is_split(client, font_ids):
    # alpha 不含任何 emoji；emoji 字体缺 👨 之外的…此处构造：👨 在 emoji，🙂 也在，
    # 但 👨‍🙂 不是合法序列也无妨——只要部件分属不同字体即拆分。
    # 用 👩‍👩‍👧（emoji 字体全覆盖）对照：不拆；再删一个部件的覆盖场景由
    # missing 测试覆盖。这里验证 👨‍👩‍👧 不产生 cluster_split。
    task = run_task(client, corpus=[{"text": "👨‍👩‍👧"}],
                    chain=[font_ids["alpha"], font_ids["emoji"]])
    assert findings(client, task["id"], kind="cluster_split")["total"] == 0


# ---------- 变体选择符 ----------

def test_variation_selector(client, font_ids):
    text = "A️"  # A + U+FE0F
    # vs 字体有 cmap14 专门字形 -> 无问题
    t1 = run_task(client, corpus=[{"text": text}], chain=[font_ids["vs"]])
    assert findings(client, t1["id"], kind="variation_lost")["total"] == 0

    # alpha 无 cmap14 -> 变体丢失
    t2 = run_task(client, corpus=[{"text": text}], chain=[font_ids["alpha"]])
    res = findings(client, t2["id"], kind="variation_lost")
    assert res["total"] == 1
    detail = json.loads(res["items"][0]["detail"])
    assert detail["base"] == "U+0041"
    assert detail["supported_by"] == []

    # alpha 在前（采用）、vs 在后能补 -> 仍记丢失，但 supported_by 指向 vs
    t3 = run_task(client, corpus=[{"text": text}],
                  chain=[font_ids["alpha"], font_ids["vs"]])
    res = findings(client, t3["id"], kind="variation_lost")
    assert res["total"] == 1
    assert json.loads(res["items"][0]["detail"])["supported_by"] == [font_ids["vs"]]


# ---------- 规范化 / PUA / 必用字体 ----------

def test_normalization_nfkc(client, font_ids):
    # ﬁ (U+FB01 连字) NFKC 后变 "fi"，alpha 可覆盖
    t1 = run_task(client, corpus=[{"text": "ﬁle"}], chain=[font_ids["alpha"]],
                  normalization="NFKC")
    kinds = findings(client, t1["id"])
    assert findings(client, t1["id"], kind="missing_glyph")["total"] == 0
    norm = findings(client, t1["id"], kind="normalization")
    assert norm["total"] == 1
    assert json.loads(norm["items"][0]["detail"])["normalized"] == "file"

    # 不规范化 -> U+FB01 缺字
    t2 = run_task(client, corpus=[{"text": "ﬁle"}], chain=[font_ids["alpha"]])
    res = findings(client, t2["id"], kind="missing_glyph")
    assert res["total"] == 1
    assert json.loads(res["items"][0]["codepoints"]) == ["U+FB01"]


def test_normalization_nfc_changes_coverage(client, font_ids):
    # 分解形式 a+U+0301：NFC 合成 á(U+00E1) 后全链无字体覆盖 -> 缺字；
    # 不规范化时同一文本是簇拆分（a 用 beta，U+0301 落到 alpha）。
    text = "á"
    t1 = run_task(client, corpus=[{"text": text}],
                  chain=[font_ids["beta"], font_ids["alpha"]], normalization="NFC")
    res = findings(client, t1["id"], kind="missing_glyph")
    assert res["total"] == 1
    item = res["items"][0]
    # 坐标与码点指向原文（a+U+0301，长度 2），detail.cp 给出规范化后的缺失码点
    assert (item["start"], item["end"]) == (0, 2)
    assert json.loads(item["codepoints"]) == ["U+0061", "U+0301"]
    assert json.loads(item["detail"])["cp"] == "U+00E1"
    assert findings(client, t1["id"], kind="cluster_split")["total"] == 0

    t2 = run_task(client, corpus=[{"text": text}],
                  chain=[font_ids["beta"], font_ids["alpha"]])
    assert findings(client, t2["id"], kind="cluster_split")["total"] == 1


def test_pua_ignore_and_allowlist(client, font_ids):
    text = "x"  # U+E000 私用区 + 已覆盖的 x
    t1 = run_task(client, corpus=[{"text": text}], chain=[font_ids["alpha"]],
                  ignore_pua=True)
    assert findings(client, t1["id"], kind="missing_glyph")["total"] == 0

    t2 = run_task(client, corpus=[{"text": text}], chain=[font_ids["alpha"]],
                  ignore_pua=False)
    assert findings(client, t2["id"], kind="missing_glyph")["total"] == 1

    # 忽略 PUA 但白名单点名 U+E000 -> 仍报缺字
    t3 = run_task(client, corpus=[{"text": text}], chain=[font_ids["alpha"]],
                  ignore_pua=True, pua_allowlist=["U+E000"])
    assert findings(client, t3["id"], kind="missing_glyph")["total"] == 1


def test_required_font_per_script(client, font_ids):
    task = run_task(client, corpus=[{"text": "Hi"}], chain=[font_ids["alpha"]],
                    required_fonts={"Latn": font_ids["beta"]})
    res = findings(client, task["id"], kind="required_font_mismatch")
    assert res["total"] == 2  # H、i 两个簇
    item = res["items"][0]
    assert item["font_id"] == font_ids["alpha"]
    assert item["related_font_id"] == font_ids["beta"]
    assert item["script"] == "Latn"


# ---------- 双链对比 ----------

def test_chain_comparison(client, font_ids):
    task = run_task(client,
                    corpus=[{"text": "Café"}, {"text": "Hello"}],
                    chain=[font_ids["beta"]],
                    chain_b=[font_ids["beta"], font_ids["alpha"]])
    diffs = client.get(f"/tasks/{task['id']}/diffs").json()
    assert diffs["total"] == 1  # 只有 é 簇有差异
    d = diffs["items"][0]
    assert d["kind"] == "issues_changed"
    assert d["cluster"] == "é"
    assert json.loads(d["codepoints"]) == ["U+0065", "U+0301"]
    assert d["script"] == "Latn"
    assert d["start"] == 3 and d["end"] == 5
    assert d["repro"] == "é"                      # 最小复现片段
    assert "Caf" in d["context"]
    assert json.loads(d["chain_a"])["issues"] == ["missing_glyph"]
    assert json.loads(d["chain_b"])["issues"] == ["cluster_split"]


def test_chain_comparison_font_changed(client, font_ids):
    task = run_task(client, corpus=[{"text": "Hi"}],
                    chain=[font_ids["alpha"]], chain_b=[font_ids["beta"]])
    diffs = client.get(f"/tasks/{task['id']}/diffs", params={"kind": "font_changed"}).json()
    assert diffs["total"] == 2
    a = json.loads(diffs["items"][0]["chain_a"])
    b = json.loads(diffs["items"][0]["chain_b"])
    assert a["font_id"] == font_ids["alpha"] and b["font_id"] == font_ids["beta"]


# ---------- 分页 / 导出 ----------

def test_pagination(client, font_ids):
    task = run_task(client, corpus=[{"text": "😀😁😂🤣😃"}], chain=[font_ids["alpha"]])
    page1 = findings(client, task["id"], kind="missing_glyph", page=1, page_size=2)
    assert page1["total"] == 5 and len(page1["items"]) == 2
    page3 = findings(client, task["id"], kind="missing_glyph", page=3, page_size=2)
    assert len(page3["items"]) == 1
    # 两页不重叠
    ids1 = {i["id"] for i in page1["items"]}
    ids3 = {i["id"] for i in page3["items"]}
    assert not (ids1 & ids3)


def test_export_json_and_csv(client, font_ids):
    task = run_task(client, corpus=[{"text": "Café"}], chain=[font_ids["beta"]])
    resp = client.get(f"/tasks/{task['id']}/export", params={"what": "findings", "format": "json"})
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    data = resp.json()
    assert data[0]["kind"] == "missing_glyph"

    resp = client.get(f"/tasks/{task['id']}/export", params={"what": "findings", "format": "csv"})
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("id,task_id,chain,kind")
    assert "missing_glyph" in lines[1]

    resp = client.get(f"/tasks/{task['id']}/export", params={"what": "segments", "format": "csv"})
    assert "font_id" in resp.text.splitlines()[0]

    assert client.get(f"/tasks/{task['id']}/export",
                      params={"what": "nope"}).status_code == 400


# ---------- 参数校验 ----------

def test_task_validation(client, font_ids):
    assert client.post("/tasks", json={"corpus": [], "chain": [1]}).status_code == 400
    resp = client.post("/tasks", json={"corpus": [{"text": "x"}], "chain": [9999]})
    assert resp.status_code == 400 and "9999" in resp.text
    resp = client.post("/tasks", json={"corpus": [{"text": "x"}],
                                       "chain": [font_ids["alpha"]],
                                       "required_fonts": {"BAD": font_ids["alpha"]}})
    assert resp.status_code == 400
    resp = client.post("/tasks", json={"corpus": [{"text": "x"}],
                                       "chain": [font_ids["alpha"]],
                                       "pua_allowlist": ["not-a-codepoint"]})
    assert resp.status_code == 400


# ---------- 重启恢复 ----------

def test_restart_persistence_and_rerun(tmp_path, fonts):
    db_path = str(tmp_path / "audit.db")
    store = str(tmp_path / "fonts_store")

    app1 = create_app(db_path=db_path, fonts_dir=store)
    with TestClient(app1) as c1:
        with open(fonts["beta"], "rb") as fh:
            fid = c1.post("/fonts", files={"file": ("beta.ttf", fh, "font/ttf")}).json()["id"]
        task = run_task(c1, corpus=[{"text": "Café"}], chain=[fid])
        task_id = task["id"]
        assert findings(c1, task_id)["total"] == 1
        # 模拟崩溃：把任务改回 running
        app1.state.db.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))

    # 重启：新应用实例、同一数据库
    app2 = create_app(db_path=db_path, fonts_dir=store)
    with TestClient(app2) as c2:
        task = c2.get(f"/tasks/{task_id}").json()
        assert task["status"] == "interrupted"      # 中断任务被标记
        assert findings(c2, task_id)["total"] == 1  # 旧结果仍在
        # 重跑：清掉旧结果重新分析
        resp = c2.post(f"/tasks/{task_id}/rerun")
        assert resp.status_code == 202
        task = wait_done(c2, task_id)
        assert task["status"] == "done"
        assert findings(c2, task_id, kind="missing_glyph")["total"] == 1
        # 任务列表可追溯
        assert c2.get("/tasks").json()["total"] == 1
