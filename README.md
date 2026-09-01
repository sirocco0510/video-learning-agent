---
title: 视频挂机学习 Agent
created: 2026-08-31
updated: 2026-08-31
type: project
status: active
tags:
  - type/project
  - topic/video-learning
---

# 视频挂机学习 Agent

> 本地运行的视频挂机学习 + 内容总结工具,主打 **B站** 视频源,**字幕转写零云端成本**(用本地 faster-whisper)。

---

## 项目文档

| 文档 | 用途 |
|------|------|
| [[requirements]] | **需求规格**:功能、非功能、模块接口、验收标准(给 VS Code AI 助手看的 SSOT) |
| [[implementation-plan]] | **实施计划**:9 个 Phase 顺序推进,每个 Phase 含实现要点 + 验收代码 |
| 本 README | 项目入口 + 快速上手 |

---

## 一句话目标

**自动播放 B站视频 → 提取字幕(三级策略) → 本地 Whisper 转写 → 云端质量检查 → LLM 总结 → 追加到笔记**。

---

## 核心约束(实施前必须理解)

1. **字幕转写** = 永远**免费 / 本地 / 开源**
   - 主力:`faster-whisper`(MIT)
   - 兜底:B站官方 CC 字幕 / VideoTrans 浏览器插件导出文件
2. **云端 LLM** = **只用于两件事**
   - 字幕质量检查(从 LLM 接口读 `pass` + `score`)
   - 最终内容总结(500–800 字,**6h 批量触发**)
3. **磁盘友好**:256 GB 硬盘,每天 6 小时视频,峰值占用 < 1 GB
   - 策略 = 边转写边删,质量检查通过后才允许删源文件
4. **失败可追溯**:所有失败(转写失败 / 质量不过关)→ CSV 日志 + 原文存档 → 可重试
5. **字幕降级语义**(FR-2.5 / 2.6 / 2.8):
   - 弹窗超时 / 用户点"跳过" → **降级到策略 ③**(走下载/录屏 + Whisper)
   - **不是**记 `transcribe_fail`,不是跳过当前视频
   - 只有策略 ③ 本身(Whisper 转写)失败才记 `transcribe_fail.csv`
6. **插件"一次启动"语义**(FR-2.9/2.10):
   - 整 session 只弹一次窗,状态记在 `PluginStatus`
   - `unavailable` 后后续视频直接走策略 ③,**不再弹窗**
   - session 重启状态重置
7. **累计时长触发总结**(FR-9):
   - 默认 6 小时(`summary_threshold_sec: 21600`)
   - 到达阈值 → 触发 `LLMSummarizer.summarize_batch`(500-800 字)
   - 总结完后累加器归零,**session 停止**(`on_exhausted: stop_session`)
8. **去重**(FR-9.6):
   - 成功的视频 URL 写入 `logs/transcribed_history.jsonl`
   - 下次启动自动跳过已成功的 URL
   - 想重新看 → 手动清空 history
9. **通知分级策略**(FR-6):
   - **A 阻塞弹窗** = 插件启用 + 失败日志上限,需要用户响应
   - **B 非阻塞通知** = 质量通过、6h 触发总结等进度性消息
   - **C 静默** = 转写失败 / 质量不过关,**不通知**,只写 CSV 日志
10. **失败日志上限弹窗**(FR-6.6):
    - `transcribe_fail + quality_fail` 累计达到 `log_alert_threshold`(默认 50)的整数倍
    - → **阻塞弹窗汇总**,提供"查看 logs/"按钮(打开 Finder)
    - 跨过倍数边界才弹,避免每条失败都打扰

---

## 项目结构

```text
video-learning-agent/
├── src/vla/
│   ├── main.py                  # 主调度器
│   ├── cli.py                   # typer CLI
│   ├── config.py                # 配置加载
│   ├── models.py                # pydantic 数据模型
│   ├── llm/client.py            # 统一 LLM 客户端
│   ├── source/video_source.py   # 下载 OR 录屏
│   ├── subtitle/
│   │   ├── strategy.py          # 三级调度(含插件状态机)
│   │   ├── bilibili_official.py # 策略 ①
│   │   └── browser_plugin.py    # 策略 ②
│   ├── transcribe/streaming.py  # faster-whisper
│   ├── quality/checker.py       # 质量检查
│   ├── summary/llm_summarizer.py# 批量总结(summarize_batch)
│   ├── ui/macos_notify.py       # macOS 通知
│   ├── log/transcription_log.py
│   ├── log/failure_alert.py     # FR-6.6 失败日志上限监控
│   └── state/                   # FR-9/10 + FR-2.10 状态管理
│       ├── quota.py             # 累计时长配额
│       ├── history.py           # 去重历史(jsonl)
│       └── plugin_status.py     # 插件状态机
├── config/vla.yaml              # 默认配置
├── videos.yaml                  # 视频组输入(FR-10)
└── tests/
```

---

## 快速开始

