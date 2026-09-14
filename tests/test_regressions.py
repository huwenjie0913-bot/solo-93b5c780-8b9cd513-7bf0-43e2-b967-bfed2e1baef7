"""四个已复核缺陷的回归测试：

1. SCRIPT_RANGES 无序导致 Hebr/Arab/Deva/Thai/Hang/全角拉丁 脚本识别错误，
   required_fonts 无法按脚本生效；
2. 未实现 GB9c，क्ष（क + ् + ष）被切成两个字素簇；
3. 规范化后坐标指向规范化文本而非原文（ﬁ --NFKC--> fi 错位）；
4. cmap14 default UVS 被误报为 variation_lost（A + U+FE0E）。
"""
from __future__ import annotations

import json

from fontaudit.clusters import segment_clusters
from fontaudit.scripts import script_of
from tests.conftest import ASCII_CPS, findings, make_font, run_task


# ---------- 1. 脚本识别与 required_fonts ----------

class TestScriptRanges:
    def test_script_of_common_scripts(self):
        assert script_of(0x05D0) == "Hebr"   # א
        assert script_of(0x0627) == "Arab"   # ا
        assert script_of(0x0644) == "Arab"   # ل
        assert script_of(0x0915) == "Deva"   # क
        assert script_of(0x0E01) == "Thai"   # ก
        assert script_of(0x0E2A) == "Thai"   # ส
        assert script_of(0xAC00) == "Hang"   # 가
        assert script_of(0xD55C) == "Hang"   # 한
        assert script_of(0x1102) == "Hang"   # ᄂ 字母
        assert script_of(0xFF21) == "Latn"   # Ａ 全角
        assert script_of(0xFF41) == "Latn"   # ａ 全角
        assert script_of(0x0041) == "Latn"
        assert script_of(0x4E2D) == "Hani"

    def test_required_fonts_hebrew(self, client, font_ids):
        task = run_task(client, corpus=[{"text": "שלום", "lang": "he"}],
                        chain=[font_ids["alpha"]],
                        required_fonts={"Hebr": font_ids["beta"]})
        res = findings(client, task["id"], kind="required_font_mismatch")
        assert res["total"] == 4  # ש ל ו ם 四个簇
        assert {i["script"] for i in res["items"]} == {"Hebr"}
        assert res["items"][0]["related_font_id"] == font_ids["beta"]

    def test_required_fonts_fullwidth_latin(self, client, font_ids):
        task = run_task(client, corpus=[{"text": "ＡＢ"}],
                        chain=[font_ids["alpha"]],
                        required_fonts={"Latn": font_ids["beta"]})
        res = findings(client, task["id"], kind="required_font_mismatch")
        assert res["total"] == 2
        assert {i["script"] for i in res["items"]} == {"Latn"}


# ---------- 2. GB9c 印地语辅音连字 ----------

class TestGB9c:
    def test_devanagari_conjoint_single_cluster(self):
        assert [c for _, _, c in segment_clusters("क्ष")] == ["क्ष"]

    def test_conjoint_with_zwj(self):
        assert [c for _, _, c in segment_clusters("क्‍ष")] == ["क्‍ष"]

    def test_balinese_conjoint_single_cluster(self):
        # U+1B13 KA + U+1B44 ADEG ADEG(virama) + U+1B13 KA
        assert [c for _, _, c in segment_clusters("ᬓ᭄ᬓ")] == ["ᬓ᭄ᬓ"]

    def test_javanese_conjoint_single_cluster(self):
        # U+A98F KA + U+A9C0 PANGKON(virama) + U+A98F KA
        assert [c for _, _, c in segment_clusters("ꦏ꧀ꦏ")] == ["ꦏ꧀ꦏ"]

    def test_plain_consonants_still_split(self):
        assert [c for _, _, c in segment_clusters("कक")] == ["क", "क"]

    def test_unicode17_tulu_tigalari_conjoint(self):
        # U+11390(InCB=Consonant) + U+113D0(InCB=Linker) + U+11390：
        # Unicode 17 新增字符，运行时 unicodedata 视为未分配(Cn)，
        # 字素分类须以内置的同版本 GCB/InCB 数据为准，序列仍成一个簇
        s = "\U00011390\U000113D0\U00011390"
        assert [c for _, _, c in segment_clusters(s)] == [s]

    def test_api_reports_whole_unicode17_conjoint(self, client, font_ids):
        s = "\U00011390\U000113D0\U00011390"
        task = run_task(client, corpus=[{"text": s}], chain=[font_ids["alpha"]])
        res = findings(client, task["id"], kind="missing_glyph")
        assert res["total"] >= 1
        assert {i["cluster"] for i in res["items"]} == {s}
        segs = client.get(f"/tasks/{task['id']}/segments").json()
        assert [(x["start"], x["end"]) for x in segs["items"]] == [(0, 3)]

    def test_api_reports_whole_conjoint(self, client, font_ids):
        # alpha 无天城文覆盖 -> 缺字报告的 cluster 应是完整的 क्ष 而非碎片
        task = run_task(client, corpus=[{"text": "क्ष"}], chain=[font_ids["alpha"]])
        res = findings(client, task["id"], kind="missing_glyph")
        assert res["total"] >= 1
        assert {i["cluster"] for i in res["items"]} == {"क्ष"}
        segs = client.get(f"/tasks/{task['id']}/segments").json()
        assert [(s["start"], s["end"]) for s in segs["items"]] == [(0, 3)]

    def test_api_reports_whole_balinese_conjoint(self, client, font_ids):
        task = run_task(client, corpus=[{"text": "ᬓ᭄ᬓ"}], chain=[font_ids["alpha"]])
        res = findings(client, task["id"], kind="missing_glyph")
        assert res["total"] >= 1
        assert {i["cluster"] for i in res["items"]} == {"ᬓ᭄ᬓ"}
        segs = client.get(f"/tasks/{task['id']}/segments").json()
        assert [(s["start"], s["end"]) for s in segs["items"]] == [(0, 3)]

    def test_api_reports_whole_javanese_conjoint(self, client, font_ids):
        task = run_task(client, corpus=[{"text": "ꦏ꧀ꦏ"}], chain=[font_ids["alpha"]])
        res = findings(client, task["id"], kind="missing_glyph")
        assert res["total"] >= 1
        assert {i["cluster"] for i in res["items"]} == {"ꦏ꧀ꦏ"}
        segs = client.get(f"/tasks/{task['id']}/segments").json()
        assert [(s["start"], s["end"]) for s in segs["items"]] == [(0, 3)]


