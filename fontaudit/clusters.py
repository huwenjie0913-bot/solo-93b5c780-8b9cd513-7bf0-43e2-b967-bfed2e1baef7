"""Unicode 字素簇切分：UAX #29 extended grapheme cluster。

字素分类（GCB）、InCB（GB9c）、Extended_Pictographic（GB11）全部使用代码内置的
Unicode 17.0.0 官方数据（gcb_data.py / incb_data.py / emoji_data.py），
与运行时 unicodedata 版本无关——旧 Python 运行时也能正确切分新版 Unicode 文本。
"""
from __future__ import annotations

from .emoji_data import is_extended_pictographic
from .gcb_data import gcb_of
from .incb_data import is_incb_consonant, is_incb_extend, is_incb_linker

# GCB 值名（官方数据）在规则中的简写
_RI = "Regional_Indicator"


def _gb11(props: list[str], extp: list[bool], i: int) -> bool:
    """GB11: ExtPict Extend* ZWJ × ExtPict（emoji ZWJ 序列不断开）。"""
    if props[i - 1] != "ZWJ":
        return False
    j = i - 2
    while j >= 0 and props[j] in ("Extend", "ZWJ"):
        j -= 1
    return j >= 0 and extp[j]


def _gb9c(cps: list[int], i: int) -> bool:
    """GB9c: Consonant [Linker Extend]* Linker [Linker Extend]* × Consonant。

    印地语等文字的辅音连字（如 क्ष = क + ् + ष）不得断开。
    Consonant/Linker/Extend 均为 InCB 属性（见 incb_data.py，官方 UCD 数据）；
    InCB=Extend 依官方定义包含 ZWJ，故 क्‍ष（含 ZWJ）同样不断开。
    """
    if not is_incb_consonant(cps[i]):
        return False
    j = i - 1
    saw_linker = False
    while j >= 0 and (is_incb_linker(cps[j]) or is_incb_extend(cps[j])):
        if is_incb_linker(cps[j]):
            saw_linker = True
        j -= 1
    return saw_linker and j >= 0 and is_incb_consonant(cps[j])


def segment_clusters(text: str) -> list[tuple[int, int, str]]:
    """把文本切成字素簇，返回 [(start, end, cluster_text), ...]（字符偏移）。"""
    n = len(text)
    if n == 0:
        return []
    cps = [ord(c) for c in text]
    props = [gcb_of(cp) for cp in cps]
    extp = [is_extended_pictographic(cp) for cp in cps]
    clusters: list[tuple[int, int, str]] = []
    start = 0
    ri_run = 1 if props[0] == _RI else 0  # 当前连续 RI 个数（GB12/13）
    for i in range(1, n):
        p, c = props[i - 1], props[i]
        br = True
        if p == "CR" and c == "LF":
            br = False  # GB3
        elif p in ("CR", "LF", "Control"):
            br = True  # GB4
        elif c in ("CR", "LF", "Control"):
            br = True  # GB5
        elif p == "L" and c in ("L", "V", "LV", "LVT"):
            br = False  # GB6
        elif p in ("LV", "V") and c in ("V", "T"):
            br = False  # GB7
        elif p in ("LVT", "T") and c == "T":
            br = False  # GB8
        elif c in ("Extend", "ZWJ"):
            br = False  # GB9
        elif c == "SpacingMark":
            br = False  # GB9a
        elif p == "Prepend":
            br = False  # GB9b
        elif _gb9c(cps, i):
            br = False  # GB9c：印地语辅音连字
        elif extp[i] and _gb11(props, extp, i):
            br = False  # GB11
        elif p == _RI and c == _RI and ri_run % 2 == 1:
            br = False  # GB12/13：RI 两两成对
        if br:
            clusters.append((start, i, text[start:i]))
            start = i
        if c == _RI:
            ri_run = ri_run + 1 if (p == _RI and not br) else 1
        else:
            ri_run = 0
    clusters.append((start, n, text[start:]))
    return clusters