```bash
# 1. 系统依赖(macOS)
brew install ffmpeg

# 2. Python 项目
uv init video-learning-agent && cd video-learning-agent
uv add faster-whisper yt-dlp httpx openai pyobjc-framework-Quartz \
       pysrt webvtt-py pyyaml pydantic typer rich
uv add --dev pytest pytest-asyncio

# 3. 准备环境
export OPENAI_API_KEY=sk-xxx
# 或 DeepSeek / Qwen
export OPENAI_BASE_URL=https://api.deepseek.com/v1

# 4. 检测环境
uv run vla doctor

# 5. 单条测试(找一个 B站有 CC 的)
uv run vla process \
  --url "https://www.bilibili.com/video/BV1xxxxxxx" \
  --title "Python 入门" \
  --duration 1800 \
  --group-id "python-basics"

# 6. 批量处理(从 videos.yaml,FR-10)
uv run vla batch --config ./videos.yaml
```

### `videos.yaml` 写法(FR-10.5)

```yaml
# 视频组(暂时用 group_id = 视频组名字)
video_groups:
  - group_id: python-tutorial-basics
    title: "Python 基础教程"
    videos:
      - bvid: BV1xxxxxxxx
        title: "第1集 变量与类型"
        url: "https://www.bilibili.com/video/BV1xxxxxxxx"
        duration_sec: 1800
      - bvid: BV1yyyyyyyy
        title: "第2集 控制流"
        url: "https://www.bilibili.com/video/BV1yyyyyyyy"
        duration_sec: 2400
      - bvid: BV1zzzzzzzz
        title: "第3集 函数"
        url: "https://www.bilibili.com/video/BV1zzzzzzzz"
        duration_sec: 2100
```

内部 URL 表示(去重 key,FR-10.2):

```text
原始 URL:https://www.bilibili.com/video/BV1xxxxxxxx
内部 key:bilibili://group/python-tutorial-basics/BV1xxxxxxxx
```

---

## 字幕三级策略

```text
优先级:① B站官方 CC → ② 浏览器插件 → ③ 下载/录屏 + Whisper
```

**关键降级语义**(FR-2.5 / 2.6):

```text
策略 ① 失败/无字幕 ──┐
策略 ② 弹窗超时   ──┼──→ 主调度走策略 ③(下载/录屏 + Whisper)
策略 ② 用户点跳过  ──┘   (不是跳过,不是 transcribe_fail)
```

