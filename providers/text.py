# -*- coding: utf-8 -*-
"""文本清洗与情绪标签处理（不依赖 AstrBot，可单独测试）。"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# 情绪标签
# ---------------------------------------------------------------------------
# Fish Audio 的情绪/表演标签写在正文里，形如 [excited]、[laughing]。
# 只有命中白名单的标签才会被当作"表演指令"：既传给 API，也从 QQ 的可见文本里剔除。
# 未命中白名单的方括号内容（例如 [图片]、[链接]）会原样保留，避免误删正文。
DEFAULT_EMOTION_TAGS = (
    # 英文常用
    "excited", "happy", "sad", "angry", "surprised", "fearful", "disgusted",
    "neutral", "calm", "gentle", "soft", "whisper", "whispering", "shouting",
    "laughing", "laugh", "chuckle", "giggle", "sigh", "sighs", "crying",
    "cry", "sobbing", "teary", "nervous", "shy", "embarrassed", "confident",
    "serious", "sarcastic", "playful", "cheerful", "tired", "sleepy", "bored",
    "curious", "proud", "disappointed", "anxious", "confused", "relieved",
    "surprise", "emphasis", "pause", "breath", "breathing", "scream",
    "moaning", "gasp", "sniffing", "yawn", "humming", "singing", "panting",
    # 中文常用
    "开心", "高兴", "激动", "兴奋", "难过", "伤心", "生气", "愤怒", "惊讶",
    "害怕", "恐惧", "厌恶", "平静", "温柔", "轻声", "耳语", "大喊", "笑声",
    "大笑", "轻笑", "叹气", "哭泣", "抽泣", "紧张", "害羞", "自信", "认真",
    "讽刺", "俏皮", "疲惫", "困倦", "无聊", "好奇", "骄傲", "失望", "焦虑",
    "困惑", "释然", "强调", "停顿", "呼吸", "喘气", "哈欠", "哼唱", "唱歌",
)

_TAG_RE = re.compile(r"\[([^\[\]\n]{1,24})\]")
_TAG_STRIP_RE = re.compile(r"[ \t]{2,}")


def _norm_tag(tag: str) -> str:
    return tag.strip().lower()


def build_tag_whitelist(extra: list[str] | None = None) -> set[str]:
    """默认标签 + 用户自定义标签。"""
    tags = {_norm_tag(t) for t in DEFAULT_EMOTION_TAGS}
    for item in extra or []:
        if isinstance(item, str) and item.strip():
            tags.add(_norm_tag(item))
    return tags


def normalize_text(text: str) -> str:
    """统一全半角、去掉零宽字符、压缩空白。"""
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", str(text))
    out = out.replace("\u200b", "").replace("\ufeff", "").replace("\u3000", " ")
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def extract_emotion_tags(text: str, whitelist: set[str]) -> tuple[str, list[str]]:
    """剥离白名单内的情绪标签。

    Returns:
        (去掉标签后的文本, 按出现顺序去重的标签列表)
    """
    tags: list[str] = []
    seen: set[str] = set()

    def _repl(match: re.Match[str]) -> str:
        tag = _norm_tag(match.group(1))
        if tag not in whitelist:
            return match.group(0)
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
        return " "

    cleaned = _TAG_RE.sub(_repl, text or "")
    cleaned = _TAG_STRIP_RE.sub(" ", cleaned).strip()
    return cleaned, tags


def strip_markdown(text: str) -> str:
    """去掉 Markdown 装饰，保留可朗读的正文。"""
    out = text or ""
    # 代码块整体丢弃（念出来没有意义）
    out = re.sub(r"```.*?```", " ", out, flags=re.S)
    out = re.sub(r"~~~.*?~~~", " ", out, flags=re.S)
    # 行内代码只保留内容
    out = re.sub(r"`([^`\n]{1,80})`", r"\1", out)
    # 标题 / 引用 / 列表符号
    out = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", out)
    out = re.sub(r"(?m)^\s{0,3}>\s?", "", out)
    out = re.sub(r"(?m)^\s{0,3}[-*+]\s+", "", out)
    out = re.sub(r"(?m)^\s{0,3}\d{1,2}[.)]\s+", "", out)
    # 粗体 / 斜体 / 删除线
    out = re.sub(r"(\*\*|__)(.+?)\1", r"\2", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", out)
    out = re.sub(r"~~(.+?)~~", r"\1", out)
    # 表格分隔线
    out = re.sub(r"(?m)^\s*\|?[\s:|-]{6,}\|?\s*$", " ", out)
    out = out.replace("|", " ")
    # 裸链接 / 图片语法
    out = re.sub(r"!?\[([^\]]{0,40})\]\((?:https?://|/)[^)]+\)", r"\1", out)
    out = re.sub(r"https?://\S+", " ", out)
    # @提及、CQ 码
    out = re.sub(r"\[CQ:[^\]]+\]", " ", out)
    out = re.sub(r"@\S{1,30}", " ", out)
    return out


def clean_for_speech(text: str) -> str:
    """面向 TTS 的清洗：markdown、链接、空白折叠。"""
    return re.sub(r"\s+", " ", strip_markdown(text or "")).strip()


_SENT_END = "。！？!?…~～；;\n"


def split_for_tts(text: str, max_chars: int, max_segments: int) -> list[str]:
    """按句子边界切分，尽量每段不超过 max_chars。

    超长且无法切分的句子会被硬切；总段数受 max_segments 限制。
    """
    text = (text or "").strip()
    if not text:
        return []
    max_chars = max(10, int(max_chars))
    max_segments = max(1, int(max_segments))
    if len(text) <= max_chars:
        return [text]

    sentences: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENT_END:
            if buf.strip():
                sentences.append(buf.strip())
            buf = ""
    if buf.strip():
        sentences.append(buf.strip())

    segments: list[str] = []
    current = ""
    for sent in sentences:
        while len(sent) > max_chars:
            if current:
                segments.append(current)
                current = ""
            segments.append(sent[:max_chars])
            sent = sent[max_chars:]
        if not sent:
            continue
        if len(current) + len(sent) <= max_chars:
            current += sent
        else:
            if current:
                segments.append(current)
            current = sent
    if current:
        segments.append(current)

    if len(segments) > max_segments:
        segments = segments[:max_segments]
        segments[-1] = segments[-1][: max(10, max_chars - 1)] + "…"
    return [s for s in segments if s.strip()]
