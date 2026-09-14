"""测试基础设施：用 fontTools 现场生成最小 TTF（含 cmap / cmap14）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

from fontaudit.main import create_app


def make_font(path, cps, family, uvs=None):
    """生成最小合法 TTF。cps: 码点列表；uvs: {vs_cp: [(base_cp, glyphName|None)]}（cmap14）。"""
    order = [".notdef"] + [f"uni{c:04X}" for c in cps]
    if uvs:
        for pairs in uvs.values():
            for _, gname in pairs:
                if gname and gname not in order:
                    order.append(gname)
    fb = FontBuilder(1000)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({c: f"uni{c:04X}" for c in cps})
    pen = TTGlyphPen(None)
    empty = pen.glyph()
    fb.setupGlyf({name: empty for name in order})
    fb.setupHorizontalMetrics({name: (500, 0) for name in order})
    fb.setupHorizontalHeader()
    fb.setupNameTable({"familyName": family, "styleName": "Regular",
                       "uniqueFontIdentifier": family, "fullName": family,
                       "psName": family.replace(" ", "-")})
    fb.setupOS2()
    fb.setupPost()
    fb.setupMaxp()
    fb.save(path)
    if uvs:
        tt = TTFont(path)
        sub = CmapSubtable.newSubtable(14)
        sub.platformID = 0
        sub.platEncID = 5
        sub.language = 0
        sub.cmap = {}  # cmap 表 compile 时会访问该属性
        sub.uvsDict = uvs
        tt["cmap"].tables.append(sub)
        tt.save(path)
    return path


ASCII_CPS = [0x20] + list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(range(0x61, 0x7B))


@pytest.fixture()
def fonts(tmp_path):
    """四款测试字体：alpha(拉丁+组合符)、beta(纯拉丁)、emoji、vs(带 cmap14)。"""
    paths = {
        "alpha": make_font(tmp_path / "alpha.ttf", ASCII_CPS + [0x0301, 0x00E9], "Alpha"),
        "beta": make_font(tmp_path / "beta.ttf", ASCII_CPS, "Beta"),
        "emoji": make_font(tmp_path / "emoji.ttf",
                           [0x1F468, 0x1F469, 0x1F467, 0x2764, 0x1F642], "Emoji"),
        "vs": make_font(tmp_path / "vs.ttf", [0x41], "VSFont",
                        uvs={0xFE0F: [(0x41, "A.vs")], 0xFE0E: [(0x41, None)]}),
    }
    return paths


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "audit.db"), fonts_dir=str(tmp_path / "fonts_store"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def font_ids(client, fonts):
    """上传全部测试字体，返回 {名字: id}。"""
    ids = {}
    for name, path in fonts.items():
        with open(path, "rb") as fh:
            resp = client.post("/fonts", files={"file": (f"{name}.ttf", fh, "font/ttf")})
        assert resp.status_code == 201, resp.text
        ids[name] = resp.json()["id"]
    return ids


def wait_done(client, task_id, timeout=30):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/tasks/{task_id}").json()
        if task["status"] in ("done", "failed", "interrupted"):
            return task
        time.sleep(0.05)
    raise TimeoutError(f"task {task_id} not finished")


def run_task(client, **payload):
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    task = wait_done(client, resp.json()["id"])
    assert task["status"] == "done", task.get("error")
    return task


def findings(client, task_id, **params):
    return client.get(f"/tasks/{task_id}/findings", params=params).json()
