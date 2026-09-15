"""UAX #9（Unicode Bidirectional Algorithm）实现，供双向文本审查使用。

覆盖规则：P1-P3 段落与基础方向、X1-X10 显式嵌入/覆盖/隔离（125 层上限与
溢出计数）、BD9/BD13 隔离匹配与隔离运行序列、W1-W7 弱类型、N0 配对括号、
N1-N2 中性字符、I1-I2 隐式层级、L1 行尾重置、L2 视觉重排。

与渲染器不同，这里保留每个字符的层级与视觉位置（含 BN/控制符，不删除），
并在 X 阶段记录控制符审计事件（未闭合/孤立/溢出），供上层生成问题报告。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

MAX_DEPTH = 125  # UAX #9 显式层级上限

LRE, RLE, PDF = 0x202A, 0x202B, 0x202C
LRO, RLO = 0x202D, 0x202E
LRI, RLI, FSI, PDI = 0x2066, 0x2067, 0x2068, 0x2069

CONTROL_NAMES = {
    LRE: "LRE", RLE: "RLE", PDF: "PDF", LRO: "LRO", RLO: "RLO",
    LRI: "LRI", RLI: "RLI", FSI: "FSI", PDI: "PDI",
}
NAME_TO_CP = {v: k for k, v in CONTROL_NAMES.items()}
EMBED_TYPES = {"LRE", "RLE", "LRO", "RLO"}   # 嵌入/覆盖（PDF 闭合）
ISOLATE_TYPES = {"LRI", "RLI", "FSI"}        # 隔离（PDI 闭合）
# 闭合符 -> 开启符类型，用于生成"补闭合符"建议
CLOSER_FOR = {"LRE": PDF, "RLE": PDF, "LRO": PDF, "RLO": PDF,
              "LRI": PDI, "RLI": PDI, "FSI": PDI}

# 配对括号（BidiBrackets.txt 中 UI 文本常见子集）
_BRACKET_PAIR_CPS = (
    (0x0028, 0x0029), (0x005B, 0x005D), (0x007B, 0x007D),
    (0x0F3A, 0x0F3B), (0x0F3C, 0x0F3D), (0x169B, 0x169C),
    (0x2045, 0x2046), (0x207D, 0x207E), (0x208D, 0x208E),
    (0x2308, 0x2309), (0x230A, 0x230B), (0x2329, 0x232A),
    (0x2768, 0x2769), (0x276A, 0x276B), (0x276C, 0x276D),
    (0x276E, 0x276F), (0x2770, 0x2771), (0x2772, 0x2773),
    (0x2774, 0x2775), (0x27C5, 0x27C6), (0x27E6, 0x27E7),
    (0x27E8, 0x27E9), (0x27EA, 0x27EB), (0x27EC, 0x27ED),
    (0x27EE, 0x27EF), (0x2983, 0x2984), (0x2985, 0x2986),
    (0x2987, 0x2988), (0x2989, 0x298A), (0x298B, 0x298C),
    (0x298D, 0x2990), (0x298E, 0x298F), (0x2991, 0x2992),
    (0x2993, 0x2994), (0x2995, 0x2996), (0x2997, 0x2998),
    (0x29D8, 0x29D9), (0x29DA, 0x29DB), (0x29FC, 0x29FD),
    (0x2E02, 0x2E03), (0x2E04, 0x2E05), (0x2E09, 0x2E0A),
    (0x2E0C, 0x2E0D), (0x2E1C, 0x2E1D), (0x2E20, 0x2E21),
    (0x2E22, 0x2E23), (0x2E24, 0x2E25), (0x2E26, 0x2E27), (0x2E28, 0x2E29),
    (0x3008, 0x3009), (0x300A, 0x300B), (0x300C, 0x300D),
    (0x300E, 0x300F), (0x3010, 0x3011), (0x3014, 0x3015),
    (0x3016, 0x3017), (0x3018, 0x3019), (0x301A, 0x301B),
    (0xFE59, 0xFE5A), (0xFE5B, 0xFE5C), (0xFE5D, 0xFE5E),
    (0xFF08, 0xFF09), (0xFF3B, 0xFF3D), (0xFF5B, 0xFF5D),
    (0xFF5F, 0xFF60), (0xFF62, 0xFF63),
)
OPEN_TO_CLOSE = {o: c for o, c in _BRACKET_PAIR_CPS}
CLOSE_TO_OPEN = {c: o for o, c in _BRACKET_PAIR_CPS}

# 未分配码点的默认 Bidi_Class（UAX #9 Table 4 主要区间）
_DEFAULT_CLASS = (
    (0x0590, 0x05FF, "R"), (0x07C0, 0x07FF, "R"), (0xFB1D, 0xFB4F, "R"),
    (0x0600, 0x07BF, "AL"), (0x0800, 0x08FF, "AL"),
    (0xFB50, 0xFDFF, "AL"), (0xFE70, 0xFEFF, "AL"), (0x1EE00, 0x1EEFF, "AL"),
)


def bidi_class(ch: str) -> str:
    """字符的 Bidi_Class；未分配码点按 UAX #9 Table 4 取默认值。"""
    cls = unicodedata.bidirectional(ch)
    if cls:
        return cls
    cp = ord(ch)
    for a, b, c in _DEFAULT_CLASS:
        if a <= cp <= b:
            return c
    return "L"


