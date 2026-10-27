# -*- coding: utf-8 -*-
"""集成自检：在 AstrBot 运行时里实例化插件，用假事件跑 on_decorating_result。

需要在 AstrBot core 目录下运行（cwd 影响 AstrBot 的路径解析）：
    cd <astrbot core>
    python <repo>/tests/test_plugin_hook.py
"""

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO.parent))

# AstrBot core 目录：默认取当前工作目录（脚本约定在 core 下运行），可用 ASTRBOT_CORE 覆盖
_CORE = Path(os.environ.get("ASTRBOT_CORE") or Path.cwd()).resolve()
if not (_CORE / "astrbot").is_dir():
    raise SystemExit(f"找不到 AstrBot core 目录：{_CORE}；请 cd 到 core 或设置 ASTRBOT_CORE")
sys.path.insert(0, str(_CORE))

from astrbot.api.message_components import Plain, Record  # noqa: E402

import astrbot_plugin_fishaudio_tts.main as plugin_mod  # noqa: E402


class FakeResult:
    def __init__(self, chain):
        self.chain = chain
        self.result_content_type = None

    def is_llm_result(self):
        return True


class FakeEvent:
    def __init__(self, chain, platform="qq_official", umo="qq_official:GroupMessage:10086"):
        self._result = FakeResult(chain)
        self._platform = platform
        self.unified_msg_origin = umo
        self.sent = []

    def get_platform_name(self):
        return self._platform

    def get_result(self):
        return self._result

    def is_admin(self):
        return True

    async def send(self, chain):
        self.sent.append(chain)

    def chain_result(self, chain):
        return FakeResult(chain)

    def plain_result(self, text):
        return FakeResult([Plain(text=text)])


def make_config(**overrides):
    cfg = {
        "api": {
            "base_url": os.environ.get("FISH_BASE_URL", "https://api.fish.audio/v1"),
            "api_key": os.environ.get("FISH_API_KEY", ""),
            "model": os.environ.get("FISH_MODEL", "s2.1-pro-free"),
            "reference_id": os.environ.get("FISH_REFERENCE_ID", ""),
            "proxy": os.environ.get("FISH_PROXY", ""),
            "timeout": 60,
            "max_retries": 1,
            "format": "mp3",
        },
        "output": {
            "auto_speak": True,
            "send_text": True,
            "platforms": ["aiocqhttp", "qq_official", "qq_official_webhook"],
            "probability": 1.0,
            "min_chars": 2,
            "max_chars": 120,
            "max_segments": 3,
            "cooldown_seconds": 0,
            "max_concurrency": 2,
            "skip_if_record_exists": True,
            "qq_official_wav": True,
        },
        "emotion": {"enabled": True, "default_tag": "", "extra_tags": []},
        "cache": {"enabled": True, "max_files": 50, "ttl_days": 7},
        "session": {"default_enabled": True, "whitelist": [], "blacklist": []},
    }
    for section, values in overrides.items():
        cfg.setdefault(section, {}).update(values)
    return cfg


