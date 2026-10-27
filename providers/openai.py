# -*- coding: utf-8 -*-
"""OpenAI 兼容的 TTS 客户端（自填地址 + Key）。

适配大多数"OpenAI 格式"的语音接口：
    POST {base_url}/audio/speech
    Authorization: Bearer <api_key>
    body: {"model": ..., "input": ..., "voice": ..., "response_format": "mp3"}

也兼容几种常见的变体，靠配置开关：
    * 鉴权头：Bearer（默认）/ api-key / x-api-key
    * 音频返回：原始二进制（默认）/ JSON 里 base64（指定 json_field）
    * 额外请求体：custom_extra_json（例如 speed、emotion、style）

只依赖 aiohttp，便于离线单测。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from .fish import _pad_wav_tail, sniff_format

logger = logging.getLogger("astrbot")


class OpenAITTSError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpenAITTSClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "tts-1",
        voice: str = "alloy",
        audio_format: str = "mp3",
        auth_header: str = "bearer",
        json_field: str = "",
        extra_body: dict | None = None,
        timeout: int = 60,
        max_retries: int = 2,
        cache_dir: Path,
        cache_enabled: bool = True,
        tail_padding_ms: int = 250,
        proxy: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = model or "tts-1"
        self.voice = voice or ""
        self.audio_format = (audio_format or "mp3").lower()
        self.auth_header = (auth_header or "bearer").lower()
        self.json_field = (json_field or "").strip()
        self.extra_body = dict(extra_body or {})
        self.timeout = max(5, int(timeout or 60))
        self.max_retries = max(0, int(max_retries or 0))
        self.cache_dir = Path(cache_dir)
        self.cache_enabled = bool(cache_enabled)
        self.tail_padding_ms = max(0, int(tail_padding_ms or 0))
        self.proxy = (proxy or "").strip() or None
        self._session: aiohttp.ClientSession | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    # -- http ---------------------------------------------------------------
    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not self.api_key:
            return headers
        if self.auth_header == "api-key":
            headers["api-key"] = self.api_key
        elif self.auth_header == "x-api-key":
            headers["x-api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, text: str, fmt: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "response_format": fmt,
        }
        if self.voice:
            body["voice"] = self.voice
        body.update(self.extra_body)
        return body

    # -- cache --------------------------------------------------------------
    def _cache_path(self, key: str, fmt: str) -> Path:
        return self.cache_dir / f"{key}.{fmt}"

    @staticmethod
    def _valid(path: Path) -> bool:
        try:
            return path.exists() and path.stat().st_size > 1024
        except OSError:
            return False

    # -- core ---------------------------------------------------------------
    async def _post(self, text: str, fmt: str) -> bytes:
        if not self.base_url:
            raise OpenAITTSError("未配置 base_url")
        url = f"{self.base_url}/audio/speech"
        last = ""
        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                async with self._get_session().post(
                    url,
                    json=self._payload(text, fmt),
                    headers=self._headers(),
                    proxy=self.proxy,
                ) as resp:
                    if resp.status == 200:
                        ctype = (resp.headers.get("Content-Type") or "").lower()
                        if self.json_field or "json" in ctype:
                            data = await resp.json()
                            raw = data
                            for part in self.json_field.split(".") if self.json_field else []:
                                raw = (raw or {}).get(part) if isinstance(raw, dict) else None
                            if isinstance(raw, str):
                                return base64.b64decode(raw)
                            raise OpenAITTSError(
                                f"JSON 里没找到字段 {self.json_field or '（未配置）'}"
                            )
                        return await resp.read()
                    body = (await resp.text())[:200]
                    last = f"HTTP {resp.status}: {body}"
                    if resp.status in (408, 429) or 500 <= resp.status < 600:
                        if attempt < self.max_retries:
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2, 8.0)
                            continue
                    raise OpenAITTSError(last, status=resp.status)
            except OpenAITTSError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 8.0)
                    continue
                raise OpenAITTSError(f"网络异常：{last}") from exc
        raise OpenAITTSError(last or "未知错误")

    async def synthesize(
        self,
        text: str,
        *,
        audio_format: str | None = None,
        emotion_tag: str = "",
    ) -> Path:
        text = (text or "").strip()
        if not text:
            raise OpenAITTSError("待合成文本为空")

        fmt = (audio_format or self.audio_format or "mp3").strip().lower()
        key = hashlib.sha256(
            json.dumps(
                {
                    "t": text, "m": self.model, "v": self.voice,
                    "f": fmt, "x": self.extra_body, "e": emotion_tag,
                    "p": self.tail_padding_ms,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        path = self._cache_path(key, fmt)

        if self.cache_enabled and self._valid(path):
            logger.info("[TTS] 命中缓存：%s", path.name)
            return path

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self.cache_enabled and self._valid(path):
                return path

            data = await self._post(text, fmt)
            if len(data) < 512:
                raise OpenAITTSError(f"返回内容过短（{len(data)} 字节）")
            actual = sniff_format(data)
            if actual is None:
                snippet = data[:160].decode("utf-8", "replace")
                raise OpenAITTSError(f"返回的不是音频：{snippet}")
            real_fmt = actual
            if self.tail_padding_ms > 0 and real_fmt == "wav":
                data = _pad_wav_tail(data, self.tail_padding_ms)

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            if real_fmt != fmt:
                path = self._cache_path(key, real_fmt)
            tmp = self.cache_dir / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                await asyncio.to_thread(tmp.write_bytes, data)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)

            logger.info(
                "[TTS] 合成完成：%d 字节，%d 字，模型=%s，音色=%s",
                len(data), len(text), self.model, self.voice or "默认",
            )
            return path