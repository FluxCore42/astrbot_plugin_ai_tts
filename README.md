# AI 语音（AstrBot 插件）

把 AstrBot 的文字回复变成语音，**一条回复只发一条语音**。
支持 Fish Audio、小米 MiMo，以及**任何 OpenAI 兼容的 TTS 接口**（自己填地址和 Key）。

![logo](logo.png)

## 特性

| 能力 | 说明 |
|---|---|
| 多后端可插拔 | `fish` / `mimo` / `custom`（OpenAI 兼容），运行时可切换 |
| 一条回复一条语音 | 不再出现「文字 + 语音」两条气泡 |
| 18 类情绪 | 关键词分类，映射成音色能量、语速或 MiMo 情绪前缀 |
| 音色热切换 | `/鱼音色` 在多个参考音频间切换；配置里填训练音色 id 更稳 |
| 尾音修复 | 重写 WAV 头 + 40ms 淡出 + 250ms 静音，消除截断与「咔」声 |
| 朗读清理 | 去动作描写、去 Markdown、半角转全角、去 emoji |
| 表情配图 | 结合 meme_manager 图库，按关键词/AI 选类随机配图 |
| 会话控制 | 按会话开关语音、冷却、并发限制、黑白名单 |
| 磁盘缓存 | 相同文本+音色命中缓存，省时省钱 |

## 安装

`ash
cd AstrBot/data/plugins
git clone https://github.com/FluxCore42/astrbot_plugin_ai_tts.git
`

或手动下载后：

1. 把整个目录放进 `AstrBot/data/plugins/astrbot_plugin_ai_tts/`
2. 重启 AstrBot，或在面板里重载插件
3. 面板 → 插件 → **AI 语音** → 填配置

依赖只有 `aiohttp`（AstrBot 自带）。

## 三种后端怎么填

### ① Fish Audio

| 字段 | 值 |
|---|---|
| `provider` | `fish` |
| `base_url` | `https://api.fish.audio/v1` |
| `api_key` | 你的 Fish Key |
| `model` | `s2.1-pro-free`（免费档）或付费模型 |
| `reference_id` | 训练音色 id（推荐，最稳） |
| `reference_files` | 参考音频路径列表（零样本，会漂，不推荐） |
| `proxy` | 需要科学上网时填 `http://127.0.0.1:7892` |

> Fish 也能直接传参考音频做零样本克隆，但实测同一段文本三次合成音高差 60–120Hz，
> 听感像换了个人。**想要稳定就用训练音色 id。**

### ② 小米 MiMo

| 字段 | 值 |
|---|---|
| `provider` | `mimo` |
| `mimo_key` | MiMo API Key |
| `mimo_base_url` | `https://api.xiaomimimo.com/v1` |
| `mimo_model` | `mimo-v2.5-tts` |
| `mimo_voice` | `冰糖` / `茉莉` / `苏打` / `白桦` …，或填本地音频路径做克隆 |

### ③ 任意 OpenAI 兼容接口（custom）

| 字段 | 说明 |
|---|---|
| `provider` | `custom` |
| `custom_base_url` | 到 `/v1` 为止，例如 `https://api.siliconflow.cn/v1` |
| `custom_api_key` | 该服务的 Key |
| `custom_model` | 模型名，如 `tts-1`、`FunAudioLLM/CosyVoice2-0.5B` |
| `custom_voice` | 音色名，留空则不传 |
| `custom_auth_header` | `bearer` / `api-key` / `x-api-key` |
| `custom_json_field` | 接口若返回 JSON+base64，填字段路径（如 `data.audio`） |
| `custom_extra_json` | 额外请求体，如 `{"speed":0.95}` |

插件会请求 `{custom_base_url}/audio/speech`，请求体：

```json
{"model": "...", "input": "要念的文字", "voice": "...", "response_format": "wav"}
```

## 配置分组

面板里按 ①~⑧ 排列：

| 分组 | 关键项 |
|---|---|
| ① 语音引擎 | `provider`、`base_url`、`api_key`、`model`、`reference_id`、`format`、`proxy` |
| ② 通用引擎 | `custom_*`（OpenAI 兼容接口） |
| ③ 发语音策略 | `auto_speak`、`probability`、`max_chars`、`send_text`、`speed`、`tail_padding_ms` |
| ④ 情绪与语气 | `auto_detect`、`prosody`、`strength`、`fish_emotion`、`text_style` |
| ⑤ 回复清理 | `max_chars`（回复上限）、`strip_actions` |
| ⑥ 表情配图 | `probability`、`max_size_mb`、`skip_when_voice` |
| ⑦ 缓存与性能 | `enabled`、`max_files`、`ttl_days` |
| ⑧ 会话控制 | `default_enabled`、`whitelist`、`blacklist` |

## 指令

| 指令 | 作用 |
|---|---|
| `/鱼语音 <文字>` | 指定文字转语音 |
| `/鱼语音开` / `/鱼语音关` | 本会话自动语音开关 |
| `/鱼语音状态` | 查看当前配置与缓存 |
| `/鱼语音测试` | 立刻合成一句（管理员） |
| `/语音模型` | 查看可用后端 |
| `/语音模型 fish\|mimo\|custom` | 切换本会话后端 |
| `/语音自检` | 真实合成一次，报告耗时与产物大小 |
| `/鱼音色 列表` / `/鱼音色 2` | 在参考音频之间切换 |
| `/发表情 [分类]` | 发一张表情包 |

## 常见问题

**语音结尾被切掉 / 有「咔」声？**
Fish 返回的 WAV 头 `data` 长度字段不可靠。本插件会重写文件头、做 40ms 淡出并补
`tail_padding_ms`（默认 250ms）静音。如果嫌停顿长，可调到 150；0 表示不加静音。

**说错话了 / 角色坚持「我发不出声音」？**
模型把之前的失败写进了上下文。发 `/reset` 清一次会话即可。

**语音里有「（歪头）」这种旁白？**
本插件会删括号内容与星号动作；仍出现说明是正文叙述（没有括号），
可在人格提示词里加一句「禁止动作描写」。

**同一个音色听起来有时像别人？**
零样本克隆（传参考音频）每次都会重新猜说话人。换训练音色 id，或减少 `emotion`/`prosody`。

**一条消息回了好几条？**
AstrBot 的「分段回复」要关掉：`platform_settings.segmented_reply.enable = false`。

## 目录结构

```
main.py              插件主逻辑（钩子、指令、发送）
emotion.py           18 类情绪分类与 prosody 映射
reply_style.py       回复清理（去动作/去 Markdown/全角标点）
providers/
  fish.py            Fish Audio 客户端（含 WAV 修复、缓存）
  mimo.py            小米 MiMo 客户端
  openai.py          OpenAI 兼容客户端
  text.py            文本切分与情绪标签解析
tests/               离线测试
```

## License

MIT