async def main():
    if not os.environ.get("FISH_API_KEY"):
        print("缺少 FISH_API_KEY")
        return 2

    # 每次从干净状态开始（会话开关会持久化到 data/sessions.json）
    (REPO / "data" / "sessions.json").unlink(missing_ok=True)

    plugin = plugin_mod.FishAudioTTS(context=None, config=make_config())
    failures = []

    def check(cond, msg):
        if cond:
            print(f"PASS {msg}")
        else:
            failures.append(msg)
            print(f"FAIL {msg}")

    # A. 正常回复 -> 文本 + 语音
    ev = FakeEvent([Plain(text="你好呀，今天天气不错，我们出去走走吧。")])
    await plugin.on_decorating_result(ev)
    chain = ev.get_result().chain
    check(len(chain) == 2 and isinstance(chain[-1], Record), "正常回复插入 Record")
    if isinstance(chain[-1], Record):
        p = Path(chain[-1].file)
        check(p.exists() and p.stat().st_size > 10_000, f"音频文件存在且够大（{p.stat().st_size} 字节）")
        check(p.suffix.lower() == ".wav", f"qq_official 自动请求 WAV（{p.name}），无需 ffmpeg")
    check(isinstance(chain[0], Plain) and "天气" in chain[0].text, "保留可见文本")

    # A2. aiocqhttp（NapCat 等）仍用配置里的 mp3
    ev = FakeEvent(
        [Plain(text="这条走 NapCat 的 OneBot 通道。")],
        platform="aiocqhttp",
        umo="aiocqhttp:FriendMessage:777",
    )
    await plugin.on_decorating_result(ev)
    chain2 = ev.get_result().chain
    if isinstance(chain2[-1], Record):
        check(Path(chain2[-1].file).suffix.lower() == ".mp3", "aiocqhttp 保持 mp3 格式")

    # B. 情绪标签：传给 TTS，但不出现在可见文本里
    ev = FakeEvent([Plain(text="[excited] 太好了！[laughing] 真不错。")])
    await plugin.on_decorating_result(ev)
    chain = ev.get_result().chain
    check(isinstance(chain[0], Plain) and "excited" not in chain[0].text, "情绪标签从可见文本剥离")
    check(isinstance(chain[-1], Record), "带标签也能发语音")

    # C. 链里已有 Record（其它 TTS 插件处理过）-> 跳过
    existing = Record(file="D:/existing.mp3")
    ev = FakeEvent([Plain(text="这句话别的插件已经配过音了。"), existing])
    await plugin.on_decorating_result(ev)
    check(len(ev.get_result().chain) == 2 and ev.get_result().chain[-1] is existing, "已存在语音段时跳过，不叠加")

    # D. auto_speak 关闭 -> 不动
    p2 = plugin_mod.FishAudioTTS(context=None, config=make_config(output={"auto_speak": False}))
    ev = FakeEvent([Plain(text="这条不该变成语音。")])
    await p2.on_decorating_result(ev)
    check(len(ev.get_result().chain) == 1, "auto_speak=False 时保持纯文本")

    # E. 平台不匹配 -> 不动
    p3 = plugin_mod.FishAudioTTS(context=None, config=make_config())
    ev = FakeEvent([Plain(text="这条也不该变成语音。")], platform="telegram")
    await p3.on_decorating_result(ev)
    check(len(ev.get_result().chain) == 1, "非 QQ 平台不处理")

    # F. 会话关闭 -> 不动
    p4 = plugin_mod.FishAudioTTS(context=None, config=make_config())
    ev = FakeEvent([Plain(text="会话关了就别说话。")])
    p4._set_session_enabled(ev, False)
    await p4.on_decorating_result(ev)
    check(len(ev.get_result().chain) == 1, "会话关闭后不处理")

    # G. 限长切分：长回复 -> 多段，段数受 max_segments 限制
    long_text = "今天天气很好。我们去公园散步吧。顺便买点水果回来。晚上一起做饭吃。"
    p5 = plugin_mod.FishAudioTTS(
        context=None,
        config=make_config(output={"max_chars": 14, "max_segments": 2}),
    )
    ev = FakeEvent([Plain(text=long_text)], umo="qq_official:GroupMessage:20001")
    await p5.on_decorating_result(ev)
    records = [c for c in ev.get_result().chain if isinstance(c, Record)]
    check(1 <= len(records) <= 2, f"长文本按段数上限切分（{len(records)} 段）")

    # H. 命令手动朗读
    p6 = plugin_mod.FishAudioTTS(context=None, config=make_config())
    ev = FakeEvent([], umo="qq_official:GroupMessage:30001")
    async for _ in p6.fish_say(ev, "你好，这是手动触发的一条语音。"):
        pass
    check(len(ev.sent) == 1, "手动命令发送了一条消息链")
    if ev.sent:
        comps = ev.sent[0].chain
        check(any(isinstance(c, Record) for c in comps), "手动命令包含语音段")

    # I. 状态命令
    p7 = plugin_mod.FishAudioTTS(context=None, config=make_config())
    ev = FakeEvent([], umo="qq_official:GroupMessage:30002")
    outs = [r async for r in p7.fish_status(ev)]
    check(outs and "模型" in outs[0].chain[0].text, "状态命令输出正常")

    for p in (plugin, p2, p3, p4, p5, p6, p7):
        await p.terminate()

    print("---")
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
