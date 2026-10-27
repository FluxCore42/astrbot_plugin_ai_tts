# -*- coding: utf-8 -*-
"""情绪识别与韵律参数（借鉴 astrbot_plugin_tts_emotion_router 的 18 类情绪）。

情绪路由插件把情绪交给各家 TTS 的专属参数（MiniMax/MiMo）。
Fish Audio 没有情绪字段，但支持 `prosody.speed / volume`（实测生效），
所以这里把 18 类情绪映射成"语速 + 音量"的细微差异，让声音有起伏。
"""

from __future__ import annotations

import re
from typing import Dict, Optional

# 18 类情绪（基础 9 + 复合 9）
BASIC_EMOTIONS = (
    "happy", "sad", "angry", "fear", "surprise",
    "excited", "wronged", "neutral", "cold",
)
COMPLEX_EMOTIONS = (
    "melancholy", "gratified", "helpless", "guilty", "relieved",
    "jealous", "weary", "anxious", "affectionate",
)
EMOTIONS = BASIC_EMOTIONS + COMPLEX_EMOTIONS

# 关键词命中（中文口语为主，英文只保留高频词）
EMOTION_KEYWORDS: Dict[str, re.Pattern] = {
    "happy": re.compile(r"(开心|高兴|哈哈|嘻嘻|太好了|好耶|棒|嘿嘿|喜欢|好呀)", re.I),
    "sad": re.compile(r"(难过|伤心|呜呜|唉|可惜|心疼|失落|想哭|不开心)", re.I),
    "angry": re.compile(r"(生气|气死|可恶|烦死|讨厌|哼|别闹|过分)", re.I),
    "fear": re.compile(r"(害怕|恐惧|吓死|好可怕|瑟瑟发抖|不敢|怕)", re.I),
    "surprise": re.compile(r"(惊讶|哇|天哪|不会吧|真的吗|我去|震惊|居然)", re.I),
    "excited": re.compile(r"(兴奋|激动|太棒了|冲冲冲|等不及|好想|终于|好久)", re.I),
    "wronged": re.compile(r"(委屈|冤枉|好委屈|呜呜呜|凭什么)", re.I),
    "cold": re.compile(r"(冷漠|冷淡|无所谓|随便|随你|哦)", re.I),
    "melancholy": re.compile(r"(怅然|若有所思|感慨|惆怅|有点空)", re.I),
    "gratified": re.compile(r"(欣慰|安慰|值得了|没白费)", re.I),
    "helpless": re.compile(r"(无奈|无可奈何|没办法|只能这样|算了)", re.I),
    "guilty": re.compile(r"(愧疚|抱歉|对不起|不好意思|怪我)", re.I),
    "relieved": re.compile(r"(释然|放心了|松了口气|悬着的心|还好)", re.I),
    "jealous": re.compile(r"(嫉妒|羡慕|好羡慕|酸了)", re.I),
    "weary": re.compile(r"(厌倦|疲惫|累了|困了|够了|好累|不想动)", re.I),
    "anxious": re.compile(r"(忐忑|紧张|不安|担心|慌)", re.I),
    "affectionate": re.compile(r"(动情|深情|感动|喜欢你|想你|抱抱|撒娇)", re.I),
}

# 情绪 → Fish prosody（speed/volume 基准 1.0）
EMOTION_PROFILES: Dict[str, dict] = {
    "happy": {"speed": 1.10, "volume": 1.10},
    "sad": {"speed": 0.86, "volume": 0.85},
    "angry": {"speed": 1.14, "volume": 1.25},
    "fear": {"speed": 1.06, "volume": 0.85},
    "surprise": {"speed": 1.12, "volume": 1.10},
    "excited": {"speed": 1.18, "volume": 1.20},
    "wronged": {"speed": 0.90, "volume": 0.85},
    "neutral": {"speed": 1.00, "volume": 1.00},
    "cold": {"speed": 0.95, "volume": 0.90},
    "melancholy": {"speed": 0.87, "volume": 0.85},
    "gratified": {"speed": 0.95, "volume": 1.00},
    "helpless": {"speed": 0.90, "volume": 0.90},
    "guilty": {"speed": 0.92, "volume": 0.88},
    "relieved": {"speed": 0.98, "volume": 1.00},
    "jealous": {"speed": 1.06, "volume": 1.05},
    "weary": {"speed": 0.84, "volume": 0.85},
    "anxious": {"speed": 1.12, "volume": 0.95},
    "affectionate": {"speed": 0.92, "volume": 0.95},
}