@dataclass
class ControlEvent:
    """X 阶段控制符事件。event: open / close / stray / overflow / unclosed。"""
    offset: int            # 段落内偏移
    name: str              # LRE/RLE/.../PDI
    event: str
    depth: int = 0         # open/unclosed 时的嵌套深度（不含段落基项）
    resolved: str = ""     # FSI 解析结果（LRI/RLI）

    @property
    def cp(self) -> int:
        return NAME_TO_CP[self.name]


@dataclass
class ParagraphResult:
    start: int                 # 段落在原文中的起始偏移
    end: int                   # 结束偏移（不含）
    base_level: int
    levels: list[int]          # 每字符最终层级（与段落内偏移对齐，含 BN/控制符）
    visual_order: list[int]    # 显示顺序：段落内偏移序列（含不可见控制符）
    events: list[ControlEvent] = field(default_factory=list)

    @property
    def base_dir(self) -> str:
        return "rtl" if self.base_level else "ltr"


def _least_greater(cur: int, odd: bool) -> int:
    """大于 cur 的最小奇（odd=True）/偶层级。"""
    v = cur + 1
    return v if (v & 1) == int(odd) else v + 1


def _match_isolates(types: list[str]) -> dict[int, int | None]:
    """BD9 结构匹配（忽略溢出语义）：{隔离起始符下标: 匹配 PDI 下标或 None}。"""
    stack: list[int] = []
    match: dict[int, int | None] = {}
    for i, t in enumerate(types):
        if t in ISOLATE_TYPES:
            stack.append(i)
        elif t == "PDI" and stack:
            match[stack.pop()] = i
    for i in stack:
        match[i] = None
    return match


def _first_strong(types: list[str], start: int, end: int,
                  structural: dict[int, int | None]) -> str | None:
    """P2：区间内首个强类型（跳过隔离区内容），返回 'L'/'R'/None。"""
    i = start
    while i < end:
        t = types[i]
        if t in ISOLATE_TYPES:
            m = structural.get(i)
            i = m + 1 if m is not None and m < end else end
            continue
        if t == "L":
            return "L"
        if t in ("R", "AL"):
            return "R"
        i += 1
    return None


