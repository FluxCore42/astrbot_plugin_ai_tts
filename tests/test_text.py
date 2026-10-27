# -*- coding: utf-8 -*-
"""离线单测：只测文本处理，不联网。运行：python tests/test_text.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.text import (  # noqa: E402
    build_tag_whitelist,
    clean_for_speech,
    extract_emotion_tags,
    normalize_text,
    split_for_tts,
)

WL = build_tag_whitelist()


def test_normalize():
    assert normalize_text("  ｈｅｌｌｏ\u3000世界  ") == "hello 世界"
    assert normalize_text("\n\n\n\nabc") == "abc"


def test_extract_tags():
    clean, tags = extract_emotion_tags("[excited]你好呀[laughing]真不错", WL)
    assert tags == ["excited", "laughing"], tags
    assert clean == "你好呀 真不错".replace("  ", " "), repr(clean)

    # 不在白名单里的方括号要保留，避免误删正文
    clean2, tags2 = extract_emotion_tags("[图片] 这是一张图", WL)
    assert tags2 == []
    assert "[图片]" in clean2


def test_clean_for_speech():
    src = "**加粗** 看这里 https://example.com/a?b=1 `code` @某人"
    out = clean_for_speech(src)
    assert "https://" not in out
    assert "**" not in out
    assert "加粗" in out


def test_strip_code_block():
    src = "说明：\n```python\nprint('hi')\n```\n结束"
    out = clean_for_speech(src)
    assert "print" not in out
    assert "说明" in out and "结束" in out


def test_split_short():
    assert split_for_tts("你好呀", 120, 3) == ["你好呀"]


def test_split_long():
    text = "第一句话在这里。第二句话也在这里！第三句呢？"
    segs = split_for_tts(text, 12, 3)
    assert len(segs) >= 2, segs
    assert all(len(s) <= 12 for s in segs), segs
    assert "".join(segs).replace(" ", "") == text.replace(" ", "")


def test_split_max_segments():
    text = "句子。" * 50
    segs = split_for_tts(text, 6, 2)
    assert len(segs) <= 2, segs


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("---")
    print("ALL PASS" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
