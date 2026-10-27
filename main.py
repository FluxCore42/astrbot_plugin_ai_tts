# -*- coding: utf-8 -*-
"""AI 语音 —— 通用 TTS 插件，让 AstrBot 在 QQ 里"说话"。

设计要点：
* 只发一条：把 LLM 回复的 Plain 文本整体换成 Record，避免"文字 + 语音"刷屏。
* 后端可插拔：Fish Audio / 小米 MiMo / 任意 OpenAI 兼容接口（自填地址与 Key），
  共用同一套文本准备、情绪识别、尾音修复与缓存逻辑。
* 音频修复：Fish 返回的 WAV 头长度字段不可靠，会切掉结尾；
  这里统一重写 WAV 头，并做 40ms 淡出 + 尾部静音，消除"咔"声与急促收尾。
* 情绪：18 类中文情绪关键词分类，映射成 emotion/prosody（Fish）或
  「（开心）」前缀（MiMo），外加标点语气调整，保证音色不变的前提下有起伏。
* 优先级 -2000，晚于其它 TTS 插件；链里已有 Record 就跳过，避免重复发语音。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from .providers import FishAudioClient, FishAudioError, OpenAITTSClient, OpenAITTSError
from .reply_style import clean_reply_style, strip_for_speech
from .providers.mimo import MiMoClient, mimo_tag
from .emotion import (
    classify as classify_emotion,
    fish_emotion_for,
    prosody_for,
    style_for_tts,
    tag_for,
)
from .providers.text import (
    build_tag_whitelist,
    clean_for_speech,
    extract_emotion_tags,
    normalize_text,
    split_for_tts,
)

logger = logging.getLogger("astrbot")

DEFAULT_API = {
    "base_url": "https://api.fish.audio/v1",
    "api_key": "",
    "model": "s2.1-pro-free",
    "reference_id": "",
    "proxy": "",
    "timeout": 60,
    "max_retries": 2,
    "format": "mp3",
    "latency": "balanced",
    "reference_files": [],
    "reference_index": 0,
    "provider": "fish",
    "mimo_base_url": "https://api.xiaomimimo.com/v1",
    "mimo_key": "",
    "mimo_model": "mimo-v2.5-tts",
    "mimo_voice": "冰糖",
    "mimo_emotion": True,
}

DEFAULT_CUSTOM = {
    "custom_base_url": "",
    "custom_api_key": "",
    "custom_model": "tts-1",
    "custom_voice": "",
    "custom_auth_header": "bearer",
    "custom_json_field": "",
    "custom_extra_json": "",
}

DEFAULT_OUTPUT = {
    "auto_speak": True,
    # 语音回复不再附带同款文字：一条回复只发一种，才不会“文字+语音”两条
    "send_text": False,
    "platforms": ["aiocqhttp", "qq_official", "qq_official_webhook"],
    # 一半语音、一半文字，更像真人聊天
    "probability": 0.5,
    "min_chars": 4,
    "max_chars": 40,
    "max_segments": 1,
    "cooldown_seconds": 2,
    "max_concurrency": 2,
    "skip_if_record_exists": True,
    "qq_official_wav": True,
    "tail_padding_ms": 250,
    "speed": 0.93,
}

# 这些平台会用 pysilk 把音频转成腾讯 SILK，直接要 WAV 可以绕开对 ffmpeg 的依赖。
SILK_PLATFORMS = {
    "qq_official",
    "qq_official_webhook",
    "wecom",
    "wecom_ai_bot",
    "weixin_oc",
}

DEFAULT_EMOTION = {
    "enabled": True,
    "default_tag": "",
    "extra_tags": [],
    "auto_detect": True,
    "prosody": True,
    "fish_emotion": True,
    "strength": 0.6,
    "tag_prefix": False,
}

DEFAULT_CACHE = {
    "enabled": True,
    "max_files": 300,
    "ttl_days": 7,
}

DEFAULT_SESSION = {
    "default_enabled": True,
    "whitelist": [],
    "blacklist": [],
}

DEFAULT_REPLY_STYLE = {
    "enabled": True,
    "max_chars": 80,
    "strip_actions": True,
}

DEFAULT_MEME = {
    "enabled": True,
    "probability": 20,
    "max_size_mb": 4,
    # 语音回复不再配图，避免 QQ 把图和语音拆成两条
    "skip_when_voice": True,
}

# 回复/用户文本里的关键词 -> meme_manager 的分类目录名
MEME_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("happy", ("开心", "高兴", "哈哈", "笑", "好耶", "棒", "赢", "喜欢", "嘿嘿")),
    ("sad", ("难过", "伤心", "哭", "委屈", "失落", "累", "疲惫", "呜呜")),
    ("angry", ("生气", "愤怒", "讨厌", "气死", "烦", "哼")),
    ("sleep", ("睡觉", "困", "晚安", "摸鱼", "躺平", "休息")),
    ("food", ("吃饭", "好吃", "饿", "宵夜", "奶茶", "喝")),
    ("confused", ("？", "?", "不懂", "疑惑", "为什么", "为啥")),
    ("greeting", ("你好", "早安", "早上好", "在吗", "嗨")),
    ("work", ("工作", "加班", "代码", "上班", "学习", "考试", "作业")),
    ("money", ("工资", "穷", "付费", "氪金", "没钱")),
    ("teasing", ("吐槽", "调侃", "傻", "笨", "离谱")),
    ("affection", ("抱抱", "贴贴", "亲亲", "想你", "陪你")),
)


def _merge(base: dict, override: Any) -> dict:
    """浅合并：override 里的 None 视为未设置。"""
    out = dict(base)
    if isinstance(override, dict):
        for key, value in override.items():
            if value is not None:
                out[key] = value
    return out


class AITTSPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.cfg: dict = config if isinstance(config, dict) else {}

        self.data_dir = self._resolve_data_dir()
        self.audio_dir = self.data_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "sessions.json"

        self._state: dict[str, dict] = self._load_state()
        self._voice_cache: dict[str, tuple[float, bytes]] = {}
        self._client: FishAudioClient | None = None
        self._client_sig: tuple | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_error: str = ""
        self._last_latency: float = 0.0

    # ------------------------------------------------------------------ init
    def _resolve_data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir())
        except Exception:
            logger.debug("[FishAudio] StarTools 不可用，回退到插件目录", exc_info=True)
            return Path(__file__).resolve().parent / "data"

    def _load_state(self) -> dict[str, dict]:
        try:
            if self.state_path.exists():
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            logger.warning("[FishAudio] 会话状态读取失败，使用默认值", exc_info=True)
        return {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception:
            logger.warning("[FishAudio] 会话状态保存失败", exc_info=True)

    # ------------------------------------------------------------------ cfg
    def _api(self) -> dict:
        return _merge(DEFAULT_API, self.cfg.get("api"))

    def _custom_cfg(self) -> dict:
        return _merge(DEFAULT_CUSTOM, self.cfg.get("custom"))

    def _output(self) -> dict:
        return _merge(DEFAULT_OUTPUT, self.cfg.get("output"))

    def _emotion(self) -> dict:
        return _merge(DEFAULT_EMOTION, self.cfg.get("emotion"))

    def _cache(self) -> dict:
        return _merge(DEFAULT_CACHE, self.cfg.get("cache"))

    def _session(self) -> dict:
        return _merge(DEFAULT_SESSION, self.cfg.get("session"))

    def _meme(self) -> dict:
        return _merge(DEFAULT_MEME, self.cfg.get("meme"))

    def _reply_style(self) -> dict:
        return _merge(DEFAULT_REPLY_STYLE, self.cfg.get("reply_style"))

    # --------------------------------------------------------------- client
    def _ensure_client(self) -> FishAudioClient:
        api = self._api()
        cache = self._cache()
        sig = (
            api["base_url"], api["api_key"], api["model"], api["reference_id"],
            api["format"], api["proxy"], api["latency"], api["timeout"], api["max_retries"],
            cache["enabled"], cache["max_files"], cache["ttl_days"],
            int(self._output().get("tail_padding_ms", 250) or 0),
        )
        if self._client is not None and self._client_sig == sig:
            return self._client

        old = self._client
        self._client = FishAudioClient(
            base_url=str(api["base_url"]),
            api_key=str(api["api_key"]),
            model=str(api["model"]),
            reference_id=str(api["reference_id"]),
            audio_format=str(api["format"]),
            proxy=str(api["proxy"]),
            latency=str(api.get("latency", "balanced")),
            tail_padding_ms=int(self._output().get("tail_padding_ms", 250) or 0),
            timeout=int(api["timeout"]),
            max_retries=int(api["max_retries"]),
            cache_dir=self.audio_dir,
            cache_enabled=bool(cache["enabled"]),
            cache_max_files=int(cache["max_files"]),
            cache_ttl_days=int(cache["ttl_days"]),
        )
        self._client_sig = sig
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.close())
            except RuntimeError:
                pass
        return self._client

    def _voice_files(self) -> list[Path]:
        """配置里登记的参考音频（相对路径按插件数据目录解析）。"""
        out: list[Path] = []
        for item in self._api().get("reference_files") or []:
            text = str(item or "").strip()
            if not text:
                continue
            path = Path(text)
            if not path.is_absolute():
                path = self.data_dir / text
            if path.exists():
                out.append(path)
        return out

    def _voice_bytes(self, umo: str) -> bytes | None:
        """取当前会话选中的参考音频内容（带 mtime 缓存）。"""
        files = self._voice_files()
        if not files:
            return None
        default_idx = int(self._api().get("reference_index", 0) or 0)
        idx = int(self._state.get(umo, {}).get("voice", default_idx) or 0)
        if idx < 0 or idx >= len(files):
            idx = 0
        path = files[idx]
        try:
            mtime = path.stat().st_mtime
            cached = self._voice_cache.get(str(path))
            if cached and cached[0] == mtime:
                return cached[1]
            data = path.read_bytes()
            self._voice_cache[str(path)] = (mtime, data)
            return data
        except Exception:  # noqa: BLE001
            logger.warning("[FishAudio] 参考音频读取失败：%s", path)
            return None

    def _voice_name(self, umo: str) -> str:
        files = self._voice_files()
        if not files:
            ref = str(self._api().get("reference_id", "") or "")
            return f"内置音色:{ref[:8]}" if ref else "内置音色:未设置"
        default_idx = int(self._api().get("reference_index", 0) or 0)
        idx = int(self._state.get(umo, {}).get("voice", default_idx) or 0)
        if idx < 0 or idx >= len(files):
            idx = 0
        return files[idx].stem

    def _ensure_custom(self) -> OpenAITTSClient:
        api = self._custom_cfg()
        base = self._api()
        out = self._output()
        extra = {}
        raw_extra = str(api.get("custom_extra_json", "") or "").strip()
        if raw_extra:
            try:
                extra = json.loads(raw_extra)
                if not isinstance(extra, dict):
                    extra = {}
            except Exception:  # noqa: BLE001
                logger.warning("[TTS] custom_extra_json 不是合法 JSON，已忽略")
        sig = (
            api["custom_base_url"], api["custom_api_key"], api["custom_model"],
            api["custom_voice"], api["custom_auth_header"], api["custom_json_field"],
            raw_extra, base["format"], base["proxy"], base["timeout"], base["max_retries"],
            int(out.get("tail_padding_ms", 250) or 0),
            bool(self._cache().get("enabled", True)),
        )
        if self._custom is not None and self._custom_sig == sig:
            return self._custom
        old = self._custom
        self._custom = OpenAITTSClient(
            base_url=str(api["custom_base_url"]),
            api_key=str(api["custom_api_key"]),
            model=str(api["custom_model"]),
            voice=str(api["custom_voice"]),
            audio_format=str(base["format"]),
            auth_header=str(api["custom_auth_header"]),
            json_field=str(api["custom_json_field"]),
            extra_body=extra,
            timeout=int(base["timeout"]),
            max_retries=int(base["max_retries"]),
            cache_dir=self.audio_dir,
            cache_enabled=bool(self._cache().get("enabled", True)),
            tail_padding_ms=int(out.get("tail_padding_ms", 250) or 0),
            proxy=str(base["proxy"]),
        )
        self._custom_sig = sig
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.close())
            except RuntimeError:
                pass
        return self._custom

    def _ensure_mimo(self) -> MiMoClient:
        api = self._api()
        out = self._output()
        sig = (
            api["mimo_base_url"], api["mimo_key"], api["mimo_model"],
            api["mimo_voice"], base["format"], base["proxy"], base["timeout"],
            base["max_retries"], int(out.get("tail_padding_ms", 250) or 0),
            bool(self._cache().get("enabled", True)),
        )
        if self._mimo is not None and self._mimo_sig == sig:
            return self._mimo
        old = self._mimo
        self._mimo = MiMoClient(
            base_url=str(api["mimo_base_url"]),
            api_key=str(api["mimo_key"]),
            model=str(api["mimo_model"]),
            voice=str(api["mimo_voice"]),
            audio_format=str(base["format"]),
            timeout=int(base["timeout"]),
            max_retries=int(base["max_retries"]),
            cache_dir=self.audio_dir,
            cache_enabled=bool(self._cache().get("enabled", True)),
            tail_padding_ms=int(out.get("tail_padding_ms", 250) or 0),
            proxy=str(base["proxy"]),
        )
        self._mimo_sig = sig
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.close())
            except RuntimeError:
                pass
        return self._mimo

    def _sema(self) -> asyncio.Semaphore:
        limit = max(1, int(self._output()["max_concurrency"]))
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(limit)
        return self._semaphore

    # -------------------------------------------------------------- session
    def _umo(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.unified_msg_origin)
        except Exception:
            return "unknown"

    @staticmethod
    def _match_any(umo: str, patterns: list) -> bool:
        for item in patterns or []:
            pat = str(item or "").strip()
            if not pat:
                continue
            if pat == umo or umo.startswith(pat):
                return True
            if pat.startswith("*") and umo.endswith(pat.lstrip("*")):
                return True
        return False

    def _session_enabled(self, event: AstrMessageEvent) -> bool:
        umo = self._umo(event)
        st = self._state.get(umo)
        if isinstance(st, dict) and isinstance(st.get("enabled"), bool):
            return st["enabled"]

        cfg = self._session()
        if self._match_any(umo, cfg.get("blacklist", [])):
            return False
        whitelist = cfg.get("whitelist", []) or []
        if whitelist:
            return self._match_any(umo, whitelist)
        return bool(cfg.get("default_enabled", True))

    def _set_session_enabled(self, event: AstrMessageEvent, enabled: bool) -> None:
        umo = self._umo(event)
        self._state.setdefault(umo, {})["enabled"] = bool(enabled)
        self._save_state()

    def _platform(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name())
        except Exception:
            return ""

    def _platform_allowed(self, event: AstrMessageEvent) -> bool:
        platforms = self._output().get("platforms", []) or []
        if not platforms:
            return True
        return self._platform(event) in {str(p) for p in platforms}

    def _effective_format(self, platform: str) -> str:
        """按平台决定请求格式。

        qq_official 的语音段会被 MediaResolver 转成腾讯 SILK：
        SILK 编码用 pysilk（纯 Python），但 mp3->wav 要调 ffmpeg。
        直接向 Fish Audio 要 WAV，就完全不需要 ffmpeg。
        """
        fmt = str(self._api().get("format") or "mp3").strip().lower()
        if platform in SILK_PLATFORMS and bool(self._output().get("qq_official_wav", True)):
            return "wav"
        return fmt

    # ------------------------------------------------------------- synthesis
    def _tag_whitelist(self) -> set[str]:
        return build_tag_whitelist(list(self._emotion().get("extra_tags", []) or []))

    def _prepare(self, raw_text: str) -> tuple[str, str]:
        """返回 (显示文本, 送 TTS 的文本)。"""
        text = normalize_text(raw_text)
        if not text:
            return "", ""

        emo = self._emotion()
        if bool(emo.get("enabled", True)):
            display, tags = extract_emotion_tags(text, self._tag_whitelist())
            display = display.strip() or text
            default_tag = str(emo.get("default_tag", "") or "").strip().strip("[]")
            # 语音单独强清理：括号里的动作/旁白一律不念
            speech = strip_for_speech(clean_for_speech(display))
            if not speech:
                speech = clean_for_speech(display)
            if not tags and default_tag:
                speech = f"[{default_tag}] {speech}"
            return display, speech

        return text, strip_for_speech(clean_for_speech(text)) or clean_for_speech(text)

    async def _synth_text(
        self,
        raw_text: str,
        *,
        ignore_limits: bool = False,
        platform: str = "",
        umo: str = "",
    ) -> list[Path]:
        out = self._output()
        display, speech = self._prepare(raw_text)
        if not speech:
            return []

        if not ignore_limits:
            if len(speech) < int(out["min_chars"]):
                return []
        chunks = split_for_tts(
            speech, int(out["max_chars"]), int(out["max_segments"])
        )
        if not chunks:
            return []

        # 情绪：按回复内容识别 18 类情绪，换算成 Fish 的语速/音量
        emotion = ""
        fish_emotion = ""
        prosody: dict | None = None
        emo_cfg = self._emotion()
        if bool(emo_cfg.get("auto_detect", True)):
            emotion = classify_emotion(display)
            if bool(emo_cfg.get("fish_emotion", True)):
                fish_emotion = fish_emotion_for(emotion)
            if bool(emo_cfg.get("text_style", True)):
                styled = style_for_tts(speech, emotion)
                chunks = split_for_tts(
                    styled, int(out["max_chars"]), int(out["max_segments"])
                )
            if bool(emo_cfg.get("prosody", True)):
                prosody = prosody_for(
                    emotion, strength=float(emo_cfg.get("strength", 0.8) or 0.8)
                )
            if bool(emo_cfg.get("tag_prefix", False)):
                tag = tag_for(emotion)
                if tag and chunks and not chunks[0].lstrip().startswith("["):
                    chunks[0] = f"[{tag}] {chunks[0]}"
            # 固定语速：不受情绪影响，保证音色与节奏稳定
            speed_cfg = float(out.get("speed", 1.0) or 1.0)
            if abs(speed_cfg - 1.0) > 0.01:
                prosody = dict(prosody or {})
                prosody["speed"] = round(
                    speed_cfg * float((prosody or {}).get("speed", 1.0)), 3
                )

            logger.info(
                "[FishAudio] 情绪=%s fish=%s 语速=%s prosody=%s",
                emotion,
                fish_emotion,
                speed_cfg,
                prosody,
            )

        client = self._ensure_client()
        fmt = self._effective_format(platform)
        ref_audio = self._voice_bytes(umo)
        logger.info("[FishAudio] 朗读文本=%s", speech)
        logger.info(
            "[FishAudio] 音色=%s 参考音频=%s字节",
            self._voice_name(umo),
            len(ref_audio) if ref_audio else 0,
        )
        paths: list[Path] = []
        started = time.monotonic()
        async with self._sema():
            provider = str(
                self._state.get(umo, {}).get("provider")
                or self._api().get("provider", "fish")
            ).lower()
            use_mimo = provider == "mimo"
            use_custom = provider in ("custom", "openai")
            mimo = self._ensure_mimo() if use_mimo else None
            custom = self._ensure_custom() if use_custom else None
            mimo_tag_text = (
                mimo_tag(emotion)
                if use_mimo and bool(emo_cfg.get("mimo_emotion", True))
                else ""
            )
            for chunk in chunks:
                try:
                    if mimo is not None:
                        paths.append(
                            await mimo.synthesize(
                                chunk,
                                audio_format=fmt,
                                emotion_tag=mimo_tag_text,
                            )
                        )
                    elif custom is not None:
                        paths.append(
                            await custom.synthesize(
                                chunk,
                                audio_format=fmt,
                                emotion_tag=mimo_tag_text,
                            )
                        )
                    else:
                        paths.append(
                            await client.synthesize(
                                chunk,
                                audio_format=fmt,
                                prosody=prosody,
                                emotion=fish_emotion,
                                reference_audio=ref_audio,
                            )
                        )
                except FishAudioError as exc:
                    self._last_error = str(exc)
                    logger.error("[FishAudio] 合成失败：%s", exc)
                    return []
                except Exception as exc:  # noqa: BLE001 - 不让插件异常冒到消息管线
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.error("[FishAudio] 合成异常", exc_info=True)
                    return []
        self._last_latency = time.monotonic() - started
        self._last_error = ""
        return paths

    def _reply_paths(self, paths: list[Path], display_text: str, send_text: bool) -> list:
        chain: list = []
        if send_text and display_text.strip():
            chain.append(Plain(text=display_text))
        chain.extend(Record(file=str(p)) for p in paths)
        return chain

    # ---------------------------------------------------------------- hooks
    @filter.on_decorating_result(priority=-2000)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        try:
            _dbg = event.get_result()
            logger.debug(
                "[FishAudio] hook 触发：platform=%s chain=%s llm_result=%s",
                self._platform(event),
                [type(c).__name__ for c in (_dbg.chain if _dbg else [])],
                (_dbg.is_llm_result() if _dbg else None),
            )
            if not self._platform_allowed(event):
                return
            if not self._session_enabled(event):
                return

            out = self._output()
            if not bool(out.get("auto_speak", True)):
                return

            result = event.get_result()
            if result is None or not result.chain:
                return
            try:
                if not result.is_llm_result():
                    return
            except Exception:
                return

            if bool(out.get("skip_if_record_exists", True)) and any(
                isinstance(comp, Record) for comp in result.chain
            ):
                logger.debug("[FishAudio] 链中已有语音段，跳过（其它 TTS 插件已处理）")
                return

            text = " ".join(
                comp.text for comp in result.chain
                if isinstance(comp, Plain) and getattr(comp, "text", None)
            ).strip()
            if not text:
                return

            style = self._reply_style()
            if bool(style.get("enabled", True)):
                cleaned = clean_reply_style(
                    text,
                    max_chars=int(style.get("max_chars", 80) or 0),
                    strip_actions=bool(style.get("strip_actions", True)),
                )
                if cleaned:
                    text = cleaned

            display, speech = self._prepare(text)
            if not display and not speech:
                return

            # 一条回复只发一种：语音，或文字（可带一张图）。不叠加，避免刷屏。
            user_text = str(getattr(event, "message_str", "") or "")
            asked_meme = any(k in user_text for k in ("表情", "斗图", "配图", "发张图"))
            asked_voice = any(
                k in user_text for k in ("语音", "发条语音", "说句话", "念一下", "读一下", "听听声音")
            )

            voice_prob = float(out.get("probability", 0.5) or 0.0)
            want_voice = voice_prob >= 1.0 or random.random() <= max(0.0, min(1.0, voice_prob))
            # 字数限制只约束语音：太短的没必要念，太长的念不完
            if want_voice and len(speech or "") < int(out["min_chars"]):
                want_voice = False
            if asked_voice and not asked_meme:
                # 用户点名要语音时强制走合成，不再抽概率
                want_voice = True
            if asked_meme:
                # 用户点名要表情包时，这条优先发图，别被语音顶掉
                want_voice = False
            max_chars = int(out["max_chars"])
            max_segments = int(out["max_segments"])
            if asked_voice and not asked_meme:
                # 用户点名要语音时放宽上限，尽量把整段念完
                max_chars = max(max_chars, 120)
            if want_voice and len(speech) > max_chars * max_segments:
                # 太长不念：语音一条念不完，宁可发文字，别把内容截断
                want_voice = False

            umo = self._umo(event)
            lock = self._locks.setdefault(umo, asyncio.Lock())
            if lock.locked():
                logger.debug("[FishAudio] 本会话上一次合成未结束，跳过")
                return

            cooldown = int(out.get("cooldown_seconds", 0) or 0)
            last = float(self._state.get(umo, {}).get("last_speak", 0) or 0)
            if want_voice and cooldown > 0 and time.time() - last < cooldown:
                want_voice = False

            async with lock:
                paths = (
                    await self._synth_text(
                        text, platform=self._platform(event), umo=umo
                    )
                    if want_voice
                    else []
                )
                # 语音回复默认不再附图，否则 QQ 会把图+语音拆成两条消息
                skip_meme = bool(paths) and bool(self._meme().get("skip_when_voice", True))
                meme = None if skip_meme else self._maybe_meme(event, text)
                if not paths and meme is None:
                    return
                if paths:
                    self._state.setdefault(umo, {})["last_speak"] = time.time()

                keep = [
                    comp for comp in result.chain
                    if not isinstance(comp, (Plain, Image))
                ]
                new_chain: list = []
                if display and (not paths or bool(out.get("send_text", False))):
                    new_chain.append(Plain(text=display))
                new_chain.extend(keep)
                if meme is not None:
                    new_chain.append(Image.fromFileSystem(str(meme)))
                    logger.info("[FishAudio] 附带表情包：%s", meme.name)
                new_chain.extend(Record(file=str(p)) for p in paths)
                result.chain = new_chain
                logger.info(
                    "[FishAudio] 最终消息链=%s",
                    [type(comp).__name__ for comp in result.chain],
                )
                if paths:
                    logger.info(
                        "[FishAudio] 已插入 %d 段语音（%d 字，%.2fs）%s",
                        len(paths), len(speech), self._last_latency,
                        "，纯语音无文字" if not out.get("send_text", False) else "",
                    )
        except Exception:  # noqa: BLE001 - 绝不能因为语音失败而吞掉回复
            logger.error("[FishAudio] on_decorating_result 异常", exc_info=True)

    # ------------------------------------------------------------- commands
    def _guess_meme_category(self, text: str) -> str:
        for category, words in MEME_KEYWORDS:
            if any(w in text for w in words):
                return category
        return ""

    def _maybe_meme(self, event: AstrMessageEvent, reply_text: str) -> Path | None:
        """确定性配图：不依赖模型输出分类标记。"""
        cfg = self._meme()
        if not bool(cfg.get("enabled", True)):
            return None

        result = event.get_result()
        if result is not None and any(isinstance(c, Image) for c in result.chain):
            return None  # meme_manager 已经配过图

        user_text = str(getattr(event, "message_str", "") or "")
        asked = any(k in user_text for k in ("表情", "斗图", "配图", "发张图"))
        if not asked:
            prob = int(cfg.get("probability", 30) or 0)
            if prob <= 0 or random.randint(1, 100) > prob:
                return None

        max_bytes = int(float(cfg.get("max_size_mb", 4)) * 1024 * 1024)
        files = self._collect_meme_files(
            self._guess_meme_category(f"{reply_text} {user_text}")
        )
        if not files:
            files = self._collect_meme_files("")
        if not files:
            logger.info("[FishAudio] 没找到表情包文件（目录 %s）", self._meme_packs_dir())
            return None
        small = []
        for p in files:
            try:
                if p.stat().st_size <= max_bytes:
                    small.append(p)
            except OSError:
                continue
        return random.choice(small or files)

    def _meme_packs_dir(self) -> Path:
        """表情包插件（meme_manager）的资源目录，和本插件同属 data/plugin_data。"""
        return self.data_dir.parent / "meme_manager" / "packs"

    def _collect_meme_files(self, category: str = "") -> list[Path]:
        root = self._meme_packs_dir()
        if not root.is_dir():
            return []
        exts = {".gif", ".png", ".jpg", ".jpeg", ".webp"}
        files: list[Path] = []
        category = (category or "").strip()
        for pack in root.iterdir():
            memes = pack / "memes"
            if not memes.is_dir():
                continue
            if category:
                dirs = [memes / category] if (memes / category).is_dir() else []
            else:
                dirs = [d for d in memes.iterdir() if d.is_dir()]
            for folder in dirs:
                try:
                    files.extend(
                        p for p in folder.iterdir()
                        if p.is_file() and p.suffix.lower() in exts
                    )
                except OSError:
                    continue
        return files

    @filter.command("发表情", alias={"meme", "表情包"})
    async def send_meme(self, event: AstrMessageEvent):
        """不依赖模型，直接随机发一张表情包。用法：/发表情 [分类]"""
        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=1)
        category = parts[1].strip() if len(parts) > 1 else ""

        files = self._collect_meme_files(category)
        if not files:
            hint = f"分类「{category}」" if category else "任何表情包"
            yield event.plain_result(f"没找到{hint}，检查 meme_manager 的资源包是否安装。")
            return

        # QQ 官方图片通道对体积敏感，优先挑 4MB 以内的
        small = []
        for p in files:
            try:
                if p.stat().st_size <= 4 * 1024 * 1024:
                    small.append(p)
            except OSError:
                continue
        pick = random.choice(small or files)
        size_mb = pick.stat().st_size / 1024 / 1024
        logger.info(
            "[FishAudio] 发送表情包：%s (%.2f MB, 共 %d 个候选)",
            pick.name, size_mb, len(small or files),
        )
        await event.send(event.chain_result([Image.fromFileSystem(str(pick))]))

    @filter.command("鱼语音", alias={"fysay", "鱼语音说"})
    async def fish_say(self, event: AstrMessageEvent, text: GreedyStr):
        """把指定文字合成为语音发送。用法：/鱼语音 你好呀"""
        raw = str(text or "").strip()
        if not raw:
            yield event.plain_result("用法：/鱼语音 <要朗读的文字>")
            return

        api = self._api()
        if not str(api.get("api_key", "")).strip():
            yield event.plain_result("还没配置 Fish Audio API Key，请到插件配置里填写。")
            return

        paths = await self._synth_text(
            raw, ignore_limits=True, platform=self._platform(event)
        )
        if not paths:
            yield event.plain_result(f"语音合成失败：{self._last_error or '未知错误'}")
            return

        display, _ = self._prepare(raw)
        chain = self._reply_paths(
            paths, display, bool(self._output().get("send_text", True))
        )
        await event.send(event.chain_result(chain))

    @filter.command("鱼音色", alias={"fyvoice", "音色"})
    async def fish_voice(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        """查看或切换参考音色：/鱼音色 列表 、/鱼音色 2"""
        files = self._voice_files()
        if not files:
            yield event.plain_result(
                "还没登记参考音频。把音频放到插件数据目录，"
                "再在配置 api.reference_files 里写上相对路径。"
            )
            return

        umo = self._umo(event)
        default_idx = int(self._api().get("reference_index", 0) or 0)
        current = int(self._state.get(umo, {}).get("voice", default_idx) or 0)
        if current < 0 or current >= len(files):
            current = 0

        text = str(arg or "").strip()
        if text in ("列表", "list", ""):
            lines = ["可用音色（用 /鱼音色 序号 切换）："]
            for idx, path in enumerate(files, 1):
                mark = "← 当前" if idx - 1 == current else ""
                size = path.stat().st_size / 1024
                lines.append(f"{idx}. {path.stem}（{size:.0f} KB）{mark}")
            yield event.plain_result("\n".join(lines))
            return

        if not text.isdigit():
            yield event.plain_result("用法：/鱼音色 列表 或 /鱼音色 2")
            return

        idx = int(text) - 1
        if idx < 0 or idx >= len(files):
            yield event.plain_result(f"序号超出范围（1~{len(files)}）。")
            return

        self._state.setdefault(umo, {})["voice"] = idx
        self._save_state()
        yield event.plain_result(f"已切换到音色：{files[idx].stem}")

    @filter.command("语音模型", alias={"ttsmodel"})
    async def tts_model(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        """查看/切换 TTS 后端：/语音模型 fish|mimo|custom"""
        api = self._api()
        umo = self._umo(event)
        default = str(api.get("provider", "fish")).lower()
        current = str(self._state.get(umo, {}).get("provider") or default).lower()

        text = str(arg or "").strip().lower()
        if text in ("", "列表", "list"):
            lines = [
                "可用后端（/语音模型 名字 切换，仅本会话）：",
                f"1. fish　—— Fish Audio（当前默认：{default}）",
                f"2. mimo　—— 小米 MiMo",
                f"3. custom —— 任意 OpenAI 兼容接口",
                f"本会话正在使用：{current}",
            ]
            yield event.plain_result("\n".join(lines))
            return

        if text in ("fish", "mimo", "custom", "openai"):
            chosen = "custom" if text == "openai" else text
            self._state.setdefault(umo, {})["provider"] = chosen
            self._save_state()
            yield event.plain_result(f"本会话已切换到后端：{chosen}")
            return

        yield event.plain_result("用法：/语音模型 fish|mimo|custom")

    @filter.command("语音自检", alias={"ttscheck"})
    async def tts_check(self, event: AstrMessageEvent):
        """跑一次真实合成，报告耗时与产物信息。"""
        umo = self._umo(event)
        started = time.monotonic()
        try:
            paths = await self._synth_text(
                "自检：这是一段测试语音，用来确认接口是否正常。",
                ignore_limits=True,
                platform=self._platform(event),
                umo=umo,
            )
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"自检失败：{type(exc).__name__}: {exc}")
            return
        cost = time.monotonic() - started
        if not paths:
            reason = self._last_error or "未知原因"
            yield event.plain_result(f"自检失败：{reason}")
            return
        size = sum(p.stat().st_size for p in paths if p.exists())
        provider = str(
            self._state.get(umo, {}).get("provider")
            or self._api().get("provider", "fish")
        )
        yield event.plain_result(
            f"自检通过\n后端：{provider}\n产物：{len(paths)} 段 / {size / 1024:.0f} KB\n"
            f"耗时：{cost:.2f}s"
        )

    @filter.command("鱼语音开")
    async def fish_on(self, event: AstrMessageEvent):
        self._set_session_enabled(event, True)
        yield event.plain_result("本会话自动语音已开启。")

    @filter.command("鱼语音关")
    async def fish_off(self, event: AstrMessageEvent):
        self._set_session_enabled(event, False)
        yield event.plain_result("本会话自动语音已关闭。")

    @filter.command("鱼语音状态")
    async def fish_status(self, event: AstrMessageEvent):
        api = self._api()
        out = self._output()
        cache = self._cache()
        try:
            files = list(self.audio_dir.glob("*"))
            cache_size = sum(f.stat().st_size for f in files if f.is_file())
        except OSError:
            files, cache_size = [], 0

        lines = [
            f"开关：{'开' if self._session_enabled(event) else '关'}（本会话）",
            f"模型：{api['model']}　音色：{api['reference_id'] or '（默认）'}",
            f"接口：{api['base_url']}",
            f"代理：{api['proxy'] or '（未设置）'}",
            f"自动语音：{'是' if out['auto_speak'] else '否'}　"
            f"附带文字：{'是' if out['send_text'] else '否'}　"
            f"概率：{out['probability']}",
            f"缓存：{'开' if cache['enabled'] else '关'}，"
            f"{len(files)} 个文件 / {cache_size / 1024 / 1024:.1f} MB",
        ]
        if self._last_error:
            lines.append(f"最近错误：{self._last_error}")
        yield event.plain_result("\n".join(lines))

    @filter.command("鱼语音测试", alias={"fytest"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fish_test(self, event: AstrMessageEvent):
        phrase = "[excited] 你好呀，我是大肥鱼。这条语音来自 Fish Audio 的语音合成。"
        paths = await self._synth_text(
            phrase, ignore_limits=True, platform=self._platform(event)
        )
        if not paths:
            yield event.plain_result(f"测试失败：{self._last_error or '未知错误'}")
            return
        size = sum(p.stat().st_size for p in paths)
        chain = [Plain(text=f"测试成功：{len(paths)} 段，{size / 1024:.0f} KB。")]
        chain.extend(Record(file=str(p)) for p in paths)
        await event.send(event.chain_result(chain))

    # --------------------------------------------------------------- cleanup
    async def terminate(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._custom is not None:
            await self._custom.close()
            self._custom = None
        if self._mimo is not None:
            await self._mimo.close()
            self._mimo = None