def _explicit(types: list[str], base_level: int):
    """X1-X10：返回 (levels, rtypes, events, iso_match)。

    rtypes 中 X9 移除类（嵌入/覆盖/PDF/BN）置 'BN'，隔离起始符/PDI 置 'ON'；
    iso_match 只含有效（未溢出）隔离的 {起始符: PDI}。
    """
    n = len(types)
    levels = [base_level] * n
    rtypes = list(types)
    events: list[ControlEvent] = []
    structural = _match_isolates(types)
    iso_match: dict[int, int] = {}
    # (level, override, isolate?, opener_offset)
    stack: list[tuple[int, str, bool, int]] = [(base_level, "N", False, -1)]
    overflow_isolate = 0
    overflow_embedding = 0
    valid_isolates = 0
    cur = base_level
    override = "N"

    for i, t in enumerate(types):
        if t in EMBED_TYPES:                       # X2-X5
            levels[i] = cur
            rtypes[i] = "BN"
            if overflow_isolate:
                continue                           # 溢出隔离区内，静默忽略
            new = _least_greater(cur, t in ("RLE", "RLO"))
            if overflow_embedding == 0 and new <= MAX_DEPTH:
                ov = {"LRO": "L", "RLO": "R"}.get(t, "N")
                stack.append((new, ov, False, i))
                cur, override = new, ov
                events.append(ControlEvent(i, t, "open", len(stack) - 1))
            else:
                overflow_embedding += 1
                events.append(ControlEvent(i, t, "overflow", len(stack) - 1))
        elif t in ISOLATE_TYPES:                   # X5a-X5c
            levels[i] = cur
            rtypes[i] = "ON"
            eff = t
            if t == "FSI":
                m = structural.get(i)
                d = _first_strong(types, i + 1, m if m is not None else n, structural)
                eff = "RLI" if d == "R" else "LRI"
            if overflow_isolate or overflow_embedding:
                overflow_isolate += 1
                events.append(ControlEvent(i, t, "overflow", len(stack) - 1, eff))
            else:
                new = _least_greater(cur, eff == "RLI")
                if new <= MAX_DEPTH:
                    stack.append((new, "N", True, i))
                    valid_isolates += 1
                    cur, override = new, "N"
                    events.append(ControlEvent(i, t, "open", len(stack) - 1, eff))
                else:
                    overflow_isolate += 1
                    events.append(ControlEvent(i, t, "overflow", len(stack) - 1, eff))
        elif t == "PDI":                           # X6a
            rtypes[i] = "ON"
            if overflow_isolate:
                overflow_isolate -= 1
                levels[i] = cur
            elif valid_isolates == 0:
                levels[i] = cur
                events.append(ControlEvent(i, t, "stray"))
            else:
                overflow_embedding = 0
                while not stack[-1][2]:            # 弹到最近一个隔离项（含）
                    stack.pop()
                opener = stack.pop()[3]
                valid_isolates -= 1
                iso_match[opener] = i
                cur, override = stack[-1][0], stack[-1][1]
                levels[i] = cur                    # PDI 取弹栈后的外层层级
                events.append(ControlEvent(i, t, "close"))
        elif t == "PDF":                           # X7
            levels[i] = cur
            rtypes[i] = "BN"
            if overflow_isolate:
                pass
            elif overflow_embedding:
                overflow_embedding -= 1
            elif len(stack) > 1 and not stack[-1][2]:
                stack.pop()
                cur, override = stack[-1][0], stack[-1][1]
                events.append(ControlEvent(i, t, "close"))
            else:
                events.append(ControlEvent(i, t, "stray"))
        elif t == "B":                             # X8
            levels[i] = base_level
        elif t == "BN":
            levels[i] = cur
        else:                                      # X6
            levels[i] = cur
            if override != "N":
                rtypes[i] = override

    # 段落结束仍未弹出的栈项 = 未闭合控制符（越界到段落之外）
    for depth, (_, _, _, opener) in enumerate(stack[1:], start=1):
        events.append(ControlEvent(opener, types[opener], "unclosed", depth))
    return levels, rtypes, events, iso_match


def _level_runs(indices: list[int], levels: list[int]) -> list[tuple[int, list[int]]]:
    """X10：把（未移除）下标按层级切成 level run。"""
    runs: list[tuple[int, list[int]]] = []
    for i in indices:
        if runs and runs[-1][0] == levels[i]:
            runs[-1][1].append(i)
        else:
            runs.append((levels[i], [i]))
    return runs


def _isolating_sequences(runs, types, iso_match):
    """BD13：run 末字符是有效隔离起始符、其匹配 PDI 是某 run 首字符时链接。"""
    first_char_run = {run[1][0]: k for k, run in enumerate(runs)}
    nxt: dict[int, int] = {}
    for k, (_, idxs) in enumerate(runs):
        last = idxs[-1]
        if types[last] in ISOLATE_TYPES and last in iso_match:
            pdi = iso_match[last]
            if pdi in first_char_run:
                nxt[k] = first_char_run[pdi]
    linked = set(nxt.values())
    sequences = []
    for k in range(len(runs)):
        if k in linked:
            continue
        seq = [k]
        while seq[-1] in nxt:
            seq.append(nxt[seq[-1]])
        sequences.append(seq)
    return sequences