详细规格见 [[requirements#FR-2 字幕提取(三级策略)]]。

---

## 视频源双路径

```text
yt-dlp simulate 检测
  ├─ OK  → yt-dlp download(最低画质)
  └─ FAIL → ffmpeg + avfoundation 录屏(策略 B:含系统音频)
```

详细规格见 [[requirements#FR-1 视频源管理]] 与 [[requirements#FR-8 录屏与音频]]。

---

## 质量门控

```text
Whisper 转写
   ↓
启发式预筛(字/秒 + 重复)
   ├─ FAIL → 记录日志,保留源文件
   └─ PASS → 云端 LLM 审核
              ├─ PASS(score ≥ 70)→ 删源文件 + 总结 + 写笔记
              └─ FAIL              → 记录日志,保留源文件 + 原文
```

详细规格见 [[requirements#FR-4 质量门控]]。

---

## 累计时长触发总结(FR-9)

```text
每条视频质量通过 → 累加器 += duration_sec
  │
  ├─ 累加器 < 21600 (6h) → 继续下一个视频(字幕缓存到窗口)
  │
  └─ 累加器 ≥ 21600 → 触发 summarize_batch(window)
                          ├─ 一次性生成 500-800 字总结
                          ├─ 写入 notes.md
                          ├─ 累加器归零
                          └─ session 停止
```

**关键**:
- ❌ 不是每条视频都总结
- ✅ 是**累计 6h**才总结一次
- ✅ 总结完**不再继续看**(可重启 session 重新累计)

---

## 视频组与 URL 去重(FR-9/10)

```yaml
# videos.yaml 写法:视频组 → 视频列表
video_groups:
  - group_id: python-basics    # 暂时用名字当 ID
    videos:
        - { url: "...", bvid: "BV1xxx", duration_sec: 1800 }
```

**URL 去重 key**(FR-10.2):
```text
bilibili://group/{group_id}/{bvid}
```

**成功转写一条** → 写入 `logs/transcribed_history.jsonl`:
```json
{"url": "bilibili://group/python-basics/BV1xxx",
 "title": "...", "duration_sec": 1800,
 "transcribed_at": "2026-08-31T10:00:00"}
```

**下次启动** → 自动过滤已成功 URL,直接跳过。

详细规格见 [[requirements#FR-9 累计时长与去重(配额管理)]] 与 [[requirements#FR-10 视频组概念(B站 playlist / 番剧 / 合集)]]。

---

## 插件状态机(FR-2.9/2.10)

```text
整 session 只有一个插件状态(PluginStatus 单例)
  │
  ├─ unknown(默认)
  │    └─ 第一次需要插件时弹窗
  │         ├─ 用户"已开启" + 等到文件 → available
  │         ├─ 用户"已开启" + 超时 → unavailable(后续不弹窗)
  │         ├─ 用户"跳过" → unavailable
  │         └─ 弹窗超时 → unavailable
  │
  ├─ available(用户已开启且有效)
  │    ├─ 扫描有文件 → 走质量门控
  │    └─ 扫描无文件 → unavailable(后续不弹窗)
  │
  └─ unavailable(后续视频直接降级到策略 ③)
       │
       └─ 插件字幕质量不过关 → 自动转 unavailable(FR-2.11)
```

**关键**:
- 整 session **只弹一次窗**(FR-2.9)
- `unavailable` 后不再尝试,**直走策略 ③**
- session 重启 → 状态重置为 `unknown`

---

## 通知分级(FR-6)

| 级别 | 触发场景 | 形式 | 典型事件 |
|------|----------|------|----------|
| **A 阻塞弹窗** | 必须用户响应 | `osascript display dialog` + 超时降级 | • 插件启用确认(FR-2.9)<br>• 失败日志达上限(FR-6.6) |
| **B 非阻塞通知** | 进度性消息 | `display notification`(macOS 通知中心) | • 质量通过(进度)<br>• 累计 6h 触发总结(FR-9.4)<br>• 视频源降级提示 |
| **C 静默** | 不打扰用户 | 只写 CSV 日志,不弹任何通知 | • 转写失败(FR-6.4)<br>• 质量不过关(FR-6.4)<br>• 插件字幕质量不过关(FR-2.11) |

**关键语义**:
- 转写失败 ≠ 不严重的失败;只是**不打扰用户**,日志 + 原文存档保证可追溯
- 失败累计到 `log_alert_threshold` 倍数 → **升级为阻塞弹窗**(FR-6.6)
- 用户打开弹窗可一键跳转 `logs/` 目录(打开 Finder)

详细规格见 [[requirements#FR-6 macOS 系统通知]]。

---

## 失败日志上限弹窗(FR-6.6)

```text
每写一条 transcribe_fail / quality_fail → check_after_write()
  │
  ├─ 累计 < log_alert_threshold (默认 50) → 静默,不通知
  │
  └─ 累计 ≥ log_alert_threshold 且跨过新的整数倍 → 阻塞弹窗汇总
       ├─ 标题:"⚠️ 失败积累过多"
       ├─ 内容:"已积累 N 条失败(转写 X 条 + 质量 Y 条),请检查 logs/"
       ├─ 按钮:"OK" / "查看 logs/"
       └─ 点击"查看 logs/" → subprocess.run(["open", logs_dir])
```

**关键设计**:
- `last_alerted_multiple` 字段记录已弹过的最大倍数,**只在跨边界时弹**,不重复打扰
- 默认 `log_alert_threshold = 50`(可在 `config/vla.yaml` 的 `notifier.log_alert_threshold` 调整)
- 弹窗本身有 `timeout_sec`(默认 60s),用户不响应也不阻塞主流程

详细规格见 [[requirements#FR-6 macOS 系统通知]] 中的 FR-6.5/6.6/6.7。

---

## VS Code 开发环境(替代 Cursor)

由于 Cursor 免费版限制,本项目改用 **VS Code + AI 助手扩展** 开发。

### 推荐扩展(免费 + 可接国内大模型)

| 扩展 | 特点 |
|------|------|
| **Continue**(开源) | 支持 OpenAI / Anthropic / Ollama / DeepSeek / Qwen |
| **Cline** | Claude / GPT / Gemini / 国产模型,多文件编辑能力强 |
| **Roo Code** | Cline fork,UI 更现代,带图形化工作流 |

### 工作流

1. 三份文档(`README` / [[requirements]] / [[implementation-plan]])就是 AI 助手的**上下文 SSOT**
2. 每个 Phase 开始时,把 [[implementation-plan]] 对应小节的"必读" + "验收"代码贴给 AI 助手
3. AI 助手按 Phase 推进,每完成一个跑验收代码,通过再开下一个

详细使用建议见 [[implementation-plan#VS Code AI 助手使用建议]]。

---

## macOS 首次运行需要授权

| 权限 | 触发场景 | 设置路径 |
|------|----------|----------|
| 屏幕录制 | ffmpeg 录屏时 | 系统设置 → 隐私与安全性 → 屏幕录制 |
| 通知 | `display notification` | 系统设置 → 通知 → 允许终端 / osascript |
| 辅助功能 | AppleScript 弹窗 | 系统设置 → 辅助功能 |

---

## 文档维护原则

- 修改需求 → 先改 [[requirements]],再同步 [[implementation-plan]]
- 每个 Phase 完成后,在 [[implementation-plan#进度跟踪]] 勾掉
- 验收代码 = 唯一真相;改了规格必须同步更新验收代码

---

## 相关资源

- [B站开放平台](https://socialsiki.bilibili.com/)(API 文档参考)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [VideoTrans](https://github.com/sign/VideoTranslator)(浏览器插件)
- [ffmpeg avfoundation](https://ffmpeg.org/ffmpeg-devices.html#avfoundation)