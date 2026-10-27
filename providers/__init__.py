# -*- coding: utf-8 -*-
"""通用 TTS 后端包。

三种后端共用同一套"文本准备 + 发送"流程：
    fish   —— Fish Audio（/v1/tts，支持 reference_id 训练音色与零样本参考音频）
    mimo   —— 小米 MiMo（/v1/chat/completions，情绪用「（开心）」前缀）
    openai —— 任何 OpenAI 兼容的 /audio/speech 接口（自填 base_url + api_key）

每个后端只需实现：
    async def synthesize(text, *, audio_format=None, ...) -> Path
"""

from .fish import FishAudioClient, FishAudioError, sniff_format
from .mimo import MiMoClient, MiMoError, mimo_tag
from .openai import OpenAITTSClient, OpenAITTSError
from .text import (
    build_tag_whitelist,
    clean_for_speech,
    extract_emotion_tags,
    normalize_text,
    split_for_tts,
)

__all__ = [
    "FishAudioClient",
    "FishAudioError",
    "MiMoClient",
    "MiMoError",
    "OpenAITTSClient",
    "OpenAITTSError",
    "sniff_format",
    "mimo_tag",
    "build_tag_whitelist",
    "clean_for_speech",
    "extract_emotion_tags",
    "normalize_text",
    "split_for_tts",
]