# ---------- 3. 规范化坐标映射回原文 ----------

class TestNormalizationMapping:
    def test_nfkc_expansion_coords_point_to_original(self, client, font_ids, tmp_path):
        # 缺 'f' 的字体：NFKC 把 ﬁ(U+FB01, 1 字符) 展开为 fi(2 字符) 后，
        # f 簇缺字的坐标必须指向原文的 ﬁ（0..1），而非规范化文本的 0..1 之外
        nofi = make_font(tmp_path / "nofi.ttf",
                         [c for c in ASCII_CPS if c != 0x66], "NoFI")
        with open(nofi, "rb") as fh:
            nofi_id = client.post("/fonts", files={"file": ("nofi.ttf", fh, "font/ttf")}).json()["id"]
        task = run_task(client, corpus=[{"text": "ﬁle"}],
                        chain=[nofi_id, font_ids["alpha"]], chain_b=[nofi_id],
                        normalization="NFKC")
        # 链 B：f 全链无覆盖 -> 缺字，坐标/簇/码点均指向原文 ﬁ
        res = findings(client, task["id"], kind="missing_glyph", chain="B")
        assert res["total"] == 1
        item = res["items"][0]
        assert (item["start"], item["end"]) == (0, 1)
        assert item["cluster"] == "ﬁ"
        assert json.loads(item["codepoints"]) == ["U+FB01"]
        assert json.loads(item["detail"])["cp"] == "U+0066"  # 规范化后缺失的是 f
        # 差异坐标同样指向原文，最小复现片段为 ﬁ
        diffs = client.get(f"/tasks/{task['id']}/diffs").json()
        assert diffs["total"] == 1
        d = diffs["items"][0]
        assert (d["start"], d["end"]) == (0, 1)
        assert d["cluster"] == "ﬁ" and d["repro"] == "ﬁ"
        assert json.loads(d["codepoints"]) == ["U+FB01"]
        assert d["context"] == "ﬁle"

    def test_nfc_composition_coords_point_to_original(self, client, font_ids):
        # a+U+0301（原文 2 字符）NFC 合成 á（1 字符），坐标仍按原文计
        task = run_task(client, corpus=[{"text": "xáy"}],
                        chain=[font_ids["beta"], font_ids["alpha"]], normalization="NFC")
        res = findings(client, task["id"], kind="missing_glyph")
        assert res["total"] == 1
        item = res["items"][0]
        assert (item["start"], item["end"]) == (1, 3)  # 原文中的 a+U+0301
        assert json.loads(item["detail"])["cp"] == "U+00E1"


# ---------- 4. cmap14 default UVS 语义 ----------

class TestDefaultUVS:
    def test_default_uvs_not_variation_lost(self, client, font_ids):
        # vs 字体对 (A, U+FE0E) 有 default UVS：显式映射回默认字形，不算丢失
        task = run_task(client, corpus=[{"text": "A︎"}], chain=[font_ids["vs"]])
        assert findings(client, task["id"], kind="variation_lost")["total"] == 0
        assert findings(client, task["id"])["total"] == 0

    def test_no_cmap14_still_variation_lost(self, client, font_ids):
        # alpha 无 cmap14：U+FE0E 被静默忽略，仍应报 variation_lost
        task = run_task(client, corpus=[{"text": "A︎"}], chain=[font_ids["alpha"]])
        res = findings(client, task["id"], kind="variation_lost")
        assert res["total"] == 1
        assert json.loads(res["items"][0]["detail"])["base"] == "U+0041"

    def test_specific_uvs_still_ok(self, client, font_ids):
        # 既有行为不回归：(A, U+FE0F) 在 vs 字体有专门变体字形 -> 无问题
        task = run_task(client, corpus=[{"text": "A️"}], chain=[font_ids["vs"]])
        assert findings(client, task["id"])["total"] == 0
