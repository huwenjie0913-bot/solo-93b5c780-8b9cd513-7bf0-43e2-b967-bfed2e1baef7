"""API 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


class CorpusItem(BaseModel):
    text: str = Field(..., description="UTF-8 文本")
    lang: Optional[str] = Field(None, description="BCP47 语言标签，如 zh-Hans、ar、ja")


class TaskCreate(BaseModel):
    name: str = Field("audit", description="任务名")
    corpus: list[CorpusItem] = Field(..., description="带语言标签的语料")
    chain: list[int] = Field(..., description="回退链 A：字体 id，按回退顺序")
    chain_b: Optional[list[int]] = Field(None, description="回退链 B（可选，用于对比）")
    normalization: Literal["none", "NFC", "NFKC"] = Field("none", description="分析前规范化")
    ignore_pua: bool = Field(False, description="忽略私用区码点")
    pua_allowlist: list[Union[int, str]] = Field(
        default_factory=list,
        description='ignore_pua 时仍需审查的 PUA 码点，支持 "U+E000"/"0xE000"/十进制/单字符')
    required_fonts: dict[str, int] = Field(
        default_factory=dict,
        description='脚本必用字体，如 {"Hani": 3, "Latn": 1}（ISO 15924 四字母码 -> 字体 id）')


def parse_codepoint(value: Union[int, str]) -> int:
    """把 'U+E000' / '0xE000' / '57344' / 单字符 解析为码点整数。"""
    if isinstance(value, int):
        cp = value
    else:
        s = str(value).strip()
        if s.upper().startswith("U+"):
            cp = int(s[2:], 16)
        elif s.lower().startswith("0x"):
            cp = int(s, 16)
        elif s.isdigit():
            cp = int(s)
        elif len(s) == 1:
            cp = ord(s)
        else:
            raise ValueError(f"无法解析码点: {value!r}")
    if not (0 <= cp <= 0x10FFFF):
        raise ValueError(f"码点越界: {value!r}")
    return cp