# 中文标签（可选：作为情绪提示塞进文本，默认关闭，避免被念出来）
EMOTION_TAGS_CN: Dict[str, str] = {
    "happy": "开心", "sad": "难过", "angry": "生气", "fear": "害怕",
    "surprise": "惊讶", "excited": "兴奋", "wronged": "委屈", "neutral": "",
    "cold": "冷淡", "melancholy": "怅然", "gratified": "欣慰",
    "helpless": "无奈", "guilty": "愧疚", "relieved": "释然",
    "jealous": "嫉妒", "weary": "疲惫", "anxious": "忐忑",
    "affectionate": "温柔",
}


def classify(text: str, minimum_score: float = 1.0) -> str:
    """返回 18 类情绪之一；分不清时返回 neutral。"""
    t = text or ""
    if not t.strip():
        return "neutral"

    scores: Dict[str, float] = {emo: 0.0 for emo in EMOTIONS}
    for emo, pattern in EMOTION_KEYWORDS.items():
        hits = len(pattern.findall(t))
        if hits:
            scores[emo] += float(hits)

    # 标点加权
    if "！" in t or "!" in t:
        scores["excited"] += 0.5
        scores["angry"] += 0.3
    if "？" in t or "?" in t:
        scores["surprise"] += 0.2
    if t.count("……") >= 1 or t.count("...") >= 1:
        scores["helpless"] += 0.4

    best = max(scores, key=lambda k: scores[k])
    if scores[best] < minimum_score:
        return "neutral"
    return best


def prosody_for(emotion: str, *, strength: float = 1.0, base: Optional[dict] = None) -> dict:
    """把情绪换算成 Fish 的 prosody 参数。strength 控制情绪幅度（0~1）。"""
    profile = EMOTION_PROFILES.get(emotion) or EMOTION_PROFILES["neutral"]
    base = base or {}
    base_speed = float(base.get("speed", 1.0) or 1.0)
    base_volume = float(base.get("volume", 1.0) or 1.0)

    speed = 1.0 + (profile["speed"] - 1.0) * max(0.0, min(1.0, strength))
    volume = 1.0 + (profile["volume"] - 1.0) * max(0.0, min(1.0, strength))
    return {
        "speed": round(max(0.5, min(2.0, base_speed * speed)), 3),
        "volume": round(max(0.1, min(3.0, base_volume * volume)), 3),
    }


def tag_for(emotion: str) -> str:
    return EMOTION_TAGS_CN.get(emotion, "")


# 18 类情绪 → Fish 的 emotion 字段（实测会明显改变音色与能量）
FISH_EMOTION_MAP: Dict[str, str] = {
    "happy": "happy",
    "excited": "excited",
    "gratified": "happy",
    "relieved": "happy",
    "affectionate": "tender",
    "jealous": "happy",
    "sad": "sad",
    "melancholy": "sad",
    "weary": "sad",
    "wronged": "sad",
    "helpless": "sad",
    "guilty": "sad",
    "angry": "angry",
    "fear": "fear",
    "anxious": "fear",
    "surprise": "surprise",
    "neutral": "neutral",
    "cold": "neutral",
}


def fish_emotion_for(emotion: str) -> str:
    """把内部情绪名映射成 Fish 的 emotion 取值。"""
    return FISH_EMOTION_MAP.get(emotion, "neutral")


# 情绪 → 语气标点（只改标点，不改字面意思）
# 实测：感叹号/省略号能显著抬高音高与音高波动（215Hz→237~286Hz），这是最有效的手段
_EXCITED = {"excited", "angry", "surprise", "happy", "jealous"}
_SOFT = {"sad", "weary", "melancholy", "helpless", "wronged", "guilty", "anxious", "cold"}
_WARM = {"affectionate", "gratified", "relieved"}


def style_for_tts(text: str, emotion: str) -> str:
    """按情绪调整标点语气（只影响朗读，不影响显示的原文）。"""
    t = (text or "").strip()
    if not t:
        return t

    if emotion in _EXCITED:
        if not t.endswith(("！", "!", "？", "?", "！", "…", "~", "～")):
            t = t.rstrip("。.") + "！"
    elif emotion in _SOFT:
        # 省略号容易被念成拖长的怪声，统一用句号收尾
        if not t.endswith(("。", "……", "…", "~", "～", "？", "?")):
            t = t.rstrip("。.") + "。"
    elif emotion in _WARM:
        if not t.endswith(("～", "~", "…", "？", "?")):
            t = t.rstrip("。.") + "～"

    # 兜底：没有句末标点时补一个句号，模型才会自然收尾
    if not t.endswith(("。", "！", "？", "…", "~", "～", "，", ".", "!", "?")):
        t = t.rstrip() + "。"
    return t