# -*- coding: utf-8 -*-
"""回复风格后处理：去动作描写 / 去 Markdown / 超长按句截断。

目的是让回复"像人在 QQ 上打字"：短、干净、没有旁白。
"""

from __future__ import annotations

import re

# *动作* / **动作**
_STAR_ACTION = re.compile(r"\*{1,2}[^*\n]{1,60}\*{1,2}")
# 独占一行的（动作）〔动作〕[动作]
_PAREN_LINE = re.compile(r"(?m)^[ \t]*[（(〔\[][^）)〕\]\n]{1,16}[）)〕\]][ \t]*$")
# 行内短括号动作，如"（歪头）"
_INLINE_PAREN = re.compile(r"[（(〔【][^（）()〔〕【】\n]{1,30}[）)〕】]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`\n]{1,60})`")
_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*")
_LIST = re.compile(r"(?m)^[ \t]{0,3}[-*+][ \t]+")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_MULTI_NL = re.compile(r"\n{3,}")

_SENT_END = "。！？!?…~～"

# 语音专用：任何括号/方括号内容都不念（描写、旁白、补充说明）
_BRACKET_ALL = re.compile(r"[（(〔【\[][^（）()〔〕【】\[\]\n]{1,60}[）)〕】\]]")


# 朗读时要清掉的符号：省略号、波浪号、引号、Markdown、emoji
_ELLIPSIS = re.compile(r"[…\.]{2,}")
_TILDE = re.compile(r"[~～]+")
_QUOTES = re.compile(r"[“”\"'‘’「」『』《》<>]")
_MD = re.compile(r"[*_#`|]+")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)
_DUP_PUNCT = re.compile(r"([，。！？、；：])\1+")


# 半角标点 → 全角：Fish 对中文标点的停顿更自然
_HALF2FULL = str.maketrans({
    ",": "，", ":": "：", ";": "；",
    "?": "？", "!": "！",
})
_DOT_NOT_DECIMAL = re.compile(r"(?<!\d)\.(?!\d)")


def strip_for_speech(text: str) -> str:
    """语音专用：去掉旁白与会被念出来的符号，并统一全角标点。"""
    out = _STAR_ACTION.sub("", text or "")
    out = _BRACKET_ALL.sub("", out)
    out = _MD.sub("", out)
    out = _EMOJI.sub("", out)
    out = _QUOTES.sub("", out)
    out = _ELLIPSIS.sub("。", out)
    out = _TILDE.sub("。", out)
    out = _DOT_NOT_DECIMAL.sub("。", out)
    out = out.translate(_HALF2FULL)
    out = _DUP_PUNCT.sub(r"\1", out)
    out = re.sub(r"。{2,}", "。", out)
    out = re.sub(r"[，、；：]{2,}", "，", out)
    out = _MULTI_SPACE.sub(" ", out)
    out = re.sub(r"^[\s，,、；;：:。]+", "", out).strip()
    out = re.sub(r"\s+([。！？!?])", r"\1", out)
    return out


def clean_reply_style(
    text: str,
    *,
    max_chars: int = 80,
    strip_actions: bool = True,
) -> str:
    """把模型回复收拾成"像人打字"的样子。

    Args:
        text: 原始回复。
        max_chars: 软上限，超过后按最后一个句末标点截断；0 表示不限。
        strip_actions: 是否删除 *动作*、（动作）这类旁白。
    """
    out = text or ""

    if strip_actions:
        out = _STAR_ACTION.sub("", out)
        out = _PAREN_LINE.sub("", out)
        out = _INLINE_PAREN.sub("", out)

    out = _BOLD.sub(r"\1", out)
    out = _CODE.sub(r"\1", out)
    out = _HEADING.sub("", out)
    out = _LIST.sub("", out)
    out = _MULTI_SPACE.sub(" ", out)
    out = _MULTI_NL.sub("\n\n", out).strip()
    out = re.sub(r"^[\s，,、；;：:]+", "", out)
    out = re.sub(r"\s+([。！？!?])", r"\1", out)

    if max_chars and len(out) > max_chars:
        cut = -1
        for idx, ch in enumerate(out[:max_chars]):
            if ch in _SENT_END:
                cut = idx + 1
        if cut >= max(20, max_chars // 3):
            out = out[:cut]
        else:
            out = out[:max_chars].rstrip("，,、；;：: ") + "…"

    return out.strip()