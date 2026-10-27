# -*- coding: utf-8 -*-
"""Fish Audio TTS HTTP 客户端。

只依赖 aiohttp（AstrBot 自带），不依赖 AstrBot 运行时，便于离线单测。

接口约定（与官网 /v1/tts 一致）：
    POST {base_url}/tts
    Authorization: Bearer <api_key>
    model: <model>                  # Fish 要求放在 header 里
    Content-Type: application/json  (也支持 msgpack，这里用 JSON 更易调试)
    body: {"text": ..., "reference_id": ..., "format": "mp3"}
    成功返回 audio/* 二进制
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import time
import uuid
import wave
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger("astrbot")

def _pad_wav_tail(data: bytes, padding_ms: int, fade_ms: int = 40) -> bytes:
    """给 WAV 末尾加淡出并补静音，同时重写正确的文件头。

    Fish 返回的 WAV 两个问题：
    1. data 长度字段不可靠，播放器会提前判定结束；
    2. 最后一个字常在高振幅处硬切，会产生"咔"声（听起来像蜂鸣）。
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    try:
        from array import array

        with wave.open(io.BytesIO(data), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())

        buf = io.BytesIO()
        with wave.open(buf, "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(width)
            writer.setframerate(rate)
            if width == 2:
                samples = array("h")
                samples.frombytes(frames)
                total = len(samples)
                fade_len = min(max(1, int(rate * fade_ms / 1000) * channels), total)
                for i in range(fade_len):
                    # 从尾部往前线性衰减
                    factor = 1.0 - (i / fade_len)
                    pos = total - fade_len + i
                    samples[pos] = int(samples[pos] * factor)
                pad_frames = int(rate * padding_ms / 1000)
                samples.extend([0] * (pad_frames * channels))
                writer.writeframes(samples.tobytes())
            else:
                writer.writeframes(frames)
                if padding_ms > 0:
                    pad_frames = int(rate * padding_ms / 1000)
                    writer.writeframes(
                        b"\x00" * (pad_frames * channels * width)
                    )
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return data


_MIN_AUDIO_BYTES = 512


class FishAudioError(Exception):
    """合成失败，status 为 HTTP 状态码（网络错误时为 None）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def sniff_format(data: bytes) -> str | None:
    """根据文件头判断音频格式。"""
    if not data:
        return None
    if data[:3] == b"ID3":
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"OggS":
        return "opus"
    return None


class FishAudioClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "s2.1-pro-free",
        reference_id: str = "",
        audio_format: str = "mp3",
        proxy: str = "",
        latency: str = "",
        tail_padding_ms: int = 0,
        timeout: int = 60,
        max_retries: int = 2,
        cache_dir: Path,
        cache_enabled: bool = True,
        cache_max_files: int = 300,
        cache_ttl_days: int = 7,
    ) -> None:
        self.base_url = (base_url or "https://api.fish.audio/v1").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "s2.1-pro-free").strip()
        self.reference_id = (reference_id or "").strip()
        self.audio_format = (audio_format or "mp3").strip().lower()
        self.proxy = (proxy or "").strip() or None
        self.latency = (latency or "").strip().lower()
        self.tail_padding_ms = max(0, int(tail_padding_ms or 0))
        self.timeout = max(5, int(timeout))
        self.max_retries = max(0, int(max_retries))
        self.cache_dir = Path(cache_dir)
        self.cache_enabled = bool(cache_enabled)
        self.cache_max_files = max(0, int(cache_max_files))
        self.cache_ttl_days = max(0, int(cache_ttl_days))

        self._session: aiohttp.ClientSession | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    # -- lifecycle ---------------------------------------------------------
    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def configure(self, **changes: Any) -> None:
        """热更新配置（插件配置项变化时调用）。"""
        for key, value in changes.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                trust_env=False,  # 代理只认显式配置，避免系统代理干扰
            )
        return self._session

    # -- cache -------------------------------------------------------------
    def _cache_path(self, key: str, fmt: str) -> Path:
        return self.cache_dir / f"{key}.{fmt}"

    @staticmethod
    def _cache_key(text: str, model: str, reference_id: str, fmt: str, endpoint: str, extra: str = "") -> str:
        raw = json.dumps(
            {"t": text, "m": model, "r": reference_id, "f": fmt, "e": endpoint, "x": extra},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _valid_cached(self, path: Path) -> bool:
        try:
            if not path.exists() or path.stat().st_size < _MIN_AUDIO_BYTES:
                return False
            with path.open("rb") as fh:
                head = fh.read(16)
            return sniff_format(head) == self.audio_format
        except OSError:
            return False

    def _cleanup_cache(self) -> None:
        """按 TTL 与数量上限清理缓存，失败不影响主流程。"""
        try:
            files = [p for p in self.cache_dir.glob("*") if p.is_file()]
            if not files:
                return
            now = time.time()
            if self.cache_ttl_days > 0:
                ttl = self.cache_ttl_days * 86400
                for p in files:
                    try:
                        if now - p.stat().st_mtime > ttl:
                            p.unlink(missing_ok=True)
                    except OSError:
                        continue
            files = [p for p in self.cache_dir.glob("*") if p.is_file()]
            if self.cache_max_files and len(files) > self.cache_max_files:
                files.sort(key=lambda p: p.stat().st_mtime)
                for p in files[: len(files) - self.cache_max_files]:
                    p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - 清理失败不应影响合成
            logger.debug("[FishAudio] 缓存清理跳过", exc_info=True)

    # -- request -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/*",
            "model": self.model,
        }

    def _payload(
        self,
        text: str,
        fmt: str,
        prosody: dict | None = None,
        emotion: str = "",
        reference_audio: bytes | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "format": fmt}
        if reference_audio:
            # 零样本音色：直接传参考音频（data URI），不需要建库额度
            import base64 as _b64

            body["reference_audio"] = (
                "data:audio/mpeg;base64," + _b64.b64encode(reference_audio).decode()
            )
        elif self.reference_id:
            body["reference_id"] = self.reference_id
        if prosody:
            # Fish 支持 prosody.speed / prosody.volume，实测生效
            body["prosody"] = dict(prosody)
        if emotion:
            # Fish 的 emotion 字段会明显改变音色与能量（实测 happy +30%/sad -30% 音量）
            body["emotion"] = emotion
        if self.latency in ("normal", "balanced"):
            # balanced：Fish 会把首字节压到 ~300ms，整体抖动更小
            body["latency"] = self.latency
        return body

    async def _post(
        self,
        text: str,
        fmt: str,
        prosody: dict | None = None,
        emotion: str = "",
        reference_audio: bytes | None = None,
    ) -> bytes:
        url = f"{self.base_url}/tts"
        last_error: FishAudioError | None = None
        backoff = 1.0

        for attempt in range(self.max_retries + 1):
            try:
                async with self._get_session().post(
                    url,
                    json=self._payload(text, fmt, prosody, emotion, reference_audio),
                    headers=self._headers(),
                    proxy=self.proxy,
                ) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get("Content-Type", "")
                        data = await resp.read()
                        if len(data) < _MIN_AUDIO_BYTES:
                            raise FishAudioError(
                                f"返回内容过短（{len(data)} 字节），可能是空音频",
                                status=200,
                            )
                        if "audio" not in content_type.lower() and sniff_format(data) is None:
                            snippet = data[:300].decode("utf-8", "replace")
                            raise FishAudioError(
                                f"返回的不是音频（Content-Type={content_type}）：{snippet}",
                                status=200,
                            )
                        return data

                    body = (await resp.text())[:400]
                    last_error = FishAudioError(
                        f"HTTP {resp.status}：{body}", status=resp.status
                    )
                    retryable = resp.status in (408, 429) or 500 <= resp.status < 600
                    if retryable and attempt < self.max_retries:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 8.0)
                        continue
                    raise last_error
            except FishAudioError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = FishAudioError(
                    f"网络异常（{type(exc).__name__}）：{exc}；"
                    f"若接口被墙/被污染，请在配置里填写 HTTP 代理",
                    status=None,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 8.0)
                    continue
                raise last_error from exc

        raise last_error or FishAudioError("未知错误")

    # -- public ------------------------------------------------------------
    async def synthesize(
        self,
        text: str,
        *,
        cache: bool | None = None,
        audio_format: str | None = None,
        prosody: dict | None = None,
        emotion: str = "",
        reference_audio: bytes | None = None,
    ) -> Path:
        """把一段文本合成为音频文件，返回本地路径。

        Args:
            text: 已清洗、已带情绪标签的文本。
            cache: 覆盖全局缓存开关（None 表示用配置）。
            audio_format: 覆盖本次请求格式（None 表示用配置）。
                qq_official 需要 WAV——官方适配器会用 pysilk 直接转 SILK，
                走 WAV 可以完全不依赖 ffmpeg。

        Raises:
            FishAudioError: 参数缺失、网络失败、接口报错或返回内容异常。
        """
        text = (text or "").strip()
        if not text:
            raise FishAudioError("待合成文本为空")
        if not self.api_key:
            raise FishAudioError("未配置 Fish Audio API Key")

        fmt = (audio_format or self.audio_format or "mp3").strip().lower()
        use_cache = self.cache_enabled if cache is None else bool(cache)
        import hashlib as _hash

        extra = json.dumps(
            {
                "p": prosody or {},
                "e": emotion,
                "v": _hash.sha1(reference_audio).hexdigest()[:12] if reference_audio else "",
                "t": self.tail_padding_ms,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        key = self._cache_key(text, self.model, self.reference_id, fmt, self.base_url, extra)
        path = self._cache_path(key, fmt)

        if use_cache and self._valid_cached(path):
            try:
                os.utime(path, None)
            except OSError:
                pass
            return path

        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock

        async with lock:
            # 双击/并发时可能在等锁期间已经生成好
            if use_cache and self._valid_cached(path):
                return path

            data = await self._post(text, fmt)
            actual = sniff_format(data)
            if actual is None:
                raise FishAudioError("返回的音频头无法识别（不是 mp3/wav/opus）")
            if actual != fmt:
                logger.warning(
                    "[FishAudio] 请求格式 %s 实际返回 %s，按请求格式落盘",
                    fmt,
                    actual,
                )
            # WAV 一律重写：Fish 返回的 data 长度字段不可靠，会切掉结尾；
            # 同时按配置补一段尾音静音。
            if sniff_format(data) == "wav":
                data = _pad_wav_tail(data, self.tail_padding_ms)

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_dir / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                await asyncio.to_thread(tmp.write_bytes, data)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)

            logger.info(
                "[FishAudio] 合成完成：%d 字节，%d 字，模型=%s，缓存=%s",
                len(data),
                len(text),
                self.model,
                "on" if use_cache else "off",
            )
            if use_cache:
                self._cleanup_cache()
            return path