def _resolve_weak(chars: list[int], rtypes: list[str], sos: str) -> None:
    """W1-W7，在一个隔离运行序列上原地解析弱类型。"""
    prev = sos                                          # W1
    for i in chars:
        if rtypes[i] == "NSM":
            rtypes[i] = prev
        prev = rtypes[i]
    prev_strong = sos                                   # W2
    for i in chars:
        t = rtypes[i]
        if t == "EN" and prev_strong == "AL":
            rtypes[i] = "AN"
        if t in ("L", "R", "AL"):
            prev_strong = t
    for i in chars:                                     # W3
        if rtypes[i] == "AL":
            rtypes[i] = "R"
    for k in range(1, len(chars) - 1):                  # W4
        t, p, q = rtypes[chars[k]], rtypes[chars[k - 1]], rtypes[chars[k + 1]]
        if t == "ES" and p == q == "EN":
            rtypes[chars[k]] = "EN"
        elif t == "CS" and p == q and p in ("EN", "AN"):
            rtypes[chars[k]] = p
    for k, i in enumerate(chars):                       # W5
        if rtypes[i] == "EN":
            j = k - 1
            while j >= 0 and rtypes[chars[j]] == "ET":
                rtypes[chars[j]] = "EN"
                j -= 1
            j = k + 1
            while j < len(chars) and rtypes[chars[j]] == "ET":
                rtypes[chars[j]] = "EN"
                j += 1
    for i in chars:                                     # W6
        if rtypes[i] in ("ES", "ET", "CS"):
            rtypes[i] = "ON"
    prev_strong = sos                                   # W7
    for i in chars:
        t = rtypes[i]
        if t == "EN" and prev_strong == "L":
            rtypes[i] = "L"
        if t in ("L", "R"):
            prev_strong = t


def _strong_of(t: str) -> str | None:
    """N0/N1 的强类型归并：EN/AN 视为 R。"""
    if t == "L":
        return "L"
    if t in ("R", "EN", "AN"):
        return "R"
    return None


def _resolve_brackets(chars: list[int], rtypes: list[str], levels: list[int],
                      cps: list[int], sos: str) -> None:
    """N0：处理序列内配对括号（BD16 栈式配对）。"""
    pairs: list[tuple[int, int]] = []
    stack: list[tuple[int, int]] = []
    for pos, i in enumerate(chars):
        cp = cps[i]
        if cp in OPEN_TO_CLOSE:
            stack.append((cp, pos))
        elif cp in CLOSE_TO_OPEN and stack and stack[-1][0] == CLOSE_TO_OPEN[cp]:
            pairs.append((stack.pop()[1], pos))
    for opos, cpos in pairs:
        oi, ci = chars[opos], chars[cpos]
        e = "R" if levels[oi] & 1 else "L"             # 括号对的嵌入方向
        o = "L" if e == "R" else "R"
        enclosed = {_strong_of(rtypes[j]) for j in chars[opos + 1:cpos]}
        enclosed.discard(None)
        if e in enclosed:
            rtypes[oi] = rtypes[ci] = e
        elif o in enclosed:
            prev = sos
            for j in chars[:opos]:
                s = _strong_of(rtypes[j])
                if s:
                    prev = s
            rtypes[oi] = rtypes[ci] = o if prev == o else e


def _resolve_neutral(chars: list[int], rtypes: list[str], levels: list[int],
                     sos: str, eor: str) -> None:
    """N1/N2：两侧同向取该方向，否则取嵌入方向。"""
    seq_start: int | None = None
    prev_type = sos
    # 末尾补一个 eor 哨兵，迫使行尾中性序列被结算
    for k, i in enumerate([*chars, None]):
        t = eor if i is None else rtypes[i]
        if t in ("WS", "ON", "S", "B"):
            if seq_start is None:
                seq_start = k
                prev_type = sos if k == 0 else rtypes[chars[k - 1]]
        elif seq_start is not None:
            p = _strong_of(prev_type) or prev_type
            q = _strong_of(t) or t
            for m in range(seq_start, k):
                mi = chars[m]
                rtypes[mi] = p if p == q else ("R" if levels[mi] & 1 else "L")
            seq_start = None


