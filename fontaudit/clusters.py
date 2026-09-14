"""Unicode 字素簇切分：UAX #29 extended grapheme cluster 的实用实现。

覆盖本工具关注的场景：组合附标（Extend/SpacingMark）、emoji ZWJ 序列（GB11）、
变体选择符、emoji 修饰符、标签字符、区域指示符成对（GB12/13，国旗）、
Hangul L/V/T 序列（GB6-8）、Prepend（GB9b）、印地语辅音连字（GB9c）。
"""
from __future__ import annotations

import unicodedata
from bisect import bisect_right

from .emoji_data import is_extended_pictographic
from .incb_data import is_incb_consonant, is_incb_extend, is_incb_linker

# Grapheme_Cluster_Break=Prepend（如阿拉伯文数字前缀符）
PREPEND_RANGES: tuple[tuple[int, int], ...] = (
    (0x0600, 0x0605), (0x06DD, 0x06DD), (0x070F, 0x070F), (0x08E2, 0x08E2),
    (0x0D4E, 0x0D4E), (0x110BD, 0x110BD), (0x110CD, 0x110CD),
    (0x111C2, 0x111C3), (0x1193D, 0x1193E), (0x11A3A, 0x11A3A),
    (0x11A84, 0x11A89), (0x11D46, 0x11D46),
)
_PREPEND_STARTS = [a for a, _ in PREPEND_RANGES]


def _in_ranges(cp: int, ranges: tuple[tuple[int, int], ...], starts: list[int]) -> bool:
    i = bisect_right(starts, cp) - 1
    return i >= 0 and ranges[i][0] <= cp <= ranges[i][1]


def _gcb(ch: str) -> str:
    """返回字符的 Grapheme_Cluster_Break 类别（简化命名）。"""
    cp = ord(ch)
    if cp == 0x0D:
        return "CR"
    if cp == 0x0A:
        return "LF"
    if cp == 0x200D:
        return "ZWJ"
    if _in_ranges(cp, PREPEND_RANGES, _PREPEND_STARTS):
        return "Prepend"
    if 0x1F1E6 <= cp <= 0x1F1FF:
        return "RI"
    # Hangul 音节算法
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV" if (cp - 0xAC00) % 28 == 0 else "LVT"
    # 特殊 Cf / 格式字符
    if cp == 0x200C:  # ZWNJ
        return "Extend"
    if 0x1F3FB <= cp <= 0x1F3FF:  # emoji 肤色修饰符（GCB=Extend）
        return "Extend"
    if 0xE0020 <= cp <= 0xE007F:  # 标签字符
        return "Extend"
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me"):  # 组合符、变体选择符、keycap 等
        return "Extend"
    if cat == "Mc":
        return "SpacingMark"
    if cat in ("Cc", "Cf", "Cs", "Zl", "Zp"):
        return "Control"
    return "Other"


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
    props = [_gcb(c) for c in text]
    extp = [is_extended_pictographic(cp) for cp in cps]
    clusters: list[tuple[int, int, str]] = []
    start = 0
    ri_run = 1 if props[0] == "RI" else 0  # 当前连续 RI 个数（GB12/13）
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
        elif p == "RI" and c == "RI" and ri_run % 2 == 1:
            br = False  # GB12/13：RI 两两成对
        if br:
            clusters.append((start, i, text[start:i]))
            start = i
        if c == "RI":
            ri_run = ri_run + 1 if (p == "RI" and not br) else 1
        else:
            ri_run = 0
    clusters.append((start, n, text[start:]))
    return clusters
