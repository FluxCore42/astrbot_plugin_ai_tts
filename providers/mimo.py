# -*- coding: utf-8 -*-
"""MiMo TTS 后端（小米 mimo-v2.5-tts）。

与 Fish 不同：MiMo 走 /chat/completions，情绪用「（开心）」前缀，
音频以 base64 放在 choices[0].message.audio.data 里。

对外接口与 FishAudioClient 对齐，方便主流程无感切换：
    await client.synthesize(text, audio_format="wav", emotion_tag="开心")
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from .fish import _pad_wav_tail, sniff_format

logger = logging.getLogger("astrbot_plugin_fishaudio_tts")

# 我们的 18 类情绪 → MiMo 中文标签
MIMO_TAGS: dict[str, str] = {
    "happy": "开心", "sad": "悲伤", "angry": "愤怒", "fear": "恐惧",
    "surprise": "惊讶", "excited": "兴奋", "wronged": "委屈",
    "neutral": "平静", "cold": "冷漠", "melancholy": "怅然",
    "gratified": "欣慰", "helpless": "无奈", "guilty": "愧疚",
    "relieved": "释然", "jealous": "嫉妒", "weary": "厌倦",
    "anxious": "忐忑", "affectionate": "动情",
}

# 常用音色（mimo 官方）：
# 冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean / mimo_default
MIMO_VOICES = (
    "冰糖", "茉莉", "苏打", "白桦",
    "Mia", "Chloe", "Milo", "Dean", "mimo_default",
)


class MiMoError(Exception):
    """MiMo 合成失败。"""


def mimo_tag(emotion: str) -> str:
    return MIMO_TAGS.get(emotion, "")


class MiMoClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.xiaomimimo.com/v1",
        api_key: str = "",
        model: str = "mimo-v2.5-tts",
        voice: str = "冰糖",
        audio_format: str = "wav",
        timeout: int = 60,
        max_retries: int = 2,
        cache_dir: Path,
        cache_enabled: bool = True,
        tail_padding_ms: int = 250,
        proxy: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = model or "mimo-v2.5-tts"
        self.voice = voice or "冰糖"
        self.audio_format = (audio_format or "wav").lower()
        self.timeout = max(5, int(timeout or 60))
        self.max_retries = max(0, int(max_retries or 0))
        self.cache_dir = Path(cache_dir)
        self.cache_enabled = bool(cache_enabled)
        self.tail_padding_ms = max(0, int(tail_padding_ms or 0))
        self.proxy = (proxy or "").strip() or None
        self._session: aiohttp.ClientSession | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _cache_path(self, key: str, fmt: str) -> Path:
        return self.cache_dir / f"{key}.{fmt}"

    @staticmethod
    def _valid(path: Path) -> bool:
        try:
            return path.exists() and path.stat().st_size > 1024
        except OSError:
            return False

    def _resolve_voice(self) -> str:
        """音色可以是预设名，也可以是样本音频（本地路径或 data URI）。

        - 预设名（冰糖/茉莉…）原样传；
        - 本地文件或 data URI 视为克隆样本，转成 data:audio/...;base64,。
        """
        voice = (self.voice or "").strip()
        if not voice:
            return "冰糖"
        if voice.startswith("data:audio/"):
            return voice
        candidate = Path(voice)
        if not candidate.is_absolute():
            candidate = self.cache_dir.parent / voice
        if candidate.exists() and candidate.is_file():
            import base64 as _b64

            suffix = candidate.suffix.lower().lstrip(".") or "mp3"
            mime = "mpeg" if suffix in ("mp3", "mpeg") else suffix
            data = _b64.b64encode(candidate.read_bytes()).decode()
            return f"data:audio/{mime};base64,{data}"
        return voice

    def _payload(self, text: str, fmt: str, tag: str) -> dict[str, Any]:
        content = f"（{tag}）{text}" if tag else text
        return {
            "model": self.model,
            "messages": [{"role": "assistant", "content": content}],
            "audio": {"format": fmt, "voice": self._resolve_voice()},
        }

    async def _post(self, text: str, fmt: str, tag: str) -> bytes:
        if not self.api_key:
            raise MiMoError("未配置 MiMo API Key")
        url = f"{self.base_url}/chat/completions"
        headers = {"api-key": self.api_key, "Content-Type": "application/json"}
        last: str = ""
        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                async with self._get_session().post(
                    url,
                    json=self._payload(text, fmt, tag),
                    headers=headers,
                    proxy=self.proxy,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices") or []
                        message = (choices[0] if choices else {}).get("message", {})
                        audio_b64 = (message.get("audio") or {}).get("data")
                        if not audio_b64:
                            raise MiMoError("返回里没有音频数据")
                        raw = base64.b64decode(audio_b64)
                        if len(raw) < 1024:
                            raise MiMoError(f"音频过短（{len(raw)} 字节）")
                        return raw
                    body = (await resp.text())[:200]
                    last = f"HTTP {resp.status}: {body}"
                    if resp.status in (429,) or 500 <= resp.status < 600:
                        if attempt < self.max_retries:
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2, 8.0)
                            continue
                    raise MiMoError(last)
            except MiMoError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 8.0)
                    continue
                raise MiMoError(f"网络异常：{last}") from exc
        raise MiMoError(last or "未知错误")

    async def synthesize(
        self,
        text: str,
        *,
        audio_format: str | None = None,
        emotion_tag: str = "",
    ) -> Path:
        text = (text or "").strip()
        if not text:
            raise MiMoError("待合成文本为空")

        fmt = (audio_format or self.audio_format or "wav").strip().lower()
        key = hashlib.sha256(
            json.dumps(
                {
                    "t": text, "m": self.model,
                    "v": (self.voice or "")[:24],
                    "vh": hashlib.sha1((self.voice or "").encode()).hexdigest()[:8],
                    "e": emotion_tag, "f": fmt, "p": self.tail_padding_ms,
                },
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]
        path = self._cache_path(key, fmt)

        if self.cache_enabled and self._valid(path):
            logger.info("[MiMo] 命中缓存：%s", path.name)
            return path

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self.cache_enabled and self._valid(path):
                return path

            data = await self._post(text, fmt, emotion_tag)
            actual = sniff_format(data)
            if actual and actual != fmt:
                logger.warning("[MiMo] 请求 %s 实际返回 %s", fmt, actual)
            if self.tail_padding_ms > 0 and (actual or fmt) == "wav":
                data = _pad_wav_tail(data, self.tail_padding_ms)

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_dir / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                await asyncio.to_thread(tmp.write_bytes, data)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)

            logger.info(
                "[MiMo] 合成完成：%d 字节，%d 字，音色=%s，情绪=%s",
                len(data), len(text), self.voice, emotion_tag or "无",
            )
            return path