def _resolve_implicit(indices: list[int], rtypes: list[str], levels: list[int]) -> None:
    """I1/I2：按当前层级奇偶提升。"""
    for i in indices:
        t = rtypes[i]
        if t not in ("L", "R", "EN", "AN"):
            continue
        if levels[i] & 1 == 0:
            if t == "R":
                levels[i] += 1
            elif t in ("EN", "AN"):
                levels[i] += 2
        elif t in ("L", "EN", "AN"):
            levels[i] += 1


def _assign_removed_levels(rtypes: list[str], levels: list[int], base_level: int) -> None:
    """X9 移除类（显式控制符/BN）的最终层级：取前一字符的最终层级
    （链式向前，等效于最近一个未移除字符的层级），段首取段落层级。"""
    for i in range(len(levels)):
        if rtypes[i] == "BN":
            levels[i] = levels[i - 1] if i > 0 else base_level


def _reset_line_ends(types: list[str], levels: list[int], base_level: int) -> None:
    """L1：B/S 及其前导空白、行尾空白与隔离格式符重置为段落层级。"""
    resetting = True
    for i in range(len(levels) - 1, -1, -1):
        o = types[i]
        if o in ("B", "S"):
            levels[i] = base_level
            resetting = True
        elif resetting and (o == "WS" or o in ISOLATE_TYPES or o == "PDI"):
            levels[i] = base_level
        else:
            resetting = False


def _visual_order(levels: list[int]) -> list[int]:
    """L2：从最高层到最低奇数层，逐层反转不低于该层的连续序列。"""
    order = list(range(len(levels)))
    odd = [l for l in levels if l & 1]
    if not odd:
        return order
    for lvl in range(max(levels), min(odd) - 1, -1):
        k = 0
        while k < len(order):
            if levels[order[k]] >= lvl:
                j = k
                while j + 1 < len(order) and levels[order[j + 1]] >= lvl:
                    j += 1
                order[k:j + 1] = reversed(order[k:j + 1])
                k = j + 1
            else:
                k += 1
    return order


def resolve_paragraph(text: str, base_level: int, offset: int = 0) -> ParagraphResult:
    """解析单个段落，返回层级、视觉顺序与控制符事件。"""
    types = [bidi_class(c) for c in text]
    cps = [ord(c) for c in text]
    levels, rtypes, events, iso_match = _explicit(types, base_level)

    indices = [i for i in range(len(text)) if rtypes[i] != "BN"]
    runs = _level_runs(indices, levels)
    for seq in _isolating_sequences(runs, types, iso_match):
        chars = [i for k in seq for i in runs[k][1]]
        first, last = seq[0], seq[-1]
        prev_level = runs[first - 1][0] if first > 0 else base_level
        next_level = runs[last + 1][0] if last + 1 < len(runs) else base_level
        sos = "R" if max(runs[first][0], prev_level) & 1 else "L"
        eor = "R" if max(runs[last][0], next_level) & 1 else "L"
        _resolve_weak(chars, rtypes, sos)
        _resolve_brackets(chars, rtypes, levels, cps, sos)
        _resolve_neutral(chars, rtypes, levels, sos, eor)
    _resolve_implicit(indices, rtypes, levels)
    _assign_removed_levels(rtypes, levels, base_level)
    _reset_line_ends(types, levels, base_level)
    order = _visual_order(levels)
    return ParagraphResult(offset, offset + len(text), base_level, levels, order, events)


def resolve_text(text: str, base_dir: str = "auto") -> list[ParagraphResult]:
    """按 B 类字符切段落并逐段解析。base_dir: 'ltr'/'rtl'/'auto'（首强字符）。"""
    paragraphs: list[ParagraphResult] = []
    start = 0
    spans: list[tuple[int, int]] = []
    for i, ch in enumerate(text):
        if bidi_class(ch) == "B":
            spans.append((start, i + 1))
            start = i + 1
    if start < len(text):
        spans.append((start, len(text)))
    for s, e in spans:
        seg = text[s:e]
        types = [bidi_class(c) for c in seg]
        if base_dir == "auto":
            d = _first_strong(types, 0, len(types), _match_isolates(types))
            level = 1 if d == "R" else 0
        else:
            level = 1 if base_dir == "rtl" else 0
        paragraphs.append(resolve_paragraph(seg, level, s))
    return paragraphs
