---
title: 视频挂机学习 Agent — 需求规格
created: 2026-08-31
updated: 2026-08-31
type: project
status: active
tags:
  - type/project
  - topic/video-learning
  - topic/whisper
  - topic/bilibili
---

# 视频挂机学习 Agent — 需求规格

> 本文档为 VS Code AI 助手实施时的**唯一事实来源**(Single Source of Truth)。
> VS Code AI 助手必须严格按照本文档的模块边界、接口契约、验收标准实现。

---

## 一、项目目标

构建一个本地运行的 Agent,完成以下三件事:

1. **自动播放**B站(及类似网站)在线视频,模拟鼠标 hover 触发进度条,记录开始/结束截图。
2. **提取字幕**并生成 500–800 字的视频内容总结,以 Markdown 追加到笔记文件。
3. **最小化云端 API 成本**:字幕转写**只用本地开源模型**;云端模型**只用于字幕语义调整+ 质量检查 + 最终总结**。

---

## 二、功能需求(FR)

### FR-0 变更日志(CHANGELOG)

> **用途**:记录需求/设计的**改动点 + 删减点**,便于审计与回溯。SSOT 锚定:`requirements.md` 改 → 同步 `implementation-plan.md` → 改代码 → 跑验收。每次重大重构追加一段,旧段不删除(保留历史)。

#### 2026-09-03 重构 v3 — 方案 A + 方案 C 落地

**背景**:本轮做了两个独立但互补的重构:
- **方案 A**:音频源从"三级降级"砍到"二级降级"(砍掉 Puppeteer 流式路径)
- **方案 C**:Tab Audio Recorder 扩展通知从"A 级阻塞 dialog"改为"自动探测 + B 级非阻塞通知"

**改动点**:

| # | 位置 | 改动描述 |
|---|------|----------|
| 1 | FR-2.14 | 音频源**三级 → 二级**(删路径 ② Puppeteer 流式录音);总览段加入"为什么砍 Puppeteer"说明(`getUserMedia({audio:true})` 只能拿麦克风,无法访问 `chrome.tabCapture`) |
| 2 | FR-2.16 | 音频格式从 "`.wav`(yt-dlp)或 `.webm`(Puppeteer / Tab Audio Recorder)" → "`.wav`(yt-dlp)或 `.webm`(Tab Audio Recorder)";新增不变量"Whisper 永不经过麦克风 ADC" |
| 3 | FR-2.19 | 通用 fallback adapter 改"跳过策略 ①,直接走 ② Tab Audio Recorder" |
| 4 | FR-2.21 | 探测触发条件"路径 ①② miss →" → "路径 ① miss 后" |
| 5 | FR-2.22 | 音频文件清理规则删 "路径 ② Puppeteer 流式录的 `.webm`" |
| 6 | FR-2.26 | 命名规范删 `<bvid>_<timestamp>.webm`(Puppeteer 流式命名) |
| 7 | FR-2.24 | `TabAudioRecorder._resolve_ext_id()` 动态从 `chrome.management.getAll()` 匹配(不硬编码 `hanfcigjijjcbdbfoplddndcblmlfiio`) |
| 8 | FR-2.24a | 新增 `probe_status() -> Literal["enabled","disabled","not_installed"]` 三态探测 |
| 9 | FR-2.25 | `DownloadButtonClicker.click_download(audio_id, ext_id, ...)` 接收动态 ext_id 参数 |
| 10 | FR-1.5 | "可下载 → 下载;不可下载 → 系统录屏降级" → "可下载 → yt-dlp -x 抽音频;不可下载 → Tab Audio Recorder" |
| 11 | FR-8(整段) | 重命名为"音频源规范",删 FR-8.1/8.2/8.3/8.4(原 ffmpeg + avfoundation 录屏),新增 FR-8.1/8.2(yt-dlp -x + Tab Audio Recorder) |
| 12 | FR-2 顶层表(§二) | 策略 ③ 描述 "音频三级降级 + Whisper" → "音频二级降级 + Whisper",子路径删 Puppeteer |
| 13 | §六 模块清单 | 新增 `subtitle/platform_adapter.py` / `bilibili_adapter.py` / `tab_audio_recorder.py` / `audio/source_factory.py` / `audio/queue.py` / `audio/worker_pool.py` |
| 14 | §六 MacOSNotifier | 删除 `ask_open_browser()` 方法;`alert()` 标注"仅用于 FR-6.6 失败日志上限" |
| 15 | §七 数据流 | 单条视频完整数据流重写:删 macOS 弹窗逻辑、删录屏路径、删 `remind_timeout_sec`;字幕降级路径重写为 `PlatformAdapter.get_subtitle()` 三级调度 |
| 16 | §八 配置 vla.yaml | 删 `video_source.record`(screen_index / fps / crf / audio_input / preset);删 `browser_plugin.remind_timeout_sec` + `plugin_paths`;新增 `extension.tab_audio_recorder.match_keyword` |
| 17 | §十一 AC-1/AC-4/AC-9 | 三段验收标准重写,删"插件弹窗"场景,加"Tab Audio Recorder 三态分支" |

**删减点**(代码/实现/引用):

| #   | 删除内容                                               | 原位置                                                                                                         | 原因                                                    |
| --- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| 1   | **FR-2.23**(扩展 popup 清理代码)                         | §二 FR 表                                                                                                     | 用户明确指示"扩展 popup 清理代码不保留"                              |
| 2   | **路径 ② Puppeteer 流式录音**                            | FR-2.14 / FR-2.16b / FR-2.22 / FR-2.26 / 架构图 / §七 数据流 / §十一 AC                                              | `getUserMedia` 只能拿麦克风,音频质量差 + 需额外授权                   |
| 3   | **硬编码扩展 ID** `hanfcigjijjcbdbfoplddndcblmlfiio`    | 架构图 / FR-2.14 / FR-2.15 / FR-2.24 / FR-2.25                                                                 | 用户明确指示"扩展 ID 不要固定,从浏览器扩展列表中拉取"                        |
| 4   | **A 级阻塞 dialog 通知**(`ask_open_browser`)            | FR-6.1/6.2/6.3 / `MacOSNotifier` 类 / §十一 AC-4 / §十一 AC-9                                                    | 方案 C 改为 `probe_status` 自动探测 + B 级非阻塞通知,无需用户介入         |
| 5   | **PluginStatus 单例 + session 状态机**                  | FR-2.9 / FR-2.10 / FR-2.11 / §十一 AC-9                                                                       | 与 `ask_open_browser` 同步删除,改为无状态 `probe_status` 每次即时探测 |
| 6   | **ffmpeg + avfoundation 录屏**                       | FR-8.1/8.2/8.3/8.4 / 配置 `video_source.record.*` / `AudioSourceFactory._record_screen` / `BrowserRecorder` 类 | 不再录屏,只抽音频,macOS 屏幕录制权限不再需要                            |
| 7   | **`browser_plugin.remind_timeout_sec`** 30s 弹窗超时   | FR-2.6 / 配置 `vla.yaml` / `MacOSNotifier.ask_open_browser`                                                   | 弹窗已删除,配置项无意义                                          |
| 8   | **`browser_plugin.plugin_paths`**(VideoTrans 扫描目录) | 配置 `vla.yaml` / `BrowserPluginSubtitle.find_subtitle`                                                       | 字幕策略 ② 改为 Puppeteer 通用 JS 探测,不再扫描目录                   |

**新增点**:

| # | 新增内容 | 位置 | 用途 |
|---|----------|------|------|
| 1 | FR-2.15c(字幕语义清理 Level 1/4) | §二 FR 表 | 本地 `merge_short_lines` + `dedupe_repeated_segments` + 可选云端 `SubtitleRefiner` |
| 2 | FR-2.24a(`probe_status()` 三态探测) | §二 FR 表 | 扩展状态自动探测,无状态、无锁 |
| 3 | FR-2.27(异步音频队列 + Whisper worker 池) | §二 FR 表 | 多视频并发,避免主流程被单条卡住 |
| 4 | `audio/` 目录(`source_factory.py` / `queue.py` / `worker_pool.py`) | §六 模块清单 | 二级音频降级 + 并发转写 |
| 5 | `subtitle/platform_adapter.py` + `bilibili_adapter.py` | §六 模块清单 | 平台无关三级字幕调度 |
| 6 | **FR-2.28** 视频开头 + 末尾双截图 + 系统级菜单栏/任务栏时间(**强需求**) | §二 FR 表 | 用户要求:同步 + 前置,系统级截图(非浏览器扩展),用户可继续操作其他 APP |
| 7 | **FR-2.28.2a** 截图前抢焦点 `prepare_for_screenshot()`(bring_to_front + focus + 窗口归位) | §二 FR 表 / §六 模块清单 | 系统截图截到前台 APP 视频而非其他 APP 画面 |
| 8 | **FR-2.28.2b** 截图后不主动切回原前台 APP | §二 FR 表 | 避免记录原前台 race condition + 不需要新 TCC 权限 |
| 9 | **FR-2.28.2c** `vla doctor` 验证 `requestFullscreen()` | §二 FR 表 / §十一 AC-11 | 提前暴露 fullscreen 权限问题,无需等处理第一个视频 |
| 10 | **FR-2.28.2d** B 级通知"准备截图,请稍候"(用户已 review 接受) | §二 FR 表 | 提示用户"屏幕会短暂跳到浏览器",避免惊吓 |
| 11 | **FR-2.28.2e** 截图索引 `index.jsonl`(含 duration_estimate 校验) | §二 FR 表 / §十一 AC-10 | 审计 + 用户事后回看证据 |
| 12 | **FR-2.29** 截图嵌入笔记(P1 可选,默认 false) | §二 FR 表 / §六 模块清单 | 用户在 Obsidian 里点开截图,辅助复习 |
| 13 | `capture/screen_capture.py` + `capture/pre_screenshot.py` | §六 模块清单 | 跨平台系统截图(macOS screencapture / Windows PowerShell)+ 末尾 30s 监听 |

#### 2026-09-03 重构 v3.1 — FR-3 串接字幕语义清理(对齐代码)

**背景**:FR-2.15c 已定义 Level 1 本地 + Level 4 云端字幕语义清理规格,但 **FR-3 表 / `transcribe/streaming.py` 模块规格 / 数据流都缺这一段**;代码已实现 `clean_transcript()` + `SubtitleRefiner.refine()`,SSOT 跟实现对不上。本轮把 FR-3 跟代码对齐。

**改动点**:

| # | 位置 | 改动描述 |
|---|------|----------|
| 1 | FR-3.8(新增) | 声明 **Level 1 本地清理必启用**,在转写 → 质量门控之间自动调用 `postprocess.clean_transcript()`,输出 `<bvid>.transcript.txt`(原始)+ `<bvid>.cleaned.txt`(清理后) |
| 2 | FR-3.9(新增) | 声明 **Level 4 云端 LLM 可选**,在 Level 1 之后、质量门控之前调用 `SubtitleRefiner.refine()`(默认 `refine_enabled=false`),输出 `<bvid>.refined.txt`;失败 fallback 不抛错,退化用 `.cleaned.txt` |
| 3 | §六 `transcribe/streaming.py` | 类重写:`transcribe_audio()` 7 步流程(Whisper → 写 transcript → Level 1 → 写 cleaned → 可选 Level 4 → 写 refined → 删音频);新增 `PostProcessor` + `SubtitleRefiner` + `TranscribeResult` + `RefinementResult` 4 个类规格 |
| 4 | §七 数据流 | "单条视频完整数据流"加 Level 1/4 清理步骤,质量门控接收 `final_text`(refined 或 cleaned) |
| 5 | §二 项目目标 #3 | 已更新:"云端模型**只用于字幕语义调整+ 质量检查 + 最终总结**" |

**对齐的代码点**(用户 review 时核对):
- `src/vla/transcribe/postprocess.py::PostProcessor.clean_transcript()` ← FR-3.8 实现
- `src/vla/quality/refiner.py::SubtitleRefiner.refine()` ← FR-3.9 实现
- `src/vla/transcribe/streaming.py::StreamingTranscriber.transcribe_audio()` ← 集成调用

**新增点**:

| # | 新增内容 | 位置 | 用途 |
|---|----------|------|------|
| 14 | **FR-3.8** Level 1 本地字幕语义清理(必启用) | §二 FR 表 | 转写 → 质量门控之间必调 `clean_transcript()` |
| 15 | **FR-3.9** Level 4 云端 LLM 字幕语义调整(可选,默认关闭) | §二 FR 表 | `refine_enabled=true` 时调 `SubtitleRefiner.refine()`,失败 fallback 用 cleaned |

**关键不变量(必须始终遵守)**:

1. **Whisper 永不接收视频信号** — 只接收音频(`.wav` 来自 ffmpeg 解封装,`.webm` 来自 `chrome.tabCapture`)
2. **Whisper 永不经过麦克风 ADC**(2026-09-03 新增) — `getUserMedia({audio:true})` 在普通页面 JS 只能拿麦克风,**禁止**作为音频源
3. **Tab Audio Recorder 扩展 ID 不硬编码** — 运行时从 `chrome.management.getAll()` 动态解析
4. **不再持续录屏**(2026-09-03 修正,允许单帧截图) — 旧红线"不录屏"修正为:不再持续录屏(只抽音频);**允许截单帧 PNG**(FR-2.28)用于视频证据;`audio_raw/` 文件在 Whisper 转写 + 质量通过后立即 unlink
5. **失败兜底路径降级语义不变** — 任何路径失败 → 写 `quality_skip.csv` 或 `transcribe_fail.csv`,**不阻塞 session**,继续下一条
6. **截图必须截到系统级时间戳**(2026-09-03 强需求) — 必须用系统级 `screencapture`(macOS)/ PowerShell(Windows),**禁止**用浏览器截图扩展或 `Page.captureScreenshot`(无法截到菜单栏/任务栏)
7. **截图必须前置在录制之前**(2026-09-03 强需求) — PHASE A 开头截图 → 才启动音频录制;末尾截图与录制并发进行(不需要前置)
8. **截图是主调度同步串行步骤**(2026-09-03 新增) — `prepare_for_screenshot()` + `capture_full_screen()` 必须串行调用,**不与音频录制并发**,确保前置约束 + 抢焦点稳定性
9. **禁用 macOS 原生全屏**(2026-09-03 新增) — 必须用 `element.requestFullscreen()`(在当前 Space 内全屏,不切 Space),避免 Mission Control 切换 Space 后 `screencapture` 截不到

---

### FR-1 视频源管理

| ID     | 描述                                                      | 优先级 |
| ------ | ------------------------------------------------------- | --- |
| FR-1.1 | 支持从 `videos.yaml` 批量加载视频任务                              | P0  |
| FR-1.2 | 支持 CLI 单条处理 `--url` `--title`                           | P0  |
| FR-1.3 | 视频 URL 支持 B站(`www.bilibili.com/video/BVxxx`)及 b23.tv 短链 | P0  |
| FR-1.4 | 自动检测视频可下载性(yt-dlp simulate)                             | P0  |
| FR-1.5 | 可下载 → 走 yt-dlp -x 抽音频(FR-2.14 路径 ①);不可下载 → 走 Tab Audio Recorder(FR-2.14 路径 ② + FR-2.21 probe_status 兜底);**不录屏** | P0  |
| FR-1.6 | 下载仅取最低画质,节约磁盘                                           | P0  |

### FR-2 字幕提取(平台无关三级策略)

> **设计原则**:项目目标是支持多种视频网站(B站、公司内部学习平台、未来 YouTube 等)。
> 通过 `PlatformAdapter` 抽象实现**平台无关**,每个平台实现自己的适配器。
> 三级降级在每个平台内部独立执行。

**三级降级**(每个平台内部):

| 优先级 | 名称               | 通道                                                                                        | 输出                                 |
| --- | ---------------- | ----------------------------------------------------------------------------------------- | ---------------------------------- |
| ①   | 平台 API           | 平台公开 API(httpx)                                                                           | `SubtitleResult(source="api")`     |
| ②   | Puppeteer 通用浏览器  | 用户 Chrome `--remote-debugging-port`                                                       | `SubtitleResult(source="browser")` |
| ③   | 音频二级降级 + Whisper | ① `yt-dlp -x` 抽音频 → ② Tab Audio Recorder 扩展(`chrome.tabCapture` 纯净音频) → faster-whisper`(2026-09-03 砍掉 Puppeteer 流式路径,见 FR-0) | `SubtitleResult(source="whisper")` |

**降级规则**:① miss → ②;② miss → ③;③ fail → `None`(走 transcribe_fail 记录)。

| ID       | 描述                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | 优先级 |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --- |
| FR-2.0   | **平台适配器抽象**:`PlatformAdapter` Protocol 含 `match(url)` + 3 个 fetch 方法;每个平台实现一个 adapter                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | P0  |
| FR-2.1   | **策略 ①**(B站):B站官方 CC 字幕 API,httpx 调 `api.bilibili.com/x/player/v2`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | P0  |
| FR-2.2   | **Puppeteer 连接**:用 playwright `connect_over_cdp("http://localhost:9222")` 连用户 Chrome,**复用用户登录态**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | P0  |
| FR-2.3   | **后台标签页**:`context.new_page()` 创建后台标签页,**不抢用户焦点**;完成即 `page.close()`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | P0  |
| FR-2.4   | **策略 ②**(通用):Puppeteer 通用 JS 探测,**4 种方法按优先级尝试**,首个命中即返回                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | P0  |
| FR-2.5   | **JS 探测方法 1**:HTML5 `<track kind="subtitles" / kind="captions">` 标签,拿 `src` 下载解析                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | P0  |
| FR-2.6   | **JS 探测方法 2**:`window.__INITIAL_STATE__` / `window.__INITIAL_DATA__` 递归找字幕 URL 字段                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | P0  |
| FR-2.7   | **JS 探测方法 3**:`window.player.getSubtitle()` / `window.player.subtitle` / `window.player.on('subtitle_update')`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | P1  |
| FR-2.8   | **JS 探测方法 4**:DOM 选择器扫描字幕文本(`[class*="subtitle"]`、`[class*="caption"]` 等)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | P2  |
| FR-2.9   | **B站语言优先级**:`zh-Hans > zh-CN > zh-Hant > en-US > en > ai-zh`;`ai-zh` 是 B站 AI 实时字幕,质量次于官方 CC                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | P0  |
| FR-2.10  | **跨域处理**:`page.evaluate(fetch)` 只用于同 origin(B站 page 取 page 自身 API);跨 origin 字幕 URL 用 `context.request.get()`(走浏览器 network stack,带 cookie,无 CORS 限制)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | P0  |
| FR-2.11  | **字幕格式统一**:无论 API 返回什么,统一 dump 成 `.srt` 后用 `pysrt` 解析;Puppeteer 取数据时已是 `.srt` / `.vtt` / `.json` / `.ass` 之一                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | P0  |
| FR-2.12  | **字幕来源记录**:metadata.source `api` / `browser` / `whisper`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | P0  |
| FR-2.13  | **失败日志区分**:策略 ② miss 不记 transcribe_fail(走下一级);策略 ③ Whisper 失败才记 transcribe_fail.csv                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | P0  |
| FR-2.14  | **策略 ③ 音频二级降级总览**(2026-09-03 重构 v3,方案 A 落地):**不再录屏 / 不再 Puppeteer 流式录音频**,Whisper 只用音频。**为什么砍掉路径 ② Puppeteer**:`navigator.mediaDevices.getUserMedia({audio: true})` 在普通页面 JS 里**只能拿到麦克风**,拿不到视频本身的音频(那是 `chrome.tabCapture` 专属 API,普通网页无权限);麦克风录音会带环境噪音 + 通知声 + 用户语音 → Whisper 转写准确率明显下降,且需 macOS 麦克风授权 → **移除**。二级路径在 `PlatformAdapter.fetch_via_recording(driver, url, duration_sec)` 内独立执行,首个成功即返回;**全部失败**才记 `transcribe_fail.csv`(FR-2.13)。二级路径:① **`yt-dlp -x` 抽音频**(FR-2.16a)— 视频可下载时(FR-1.4 simulate 通过)用 `yt-dlp -x --audio-format wav --postprocessor-args "-ac 1 -ar 16000" -o <stem>.wav <url>`,零本地编码、零 Chrome 依赖、磁盘 ≈60 MB/h、`chrome.tabCapture` 级别的纯净音频(直接走 ffmpeg 解封装);② **Tab Audio Recorder 扩展**(FR-2.16c)— 路径 ① 失败时,扩展 **不硬编码 ID**,运行时 `TabAudioRecorder._resolve_ext_id()` 从 `chrome.management.getAll()` 遍历 + 匹配 name/description(FR-2.24);URL 模板 `chrome-extension://<ext_id>/editor.html?id=<audio_id>`,audio_id 由扩展分配;**前置探测** `probe_status()`(FR-2.24a)三态分支(enabled/disabled/not_installed)决定直接录还是通知兜底 + `quality_skip.csv`(FR-2.21)。**关键不变量**:Whisper 永不接收视频信号,只接收音频(`.wav`/`.webm` 都来自 ffmpeg 解封装或 `chrome.tabCapture`,**不经过麦克风 ADC**);视频源不缓存,转写完立即删(FR-2.22);失败日志上限弹窗(FR-6.6)覆盖两条路径的 transcribe_fail                                                 | P0  |
| FR-2.15  | **Tab Audio Recorder 触发 + 编辑器 URL**(2026-09-03 重构 v2):扩展 ID **不固定**,运行时 `TabAudioRecorder._resolve_ext_id()`(FR-2.24)从 `chrome.management.getAll()` 遍历 chrome-extension 列表,匹配规则:`name.toLowerCase().includes("tab audio")` OR `description.toLowerCase().includes("tab audio")`,匹配到第一个即用;匹配不到 → 抛 `ExtensionNotFoundError`,`SubtitleStrategy` 捕获后写 `quality_skip.csv`。配置在 `config/vla.yaml` 的 `extension.tab_audio_recorder.match_keyword`(默认 `"tab audio"`,可改),用户也可在 `vla doctor` 命令里指定其他关键词(防止扩展改名)。**触发方式**:在动态解析到的 background page 上跑 evaluate JS 启动录制(`FR-2.24 触发器` 实现),**不依赖 hotkey**(Tab Audio Recorder 无 `chrome.commands`、macOS TCC 拦截 `Input.dispatchKeyEvent`、CDP 键盘事件对扩展 chrome.commands 无效)。**音频 ID 获取**:扩展内部启动录制后,通过 background page evaluate 读 `window.__last_audio_id` 或解析 `<ext_url>/editor.html?id=<audio_id>` URL(扩展跳转到此页面作为录制完成标志),用正则 `id=(\d+)` 提取 audio_id。**录制时长**:`duration_sec` 由调用方传入,后台 service worker 自己计时 stop;Agent 端用 `asyncio.sleep(duration_sec + post_buffer_sec=30)` 轮询 editor.html 是否就绪。**关键设计**:audio_id 是本地文件命名 + 转写队列 key(FR-2.26/2.27),全程不依赖视频画面 | P0  |
| FR-2.16  | **策略 ③ 音频输入**(2026-09-03 重构 v3,方案 A):二级降级路径详见 FR-2.14;路径 ① yt-dlp 输出 `.wav`、路径 ② Tab Audio Recorder 输出 `.webm`(opus 编码),两者均直接送 `faster-whisper` 转写(无需 ffmpeg 重抽);**Whisper 永不接收视频信号,永不经过麦克风 ADC**(2026-09-03 砍掉 Puppeteer 流式路径后的新不变量);`AudioTranscriber` Protocol 是注入点,Phase 4 接 `WhisperTranscriber`,FR-2.27 worker 池并发处理 | P0  |
| FR-2.17  | **BilibiliAdapter**:实现 FR-2.1/2.2/2.4,`match` 域名匹配 `bilibili.com` / `b23.tv`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | P0  |
| FR-2.18  | **InternalSiteAdapter**(占位):接口留 stub,等公司下发账号后实现 `match` 域名 + fetch 方法;当前抛 `NotImplementedError`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | P1  |
| FR-2.19  | **通用 fallback adapter**:未知 URL 域名时(非 B 站、非 internal site),跳过策略 ①,直接走 ② Tab Audio Recorder(FR-2.14/2.21);yt-dlp 对未知站点大概率 simulate 失败,直接进入降级链                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | P1  |
| FR-2.20  | **降级路径**:任一策略失败都降级到下一级,不跳过当前视频;仅 ③ 失败才算"字幕提取失败"                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | P0  |
| FR-2.21  | **策略 ③ 自动探测 + 通知兜底(方案 C,2026-09-03 重构 v2)**:路径 ① yt-dlp miss 后,**不再弹 A 级阻塞 dialog**(方案 C)。新流程:`TabAudioRecorder.probe_status(browser) -> Literal["enabled", "disabled", "not_installed"]`(FR-2.24a)→ 三态分支处理:**`enabled`** → 直接调 `TabAudioRecorder.start_recording(driver, url, duration_sec)` 拿 audio_id(FR-2.24),无需用户响应;**`disabled`**(扩展装了但被关) → `MacOSNotifier.info("需要启用 Tab Audio Recorder", "请在 Chrome 工具栏启用 → 下次运行自动生效")` B 级通知 + 写 `quality_skip.csv`(不阻塞,继续下一个视频);**`not_installed`**(扩展未找到) → `MacOSNotifier.warning("Tab Audio Recorder 未安装", "请从 Chrome Web Store 安装并启用")` B 级通知 + 写 `quality_skip.csv`。**无状态设计(每次即时探测)**:与旧版 `PluginStatus` 单例不同,每次调用都重新探测(扩展状态可在 Chrome 设置里随时改);探测耗时 ~100ms(一次 `chrome.management.getAll()` 调用),可接受。**降级语义**:Tab Audio Recorder 路径本身失败时(扩展无响应 / audio_id 拿不到 / 文件超时未落地)→ 写 `quality_skip.csv` + log warning,**不记 transcribe_fail**(whisper 还没启动)。**Session 行为**:不阻塞 session,优雅降级,用户后续可在 Chrome 启用扩展后下次自动生效                                                                                                                                                  | P0  |
| FR-2.22  | **音频文件清理**(2026-09-03 重构 v3,方案 A):路径 ① yt-dlp `.wav`、路径 ② Tab Audio Recorder `.webm` 二者**统一管理**(2026-09-03 砍掉 Puppeteer 流式路径后)。**主路径**:Whisper 转写完成且通过质量门控(FR-4)后由 `StreamingTranscriber` 立即 `audio_path.unlink()`,**绝不保留**;**Whisper 失败 / 质量不过关**:`logs/audio_raw/<audio_id>.webm` 或 `<bvid>.wav` 文件保留 24h 后由 `log/failure_alert.py` 后台清理线程删除(`max_fail_keep_hours=24`,可配置);**全路径 `transcribe_fail` 时**:文件进 `logs/audio_failed/` 永久保留供排查。**禁用录屏文件路径**:旧 `BrowserRecorder` 的 mp4 视频源清理逻辑整段删除;新增 `audio_raw/` + `audio_failed/` 目录结构。**并发 worker 池**:Whisper 转写 worker 池(FR-2.27)处理多条音频,每条独立 `audio_path`,由 worker 各自 unlink                                                                                                                                                                                                                                                                                                                                                                                          | P0  |
| FR-2.15c | **字幕语义清理**(2026-09-02 新增,可选):faster-whisper 转写输出常见 4 类质量问题:① 繁简混排(台湾口音→繁体,大陆→简体)② 同音字错字(如"Deep Sake"应为"Deep Seek",需视频标题辅助)③ 短碎片(单字"了"、"呢")④ 语序错位(Whisper 时序问题)。**两级清理策略**:**Level 1(必启用,本地)** — `transcribe/postprocess.py` 提供 `clean_transcript()`: ① `merge_short_lines()` 把 < `whisper.postprocess_min_line_chars`(默认 8)的行并入上一行;② `dedupe_repeated_segments()` 用 LCS-style 公共子串检测(LCS 长度长度 ≥ `whisper.postprocess_min_overlap_chars`,默认 6)去重 B站自动 CC 叠音段;阈值由 `whisper.postprocess_min/max_line_chars / min_overlap_chars` 配置。**Level 4(可选,云端 LLM)** — `quality/refiner.py` 提供 `SubtitleRefiner`: 仅当 `quality_check.refine_enabled=true` 才调云端 LLM(默认 False,因为云端 API 花钱),用 `quality_check.refine_model`(None 时 fallback `quality_check.model`)做语义整理:繁简统一 + 同音字修正 + 碎片合并 + 段落切分(用 `\n\n` 分隔)。输入 token 上限 `quality_check.refine_max_chars`(默认 **6000**,超出跳过 LLM 只用本地清理),LLM 返回 `{cleaned_text, corrections[], notes}`。**失败 fallback**: LLL 抛错 / / 解析失败 / / 空响应 → 返回 `RefinementResult(cleaned_text=原始text, corrections=[], notes=失败原因)`,不抛错(主流程不中断)。**落盘**: 写到 `<stem>.cleaned.txt`(与 `.transcript.txt` 同目录),头部加 `cleaned_at / model / notes / corrections` 元数据,不覆盖原文(用户/审计可对比)。**配额归类**:归入"字幕质量检查"云端配额(NFR-5 第 ③) | P1  |
| FR-2.24a | **Tab Audio Recorder 状态探测 `probe_status()`**(2026-09-03 新增):Python 函数,封装在 `src/vla/subtitle/tab_audio_recorder.py`。**签名**:`async def probe_status(browser: Browser) -> Literal["enabled", "disabled", "not_installed"]`。**实现**:`async def probe_status`:① 在 browser 上 evaluate `chrome.management.getAll(extensions => resolve(extensions))`;② 遍历结果,匹配 `ext.name.toLowerCase().includes(match_keyword)` 或 `ext.description.toLowerCase().includes(match_keyword)`(`match_keyword` 来自 `config/vla.yaml:extension.tab_audio_recorder.match_keyword`,默认 `"tab audio"`);③ 找到:`enabled = ext.enabled`(chrome.management 字段),返回 `"enabled"` 或 `"disabled"`;④ 没找到:返回 `"not_installed"`。**性能**:`chrome.management.getAll` 是浏览器级 API,不依赖页面焦点,~50ms 内完成;探测失败(timeout / permission denied) → 返回 `"not_installed"`。**关键设计**:探测调用在每次策略 ③ 触发时执行(频率极低,仅 5-15% 视频走路径 ③),不做缓存避免扩展状态变更后探测失真                                                                                                                                                                                                                 | P0  |
| FR-2.24  | **Tab Audio Recorder 触发器 `TabAudioRecorder`**(2026-09-03 重构 v2):Python 类,封装在 `src/vla/subtitle/tab_audio_recorder.py`。**核心方法**:`start_recording(driver, url, duration_sec) -> str` 返回 audio_id;`_resolve_ext_id(browser) -> str`(动态从 `chrome.management.getAll()` 匹配,**不硬编码**)。**实现**:`async def start_recording`:① `_resolve_ext_id` → 拿到 ext_id;② 找到扩展 background page(`browser.targets()` 遍历 `chrome-extension://<ext_id>/_generated_background_page.html`,如未打开则 `ctx.new_page().goto()`);③ 在 bg page 上 evaluate `startTabRecording()` 或等价函数(扩展内部暴露的全局函数名,可通过 DOM 探针反查);④ 轮询 `bg_page.url` 直到变成 `chrome-extension://<ext_id>/editor.html?id=(\d+)`(扩展跳转到此页 = 录制完成);⑤ 用正则提取 `id=(\d+)`,返回 audio_id。**`_resolve_ext_id` 实现**:`async def _resolve_ext_id` 调 `probe_status` 拿到 enabled 状态的扩展 ID(扩展对象自带 `id` 字段);找不到抛 `ExtensionNotFoundError`。**异常**:扩展无响应 / 跳转失败 → 抛 `RecorderTriggerError`,`SubtitleStrategy` 捕获后降级到 `quality_skip`。**关键设计**:Tab Audio Recorder 自身完成 stop + 跳转到 editor.html,所以**不需要 Agent 主动 stop**(避免抢焦)                                                                | P0  |
| FR-2.25  | **编辑器页面下载按钮 `DownloadButtonClicker`**(2026-09-03 重构 v2):Python 类,封装在 `src/vla/subtitle/tab_audio_recorder.py`。**核心方法**:`click_download(driver, audio_id, ext_id, save_dir, timeout_sec=180) -> Path`。**实现**:`async def click_download`:① `ctx.new_page().goto(f"chrome-extension://{ext_id}/editor.html?id={audio_id}")`(直接打开,跳过扩展自动跳转的等待);② 用 Playwright CDP 监听 `Browser.download` 事件(`context.on("download", ...)`);③ 在 editor.html 内 evaluate 找下载按钮(候选 selector:`button:has-text("Download")`, `button:has-text("保存")`, `#download-btn`, `[data-action="download"]`),点击;④ 等 download 事件触发,`download.save_as(save_dir / f"{audio_id}.webm")`(FR-2.26 命名规范);⑤ 超时 → 抛 `DownloadTimeoutError`,文件留 `audio_failed/`。**关键**:必须 `page.on("download")` **先注册再点按钮**,否则事件丢失。**调用顺序**:由 `TabAudioRecorder.start_recording` 返回 audio_id 后,主调度拿到 ext_id + audio_id 调 `click_download`(不需要再解析一次 ID)                                                                                                                                                                                                   | P0  |
| FR-2.27  | **异步音频队列 + Whisper worker 池**(2026-09-03 新增):多视频并行处理时,每条独立分配 audio_id,各自的下载 → 转写 → 质量门控链路**异步并发**。**核心组件**:`src/vla/audio/queue.py` 的 `AudioQueue`(asyncio.Queue,容量 10,满则阻塞避免内存爆);`src/vla/audio/worker_pool.py` 的 `WhisperWorkerPool`,默认 2 个 worker(`whisper.concurrent_workers`,Apple Silicon GPU 单卡上限,可配置)。**流程**:① 主调度把 `(audio_id, audio_path, video_meta)` 入队;② worker 从队列拿 → 调 `StreamingTranscriber.transcribe(audio_path)`;③ 转写完 → 质量门控 → 成功删源文件 + 累计时长(FR-9);失败 → 留 `audio_failed/` + 记 fail 日志。**并发安全**:同一 audio_id 不会被两个 worker 同时处理(queue 自身保证);`Quota` 累计原子操作(`asyncio.Lock`);`probe_status` 无状态(每次调用即时探测,无单例、无锁)。**降级**:worker 池满 → 主调度 await queue.put,自动限流不爆内存                                                                                                                                                                                                                                                                                                                                                                              | P0  |
| FR-2.26  | **音频文件命名 + 本地路径规范**(2026-09-03 重构 v3,方案 A):`logs/audio_raw/<audio_id>.webm`(Tab Audio Recorder 路径);`logs/audio_raw/<bvid>.wav`(yt-dlp 路径)。audio_id 是 Tab Audio Recorder 分配的纯数字字符串(来自 editor.html URL 的 `?id=` 参数),bvid 是 B站视频 ID(BV1xxx)。**目录生命周期**:`audio_raw/` 文件在 Whisper 转写 + 质量通过后立即删(FR-2.22);`audio_failed/` 是失败文件的永久归档,按 `audio_id/bvid` 分目录,每周归档一次(避免单目录文件过多)。**2026-09-03 砍掉**:`<bvid>_<timestamp>.webm` Puppeteer 流式命名(已不适用)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | P0  |
| FR-2.28  | **视频开头 + 末尾双截图**(2026-09-03 新增,**强需求**):每条走策略 ③ Whisper 的视频截 2 张 PNG,**必须截到 macOS 菜单栏时间 / Windows 任务栏时间**。**触发条件**:`screenshot.enabled=true` + 视频走策略 ③(Whisper)。**PHASE A 开头截图(前置硬约束)**:`page.goto(url)` → `evaluate(video.currentTime=0, video.pause(), video.requestFullscreen())` → `await sleep(2)` 等全屏动画 → `prepare_for_screenshot(page)`(详见 FR-2.28.2a) → 同步系统截图 → **此时才启动音频录制**(FR-2.28.2b 前置)。**PHASE B 后台监听**:asyncio task 每 1s poll `video.currentTime`。**PHASE C 末尾截图(同步,非前置 — 录制仍在进行)**:到达 `currentTime ≥ duration_sec - 33`(留 3s buffer)→ `prepare_for_screenshot(page)` → `evaluate(video.pause(), video.currentTime=duration_sec-30)` → `await sleep(0.5)` → 同步系统截图 → `evaluate(video.play())` 恢复 → 视频继续播到结束。**平台适配**:`src/vla/capture/screen_capture.py::capture_full_screen(save_path)`,macOS 走 `screencapture -x`(隐藏鼠标 + PNG 输出,~0.3-0.5s,**需屏幕录制 TCC 权限**),Windows 走 PowerShell + System.Drawing(PrimaryScreen 全屏复制 ~1.5-3s,无需特殊权限)。**关键不变量**:截图系统是主调度**同步串行步骤**,**不与音频录制并发**(确保前置约束);暂停 1-2s 期间音频录制不停(只视频暂停);末尾截图不"前置"在录制之前 — 它跟录制并发进行。**失败降级**:任一截图失败(权限拒 / 超时 / 抢焦点失败)→ 记 log warning + 跳过该张,**不阻塞**音频录制或视频播放;partial 截图(只有菜单栏时间无视频画面)仍保留供审计 | P0  |
| FR-2.28.2a | **截图前准备 `prepare_for_screenshot(page)`**(FR-2.28 子项,2026-09-03 新增):**强需求**。截图前必须抢焦点 + 窗口归位,否则 `screencapture` 会截到前台 APP 画面而非视频。**实现**:`async def prepare_for_screenshot(page)`:① `await page.bring_to_front()`(Puppeteer 拉 tab 到前台);② `await page.evaluate("window.focus()")`(JS 端保险 focus);③ `await page.evaluate("window.moveTo(0, 0); window.resizeTo(screen.width, screen.height);")`(防多显示器 / 窗口最小化,强制主显示器全屏);④ `await asyncio.sleep(0.3)`(等窗口切换动画稳定)。**关键**:**禁用 macOS 原生全屏**(F11 / 绿点按钮),必须用 `element.requestFullscreen()`(JS API,在当前 Space 内全屏,不切 Space,避免 Mission Control 切换 Space 后截不到)。**失败兜底**:抢焦点失败 → 仍尝试截图,标记 `partial=menu_bar_only`(只截到菜单栏时间,无视频画面) | P0  |
| FR-2.28.2b | **截图后行为**:截图完成后**不主动切回原前台 APP**(避免记录原前台状态的 race condition + 不需要新 TCC 权限);用户可手动 alt+tab 回到原 APP;音频录制在后台继续,不影响用户操作 | P0  |
| FR-2.28.2c | **`vla doctor` 验证 `requestFullscreen()`**:session 内首次 `element.requestFullscreen()` 浏览器会弹"按 Esc 退出"提示(用户必须确认);在 `vla doctor` 阶段预热一次(创建一个临时 `<video>` 元素 + 调 `requestFullscreen()`),验证返回值 Promise 不报错,**提前暴露权限问题**(无需等到处理第一个视频才发现) | P0  |
| FR-2.28.2d | **B 级通知"准备截图,请稍候"**:截图前 0.5s 调用 `MacOSNotifier.info("准备截图", f"将截取 {bvid} 的开头/末尾画面")`,提示用户"屏幕会短暂跳到浏览器"。**为什么必要**:无通知 → 用户可能在用其他 APP,突然看到屏幕跳到浏览器会以为电脑出 bug。**用户接受确认**:本 FR 已通过用户 review | P0  |
| FR-2.28.2e | **截图命名 + 索引**:`logs/screenshots/<bvid>_<unix_ts>_start.png` + `logs/screenshots/<bvid>_<unix_ts>_end.png`;同时写 `logs/screenshots/index.jsonl`(每行 `{bvid, start_ts, end_ts, duration_estimate, partial_flags}`),`end_ts - start_ts` 应 ≈ `duration_sec`(允许 ±5s 误差),用于审计 + 用户事后回看证据 | P0  |
| FR-2.29  | **截图嵌入笔记**(2026-09-03 新增,**P1 可选**):质量门控通过的截图(FR-4 + FR-2.28)→ 在生成 `notes.md` 时插入 Obsidian 嵌入引用 `![[screenshots/<bvid>_<ts>_start.png]]` + `![[screenshots/<bvid>_<ts>_end.png]]`(用户可点击打开)。**实现**:在 `LLMSummarizer.summarize_batch` 输出 Markdown 头部插入截图引用块(在总结内容之前)。**失败语义**:截图缺失(FR-2.28 降级后无文件)→ 跳过嵌入,不报错。**配置**:`screenshot.embed_in_notes: true`(默认 false,P1 可选) | P1  |

**架构图**:

```text
字幕提取入口 (Phase 8 main scheduler)
  │
  ├─ PlatformAdapterRegistry.match(url) → adapter
  │    ├─ BilibiliAdapter         (P0)
  │    └─ InternalSiteAdapter     (P1, stub)
  │
  ├─ ① adapter.fetch_api_subtitle(url)
  │    ├─ BilibiliAdapter: httpx → api.bilibili.com
  │    └─ InternalSiteAdapter: NotImplementedError
  │    └─ 命中 → SubtitleResult(source="api")
  │
  ├─ ② adapter.fetch_browser_subtitle(driver, url)
  │    ├─ driver.connect_over_cdp(localhost:9222)
  │    ├─ context.new_page()  # 后台标签页,不抢焦点
  │    ├─ page.goto(url), wait_for_player_ready()
  │    ├─ 4 种 JS 探测(按优先级):
  │    │    1. <track kind="subtitles"> → 拿 src
  │    │    2. window.__INITIAL_STATE__ 找 subtitle url
  │    │    3. window.player.getSubtitle() / .subtitle
  │    │    4. DOM 选择器扫描字幕文本
  │    ├─ 跨 origin 用 context.request.get(url)
  │    └─ 命中 → SubtitleResult(source="browser")
  │
  └─ ③ adapter.fetch_via_recording(driver, url, duration_sec)  # 音频二级降级(FR-2.14,2026-09-03 砍掉 Puppeteer 流式后)
       │
       ├─ ① yt-dlp -x 抽音频(FR-2.16a)
       │    ├─ yt-dlp --simulate 判定可下载?(FR-1.4)
       │    │    ├─ YES → yt-dlp -x --audio-format wav <url> → logs/audio_raw/<bvid>.wav
       │    │    └─ NO  → 失败,降级
       │    └─ faster-whisper 直读 wav(无需 ffmpeg 重抽) → SubtitleResult(source="whisper")
       │
       └─ ② Tab Audio Recorder 扩展(FR-2.16c,运行时 `TabAudioRecorder._resolve_ext_id()` 动态从 `chrome.management.getAll()` 匹配,不硬编码 ID)
            ├─ probe_status(browser) 三态探测(FR-2.24a)
            │    ├─ enabled        → 直接录制,无需用户响应
            │    ├─ disabled       → MacOSNotifier.info B 级通知 + quality_skip.csv(FR-2.21)
            │    └─ not_installed  → MacOSNotifier.warning B 级通知 + quality_skip.csv(FR-2.21)
            ├─ TabAudioRecorder.start_recording(driver, url, duration_sec) → audio_id
            │    ├─ bg page evaluate 启动录制(FR-2.24)
            │    └─ 轮询 bg_page.url 直到 editor.html?id=<audio_id>(扩展完成标志)
            ├─ DownloadButtonClicker.click_download(driver, audio_id, save_dir, timeout_sec=180)
            │    ├─ 新 page.goto editor.html?id=<audio_id>(FR-2.25)
            │    ├─ context.on("download", ...) 先注册
            │    └─ 点下载按钮 → download.save_as(logs/audio_raw/<audio_id>.webm)
            ├─ audio_id push 到 AudioQueue → WhisperWorkerPool worker 并发转写(FR-2.27)
            └─ faster-whisper 直读 webm → SubtitleResult(source="whisper", metadata={"via": "tab_audio_recorder"})
```

**关键**:Whisper **永不接收视频信号**,三个路径都只把音频(`.wav` 或 `.webm`)送 Whisper。

**PlatformAdapter 接口**(代码契约):

```python
class PlatformAdapter(Protocol):
    """所有视频平台字幕适配器都要实现这个接口。"""

    @classmethod
    def match(cls, url: str) -> bool: ...

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """策略 ①:平台公开 API。同 origin httpx 调用。"""

    def fetch_browser_subtitle(self, driver, url: str) -> tuple[str, dict] | None:
        """策略 ②:Puppeteer 通用 JS 探测。driver 是 playwright Browser 实例。"""

    def fetch_via_recording(self, driver, url: str, duration_sec: int) -> tuple[str, dict] | None:
        """策略 ③:音频二级降级(yt-dlp → Tab Audio Recorder) + faster-whisper。**不录屏 / 不麦克风**(2026-09-03 重构 v3,方案 A)。"""
```

**已验证 spike**(2026-09-01,`scripts/spike_browser_subtitle.py`):

| 项                                                      | 结果                                            |
| ------------------------------------------------------ | --------------------------------------------- |
| `playwright.connect_over_cdp("http://localhost:9222")` | ✅ 通(独立 user-data-dir=`/tmp/vla-chrome-debug`) |
| `page.goto(B站 URL)` 后台标签页                              | ✅ 不抢焦点                                        |
| `page.evaluate(fetch player/v2)`                       | ✅ 拿到 `subtitles count=1`                      |
| `context.request.get(subtitle_url)`                    | ✅ status 200,跨 origin 通过                      |
| body[] 长度                                              | 1143 条中文 AI 字幕(`ai-zh`)                       |
| dump 到 `.srt`                                          | ✅ 72947 bytes / 4571 行                        |
| 独立 profile 没 B站登录                                      | ⚠️ 字幕是 `ai-zh`(AI 实时字幕)非官方 CC;但 spike 验证了通道   |

**安全性约束**(沿用):
- 字幕永远本地(策略 ①② 完全本地调用,策略 ③ 完全本地 Whisper)
- 不引入云端转写
- 云端 LLM 仅用于:① 字幕质量检查(FR-4) ② 6h 批量总结(FR-5) ③ 字幕语义清理(FR-2.15c,2026-09-02 新增,可选 — 仅当本地清理结果仍不达预期时启用)

**Phase 3 代码改动**:
- `subtitle/bilibili_official.py`:保留,作为 `BilibiliAdapter.fetch_api_subtitle` 实现
- `subtitle/browser_plugin.py`:废弃(原"扫描 VideoTrans 目录"设计作废),仅保留 `parse()` 方法给 Puppeteer 取到字幕文件时用
- `subtitle/strategy.py`:重写,从"扫描 + 弹窗"改为"adapter 三级降级"
- 新增 `subtitle/platform_adapter.py`:Protocol + Registry
- 新增 `subtitle/bilibili_adapter.py`:BilibiliAdapter 实现
- 新增 `subtitle/internal_site_adapter.py`:InternalSiteAdapter stub
- 新增 `subtitle/browser_driver.py`:Puppeteer driver + 通用 JS 探测
- 新增 `subtitle/tab_audio_recorder.py`:Tab Audio Recorder 触发器 + 下载按钮 + `probe_status` 三态探测 + `_resolve_ext_id` 动态 ext_id(FR-2.21/2.24/2.24a/2.25)
- 新增 `audio/source_factory.py`:音频源工厂,封装 yt-dlp -x 抽音频(路径 ①)
- 新增 `audio/queue.py`:AudioQueue(asyncio.Queue,容量 10)
- 新增 `audio/worker_pool.py`:WhisperWorkerPool(默认 2 worker)

### FR-3 流式转写与磁盘管理

| ID     | 描述                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 优先级 |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --- |
| FR-3.1 | Whisper 引擎使用 **faster-whisper**(开源本地)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | P0  |
| FR-3.2 | 模型可选 `tiny/base/small/medium/large-v3`,默认 `small`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | P0  |
| FR-3.3 | 边转写边清理:音频就绪后立即删除视频源(.webm/.mp4),`.wav` 音频文件保留(由 FR-3.7 控制何时清理)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | P0  |
| FR-3.4 | 磁盘峰值占用 ≤ 1 GB(远低于 256 GB 总容量)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | P0  |
| FR-3.5 | 转写失败必须记录到 `transcribe_fail.csv`,**不删除**视频源                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | P0  |
| FR-3.7 | **音频清理策略**(2026-09 定):`StreamingTranscriber.transcribe()` 只删视频源(FR-3.3);音频 `.wav` 保留到 `save_dir`,由调用方按质量结果决定 — **质量通过**(FR-4.5)→ 调 `StreamingTranscriber.cleanup(audio_path)` 删 `.wav`;**质量失败**(FR-4.6)→ 保留 `.wav` 供重转写。理由:audio 是唯一可重转写的源,失败时不能丢                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | P0  |
| FR-3.8 | **Level 1 本地字幕语义清理**(2026-09-03 新增,必启用):faster-whisper 转写输出常见 4 类问题(繁简混排 / 同音字错字 / 短碎片 / 语序错位),`transcribe/postprocess.py` 提供 `clean_transcript()`(**注意**:详细规格见 FR-2.15c,**FR-3.8 仅声明必须在转写 → 质量门控之间调用**)— `merge_short_lines()` 把 < `whisper.postprocess_min_line_chars`(默认 8)的行并入上一行 + `dedupe_repeated_segments()` 用 LCS 检测去重 B站自动 CC 叠音段。**输出文件**:`logs/transcripts/<bvid>.transcript.txt`(Whisper 原始,保留)+ `logs/transcripts/<bvid>.cleaned.txt`(Level 1 清理后,**质量门控 + 总结都用这份**)。**触发**:`StreamingTranscriber.transcribe()` 完成后**自动调用** `clean_transcript()`,无需主调度介入                                                                                                                                                                                                                                                                          | P0  |
| FR-3.9 | **Level 4 云端 LLM 字幕语义调整**(2026-09-03 新增,可选,**代码已实现,需对齐 SSOT**):当 `quality_check.refine_enabled=true` 时,在 Level 1 清理之后、调 `QualityChecker.check()` **之前**,调云端 LLM 做语义整理:繁简统一 + 同音字修正 + 碎片合并 + 段落切分(`\n\n` 分隔)。**模块**:`quality/refiner.py` 的 `SubtitleRefiner.refine(text, title) -> RefinementResult`。**模型**:用 `quality_check.refine_model`(None 时 fallback `quality_check.model`)。**输入上限**:`quality_check.refine_max_chars`(默认 **6000**,超出跳过 LLM 只用 Level 1)。**输出文件**:`logs/transcripts/<bvid>.refined.txt`(LLM 调整后,**质量门控 + 总结用这份而非 `.cleaned.txt`**)+ `<bvid>.cleaned.txt` 保留供审计对比。**失败 fallback**:LLM 抛错 / 解析失败 / 空响应 → 返回 `RefinementResult(cleaned_text=原 cleaned_text, corrections=[], notes=失败原因)`,**不抛错**(主流程不中断),退化用 `.cleaned.txt`。**配额归类**:归入"字幕质量检查"云端配额(NFR-5 第 ③)。**关键设计**:LLM 调用在 Whisper 完成 + Level 1 清理后立即执行,延迟 ~1-3s,不影响整体转写时长 | P1  |

### FR-4 质量门控

| ID     | 描述                                               | 优先级 |
| ------ | ------------------------------------------------ | --- |
| FR-4.1 | 字幕转写后调用**云端订阅模型**做质量检查                           | P0  |
| FR-4.2 | 检查项:通顺度、完整性、准确性、重复异常                             | P0  |
| FR-4.3 | 启发式预筛:语速 < 1 字/秒 或 > 15 字/秒直接判失败                 | P0  |
| FR-4.4 | 质量分 ≥ 70 才算通过                                    | P0  |
| FR-4.5 | **通过**(2026-09 收敛)→ 视频源已删(FR-3.3)+ **保存原文**到 `logs/transcribed/<id>_<title短>.txt`(FR-7.7)+ **清理音频 .wav**(FR-3.7)+ 进入总结队列(Phase 7) | P0 |
| FR-4.6 | **未通过**(2026-09 收敛)→ 保留音频 .wav(FR-3.7,虽然视频源 FR-3.3 已删,音频是唯一可重转写的源)+ 记录到 `logs/quality_fail.csv`(FR-7.2)+ **单独存文本**到 `logs/failed_texts/<id>_<title短>.txt`(FR-7.3) | P0 |

### FR-5 LLM 总结

| ID     | 描述                                             | 优先级 |
| ------ | ---------------------------------------------- | --- |
| FR-5.1 | 仅对**通过质量检查**的字幕做总结                             | P0  |
| FR-5.2 | 总结 500–800 字,Markdown 格式                       | P0  |
| FR-5.3 | 从视频标题提取知识点,跨视频合并重复                             | P1  |
| FR-5.4 | 每个视频作为标题下的子要点                                  | P0  |
| FR-5.5 | 追加到指定笔记文件,保留历史                                 | P0  |
| FR-5.6 | **总结触发条件:累计成功转写视频时长达到 6 小时**才生成一次总结(见 FR-9)    | P0  |
| FR-5.7 | 总结范围:6 小时窗口内**所有通过质量检查**的视频,统一生成一份 500-800 字总结 | P0  |

### FR-6 macOS 系统通知(分级策略)

**通知分级**(关键):

| 类型             | 触发                                         | 方式                        |
| -------------- | ------------------------------------------ | ------------------------- |
| **A. 阻塞弹窗**    | ① 启用浏览器插件(用户必须介入);② 失败日志达到上限(汇总告知)         | `display dialog` 阻塞       |
| **B. 非阻塞通知**   | 进度类(质量通过 / session 开始 / 总结触发 / session 结束) | `display notification` 横幅 |
| **C. 静默(仅日志)** | ① 转写失败 ② 质量不过关                             | **不通知**,只写 CSV + 终端 print |

| ID | 描述 | 优先级 |
|----|------|--------|
| FR-6.1 | 非阻塞通知(`display notification`)- 用于**进度类状态**(质量通过 / session 开始 / 总结触发) | P0 |
| FR-6.2 | 阻塞式弹窗(`display dialog`)- 用于**必须用户介入**或**累计告警** | P0 |
| FR-6.3 | **弹窗触发场景**(只两类):① 启用浏览器插件(用户必须介入)② 失败日志达到上限(汇总告知) | P0 |
| FR-6.4 | **转写失败 / 质量不过关:不通知**,只写 CSV + 终端 print | P0 |
| FR-6.5 | 仅 macOS 实现;Windows 预留接口但占位 no-op | P0 |
| FR-6.6 | **失败日志上限弹窗**:`transcribe_fail.csv` 或 `quality_fail.csv` 行数达到阈值倍数(默认 50)→ 阻塞弹窗汇总通知 | P0 |
| FR-6.7 | 日志阈值可配置:`logging.log_alert_threshold`(默认 50),每达到倍数弹一次(避免每条都打扰) | P0 |

**通知决策表**:

| 场景 | 通知方式 | 说明 |
|------|---------|------|
| 视频开始处理 | 无通知 | 终端 log 即可 |
| 字幕命中(official / plugin) | 无通知 | 终端 log 即可 |
| 弹窗启用浏览器插件 | **阻塞弹窗** | 用户必须介入 |
| 字幕 None → 走策略 ③ | 无通知 | 兜底路径,正常 |
| 转写异常 → 写 transcribe_fail | **不通知** | 静默写日志 |
| 质量未通过 → 写 quality_fail | **不通知** | 静默写日志 |
| 插件字幕质量不过关 | **不通知** | 同上,只写日志 |
| 质量通过 | 非阻塞通知 | 进度反馈 |
| 累计达到 6h | 非阻塞通知 | 进度反馈 |
| 总结写完 | 非阻塞通知 | 进度反馈 |
| 失败日志达到阈值倍数 | **阻塞弹窗** | 汇总告知用户 |
| session 结束(配额耗尽) | 非阻塞通知 | 进度反馈 |

**失败日志上限弹窗机制**(FR-6.6):

```text
session 内累计失败数(transcribe_fail.csv + quality_fail.csv 之和)
  │
  ├─ count < threshold (默认 50) → 不弹窗
  │
  └─ count >= threshold 且 count 是 threshold 的整数倍
        (50, 100, 150, 200...)
        │
        └─→ 阻塞弹窗"⚠️ 已积累 N 条失败,请检查 logs/"
             ├─ 转写失败:X 条
             └─ 质量失败:Y 条

实现伪代码:
  last_alerted_at = 0        # 上次弹窗时的整数倍
  threshold = config.logging.log_alert_threshold  # 默认 50

  write_log() 之后:
    total = count(transcribe_fail) + count(quality_fail)
    current_multiple = total // threshold
    if current_multiple > last_alerted_at:
      last_alerted_at = current_multiple
      notifier.alert(...)
```

### FR-7 日志与审计

| ID | 描述 | 优先级 |
|----|------|--------|
| FR-7.1 | `logs/transcribe_fail.csv` - 转写失败记录 | P0 |
| FR-7.2 | `logs/quality_fail.csv` - 质量不过关记录 | P0 |
| FR-7.3 | `logs/failed_texts/*.txt` - 失败的字幕原文(质量不过关时) | P0 |
| FR-7.4 | 每条日志含:时间戳、视频ID、标题、URL、阶段、错误 | P0 |
| FR-7.5 | 提供 `vla logs show` 查看失败摘要 | P1 |
| FR-7.6 | 提供 `vla retry --from <csv>` 重试失败视频 | P1 |
| FR-7.7 | `logs/transcribed/*.txt` - **通过的字幕原文**(FR-4.5,2026-09 新增);Phase 7 总结时按 mtime 排序批量读取 | P0 |

### FR-8 音频源规范(2026-09-03 重命名,原"录屏与音频")

> **2026-09-03 重构 v3,方案 A**:FR-8 整段重写。原"录屏与音频"路径已删除(详见 FR-0 变更日志)。当前音频源只走两条路径:① yt-dlp -x 抽音频 → ② Tab Audio Recorder 扩展。**不录屏 / 不麦克风 / 不系统音频循环捕获**。

| ID     | 描述                                        | 优先级 |
| ------ | ----------------------------------------- | --- |
| FR-8.1 | 路径 ① `yt-dlp -x --audio-format wav --postprocessor-args "-ac 1 -ar 16000"` 抽音频 → `.wav` | P0  |
| FR-8.2 | 路径 ② Tab Audio Recorder 扩展,`chrome.tabCapture` 抓纯净 tab 音频 → `.webm`(opus) | P0  |
| FR-8.3 | ~~录屏 `libx264 preset=ultrafast CRF=28`~~ **删除**(2026-09-03) | — |
| FR-8.4 | ~~自动检测屏幕 index~~ **删除**(2026-09-03) | — |

### FR-9 累计时长与去重(配额管理)

| ID      | 描述                                                                                                                               | 优先级 |
| ------- | -------------------------------------------------------------------------------------------------------------------------------- | --- |
| FR-9.1  | 维护一个**累计成功时长计数器** `accumulated_duration_sec`,初始 0                                                                                | P0  |
| FR-9.2  | **何时累计**:每条视频**通过质量门控**(`passed=True`)→ 累加 `expected_duration` 到计数器                                                              | P0  |
| FR-9.3  | **何时归零**:计数器达到 `summary_threshold_sec`(默认 21600 = 6 小时)→ 触发总结 → **计数器归零**                                                        | P0  |
| FR-9.4  | **配额耗尽停止**:计数器达到 `summary_threshold_sec` 后,**整个 session 停止**后续自动观看 / 截图 / 转写(见 FR-9.7)                                           | P0  |
| FR-9.5  | **成功的视频写入历史文件** `logs/transcribed_history.jsonl`,每行 `{"url": ..., "title": ..., "duration_sec": ..., "transcribed_at": ISO8601}` | P0  |
| FR-9.6  | **下次启动时去重**:开始处理前,**过滤掉历史中已成功的 URL**,不重复观看                                                                                       | P0  |
| FR-9.7  | 达到配额后行为:**不再播放 / 不再截图 / 不再转写**,但已经成功转写的视频**仍正常写笔记 + 触发总结**                                                                       | P0  |
| FR-9.8  | 配额阈值可配置:`summary_threshold_sec`,默认 21600(6 小时)                                                                                   | P0  |
| FR-9.9  | 总结 LLM 输入为 6 小时窗口内所有通过的视频,**一次性**生成 500-800 字总结                                                                                  | P0  |
| FR-9.10 | 累计时长是**配额控制**,不是 token 控制;**总结 LLM 调用受触发,不浪费 token**                                                                             | P0  |


**累计时长与停止逻辑**:

```text
session 开始
  ↓
加载 transcribed_history.jsonl(去重已成功的 URL)
  ↓
accumulated_duration_sec = 0
  ↓
处理每个视频
  │
  ├─ 跳过已成功 URL(在 history 里) → 不观看、不转写
  │
  └─ 未观看 → 走完整流程(字幕/转写/质量门控)
        │
        ├─ 质量未通过 → 累加器不增,保留视频源
        │
        └─ 质量通过 → 累加器 += duration
              ├─ 写笔记(临时,不立即总结)
              ├─ 追加到 history.jsonl
              │
              ├─ 累加器 < 6h → 继续下一个视频
              │
              └─ 累加器 ≥ 6h → 触发总结 LLM(500-800 字)
                    ├─ 输入:6h 窗口内所有通过的视频字幕
                    ├─ 输出:Markdown 总结
                    ├─ 写入 notes.md(覆盖 / 追加)
                    └─ 累加器归零
                          │
                          └─ session 停止(不再看新视频)
```

**总结触发严格语义**:

```text
❌ 不是:每看完一条视频就总结
✅ 而是:累计 6 小时 → 一次性总结这 6 小时的所有内容

❌ 不是:总结完继续看下一批 6 小时
✅ 而是:总结完 → session 结束(由 FR-9.4 控制)

⚠️ 用户控制:
 • 阈值:21600s = 6h(可改)
 • session 重启可重新累积
 • 想重新看已转写视频 → 清空 transcribed_history.jsonl
```

### FR-10 视频组概念(B站 playlist / 番剧 / 合集)

| ID      | 描述                                                                    | 优先级 |
| ------- | --------------------------------------------------------------------- | --- |
| FR-10.1 | 支持**视频组**(videos.yaml 里一组视频作为一个整体)                                    | P0  |
| FR-10.2 | **URL 构成**:`bilibili://group/{group_id}/{bvid}` — 用此格式表示 B站视频组里的某条视频  | P0  |
| FR-10.3 | **group_id 暂时未定**,先用占位方案:视频组用名字作 group_id(如 `python-tutorial-basics`) | P0  |
| FR-10.4 | 同一视频组的视频累计时长共同进入 6h 配额窗口                                              | P0  |
| FR-10.5 | videos.yaml 写法示例见 §八、配置文件                                             | P0  |
| FR-10.6 | 去重 key 使用 **完整 URL**(含 group_id),所以不同视频组的同名视频互不冲突                     | P0  |

**URL 构成说明**:

```text
当前 / 临时方案(B站视频组,用户未拿到明确 group 字段):

  videos.yaml:
    - group_id: python-tutorial-basics     # 视频组 ID(暂用名字)
      title: "Python 基础教程"
      videos:
        - bvid: BV1xxxxxxxx
          title: "第1集 变量与类型"
          url: "https://www.bilibili.com/video/BV1xxxxxxxx"
          duration_sec: 1800
        - bvid: BV1yyyyyyyy
          title: "第2集 控制流"
          ...

  内部表示(FR-10.2):
    URL = "bilibili://group/python-tutorial-basics/BV1xxxxxxxx"
    # ↑ 这就是 transcribed_history.jsonl 里记的"已成功"标识
    # ↑ 也是后续判断"是否要重新观看"的去重 key

未来方案(用户拿到视频组 API 后):
  • 可能换成 season_id / ep_id / ssid 等官方字段
  • 兼容方式:保留 bilibili:// 前缀的解析层,只换 group_id 解析规则
```

**为什么暂时按 B站视频组处理**:
- 用户当前没有明确的视频组字段(FR-10.3)
- 实际场景下,**同一 B站视频组里的视频**才会被批量观看
- 用 group_id(暂时 = 视频组名字)作为配额累计单位,跨组可以独立累计

---

## 三、非功能需求(NFR)

| ID     | 描述                                       |
| ------ | ---------------------------------------- |
| NFR-1  | **Python 3.11+**,包管理用 **uv**             |
| NFR-2  | macOS 13+(Apple Silicon 原生)              |
| NFR-3  | 全部使用开源 / MIT / Apache 许可依赖,**不引入付费 SDK** |
| NFR-4  | 单条视频处理全程**无付费 API 调用**(字幕转写层)            |
| NFR-5  | 云端 API 仅用于:① 字幕质量检查 ② 最终总结 ③ 字幕语义清理(可选)               |
| NFR-6  | 字幕转写失败**不影响**后续视频处理                      |
| NFR-7  | 关键操作(质量检查 / 删除源文件)**写日志**                |
| NFR-8  | 单个模块失败**不导致**整个程序崩溃(try/except 隔离)       |
| NFR-9  | 配置热更新(yaml 文件 + 环境变量)                    |
| NFR-10 | 单元测试覆盖率 ≥ 60%(核心模块)                      |

---

## 四、技术栈

```text
运行时:
  Python          3.11+
  包管理: uv
  类型注解: 必填(用于 VS Code AI 助手理解)

核心依赖:
  faster-whisper    # 本地字幕转写(MIT,开源)
  yt-dlp            # 视频下载(开源,无依赖)
  httpx             # B站 API(替代 requests,异步友好)
  openai            # 云端 LLM 客户端(OpenAI 兼容协议,适配 Qwen/DeepSeek)
  pyobjc-framework-Quartz  # macOS 鼠标控制(预留)
  pysrt / webvtt-py # 字幕格式解析
  pyyaml            # 配置文件
  pydantic          # 数据模型校验
  typer             # CLI 框架
  rich              # 终端美化(可选)

外部工具:
  ffmpeg            # 系统命令,需 brew install ffmpeg
  Homebrew(macOS)   # 装 ffmpeg
```

**依赖约束**:
- 不使用 `requests`(换 httpx)
- 不使用 `argparse`(换 typer)
- 不使用 `dataclasses` 之外的模型(用 pydantic)

---

## 五、系统架构

```text
┌─────────────────────────────────────────────────────────────────┐
│                  Video Learning Agent v1.0                          │
│                                                                   │
│   ┌─────────────┐                                                │
│   │ CLI 入口   │(typer)                                          │
│   └──────┬──────┘                                                │
│          ↓                                                       │
│   ┌──────────────────────────────────────────────────────┐      │
│   │ 主调度器(VideoLearningAgent)                            │      │
│   └──────┬───────────────────────────────────────────────┘      │
│          │                                                       │
│   ┌──────┼────────┬──────────┬──────────┬─────────────┐         │
│   ↓      ↓        ↓          ↓          ↓             ↓         │
│ ┌────┐ ┌────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐  │
│ │视频 │ │字幕 │ │流式   │ │质量   │ │macOS   │ │日志   │  │
│ │源   │ │策略 │ │Whisper│ │检查   │ │通知   │ │审计   │  │
│ └──┬─┘ └──┬─┘ └────┬───┘ └────┬───┘ └────┬───┘ └────┬───┘  │
│    │      │        │          │          │          │         │
│    └──────┴────────┴──────────┴──────────┴──────────┘         │
│                          ↓                                       │
│                  ┌──────────────┐                                │
│                  │ LLM 总结     │                                │
│                  │(云端订阅)    │                                │
│                  └──────┬───────┘                                │
│                         ↓                                        │
│                  ┌──────────────┐                                │
│                  │ notes.md     │                                │
│                  └──────────────┘                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## 六、模块详细规格

### 模块清单

```
src/vla/
├── __init__.py
├── main.py                 # VideoLearningAgent 主类
├── cli.py                  # typer CLI 入口
├── config.py               # 配置加载(pydantic)
├── models.py               # 数据类(VideoTask, SubtitleResult, QualityResult)
│
├── source/
│   ├── __init__.py
│   └── video_source.py     # 视频源判定(_is_downloadable 探测)
│
├── subtitle/
│   ├── __init__.py
│   ├── strategy.py         # 字幕三级策略调度(① API ② 浏览器 ③ Whisper)
│   ├── bilibili_official.py# 策略 ① B站官方 CC
│   ├── browser_plugin.py   # 策略 ② Puppeteer 通用 JS 探测字幕(原 VideoTrans 扫描废弃)
│   ├── platform_adapter.py # PlatformAdapter Protocol + Registry(2026-09-03 新增)
│   ├── bilibili_adapter.py # BilibiliAdapter 实现 fetch_api/browser/recording(2026-09-03 新增)
│   └── tab_audio_recorder.py # 策略 ③ 内部:Tab Audio Recorder 触发 + 下载(FR-2.24/2.25)
│
├── audio/
│   ├── __init__.py
│   ├── source_factory.py   # 音频源工厂,封装 yt-dlp -x 抽音频(FR-2.14 路径 ①,2026-09-03 新增)
│   ├── queue.py            # AudioQueue(asyncio.Queue,容量 10,FR-2.27,2026-09-03 新增)
│   └── worker_pool.py      # WhisperWorkerPool(默认 2 worker,FR-2.27,2026-09-03 新增)
│
├── capture/
│   ├── __init__.py
│   ├── screen_capture.py   # 跨平台系统截图(macOS screencapture / Windows PowerShell,FR-2.28,2026-09-03 新增)
│   └── pre_screenshot.py   # prepare_for_screenshot()(抢焦点 + 窗口归位)+ 末尾 30s 监听(FR-2.28.2a,2026-09-03 新增)
│
├── transcribe/
│   ├── __init__.py
│   └── streaming.py        # faster-whisper 流式转写
│
├── quality/
│   ├── __init__.py
│   └── checker.py          # 云端质量检查
│
├── summary/
│   ├── __init__.py
│   └── llm_summarizer.py   # LLM 总结生成
│
├── ui/
│   ├── __init__.py
│   └── macos_notify.py     # macOS 系统通知
│
└── log/
    ├── __init__.py
    └── transcription_log.py# 转写 / 质量日志

config/
└── vla.yaml                 # 默认配置

tests/
├── test_subtitle_strategy.py
├── test_quality_checker.py
├── test_streaming_transcriber.py
├── test_transcription_log.py
└── fixtures/
    └── sample.srt

logs/                       # 运行时生成(gitignore)
├── transcribe_fail.csv
├── quality_fail.csv
└── failed_texts/
```

---

### 6.1 模块接口契约

#### `models.py`

```python
from pydantic import BaseModel, HttpUrl
from pathlib import Path

class VideoTask(BaseModel):
    id: str
    title: str
    url: HttpUrl
    expected_duration: int  # 秒

class SubtitleResult(BaseModel):
    text: str
    source: str  # "official" | "plugin" | "whisper"
    metadata: dict

class QualityResult(BaseModel):
    passed: bool
    score: int  # 0-100
    issues: list[str]
    suggestion: str
    char_count: int

class VideoSource(BaseModel):
    path: Path
    mode: str  # "download" | "record"
    duration_sec: float
```

#### `source/video_source.py`

```python
class VideoSourceFactory:
    def __init__(self, tmp_dir: Path, log: TranscriptionLog): ...

    def get(self, url: str, video_id: str, expected_duration: int) -> VideoSource:
        """
        流程:
          1. _is_downloadable() 检测
          2. 可下载 → _download()(FR-1.6 最低画质)
          3. 不可下载 → 标记 is_downloadable=False,交给 Phase 3 走音频三级降级(FR-2.14)
        返回 VideoSource,调用方负责后续消费
        """
```

#### `subtitle/strategy.py`

```python
class SubtitleStrategy:
    def __init__(self, config: VLAConfig): ...

    def get_subtitle(
        self,
        url: str,
        bvid: str,
        title: str,
    ) -> SubtitleResult | None:
        """
        三级策略:
          ① BilibiliOfficialSubtitle.get_subtitle(url)
          ② BrowserPluginSubtitle.find_subtitle(bvid, title)
          ③ 返回 None,让主调度器走下载/录屏路径
        """
```

#### `subtitle/bilibili_official.py`

```python
class BilibiliOfficialSubtitle:
    HEADERS: dict[str, str]  # UA + Referer

    def extract_bv_id(self, url: str) -> str: ...
    def get_subtitle(self, url: str) -> tuple[str, dict] | None:
        """
        步骤:
          1. GET /x/web-interface/view?bvid=xxx → 拿 cid
          2. GET /x/player/v2?bvid=xxx&cid=xxx → 拿 subtitle_url
          3. GET subtitle_url → B站 JSON
          4. 拼接 body[].content → 纯文本
        返回 (text, metadata) 或 None(无字幕)
        """
```

#### `subtitle/browser_plugin.py`

```python
class BrowserPluginSubtitle:
    PLUGIN_PATHS: list[Path]  # VideoTrans 导出目录

    def find_subtitle(self, bvid: str, title: str) -> Path | None: ...
    def wait_for_subtitle(
        self, bvid: str, title: str, timeout: int = 600
    ) -> Path | None: ...
    def parse(self, path: Path) -> str: ...
    # 支持 .srt / .vtt / .json / .ass
```

#### `transcribe/streaming.py`(2026-09-03 重构,集成 Level 1 本地 + Level 4 云端语义清理)

```python
class StreamingTranscriber:
    def __init__(
        self,
        model_size: str,
        log: TranscriptionLog,
        postprocess: PostProcessor,           # FR-3.8:Level 1 本地清理(必启用)
        refiner: SubtitleRefiner | None = None, # FR-3.9:Level 4 云端 LLM(可选,refine_enabled=true 时注入)
    ): ...

    async def transcribe_audio(
        self,
        audio_path: Path,
        bvid: str,
        title: str,
    ) -> TranscribeResult:
        """
        步骤(2026-09-03 重构,集成 Level 1/4):
          1. faster-whisper.transcribe(audio_path) → raw_segments
          2. 写 logs/transcripts/<bvid>.transcript.txt(Whisper 原始 segments)
          3. postprocess.clean_transcript(raw_text, title) → cleaned_text     # FR-3.8 Level 1(本地,必启用)
          4. 写 logs/transcripts/<bvid>.cleaned.txt(Level 1 清理后,审计用)
          5. 若 refiner 注入(refine_enabled=true):
               refined = await refiner.refine(cleaned_text, title)            # FR-3.9 Level 4(云端 LLM)
               写 logs/transcripts/<bvid>.refined.txt + corrections 元数据
               final_text = refined.cleaned_text
             else:
               final_text = cleaned_text
          6. 删音频源 audio_path(FR-3.7:质量门控前的预清理,失败时由主调度保留)
          7. return TranscribeResult(text=final_text, source="<transcript|cleaned|refined>",
                                       audio_path=audio_path)
        返回值设计:TranscribeResult.text 是**最终给质量门控的文本**(refined 或 cleaned),
                    主调度无需关心走了哪一层。
        """

    def cleanup(self, *paths: Path) -> None:
        """质量通过后,删音频(FR-3.7);失败时主调度不调此方法,保留供重转写"""


class PostProcessor:  # FR-3.8 实现,封装在 transcribe/postprocess.py
    def clean_transcript(
        self,
        text: str,
        title: str,
        min_line_chars: int = 8,
        max_line_chars: int = 60,
        min_overlap_chars: int = 6,
    ) -> str:
        """
        Level 1 本地清理(必启用,FR-3.8):
          ① merge_short_lines(< min_line_chars) → 并入上一行
          ② dedupe_repeated_segments(LCS ≥ min_overlap_chars) → 去重 B站 CC 叠音
        """


class SubtitleRefiner:  # FR-3.9 实现,封装在 quality/refiner.py
    async def refine(
        self,
        text: str,
        title: str,
        max_chars: int = 6000,
        model: str | None = None,           # None → fallback quality_check.model
    ) -> RefinementResult:
        """
        Level 4 云端 LLM 语义调整(可选,FR-3.9):
          失败 fallback:返回 RefinementResult(cleaned_text=原 text, corrections=[], notes=失败原因)
        """


class TranscribeResult(NamedTuple):
    text: str           # 最终文本(refined 或 cleaned,给质量门控)
    source: Literal["transcript", "cleaned", "refined"]  # 走的哪一层
    audio_path: Path    # 用于主调度决定清理时机(FR-3.7)


class RefinementResult(NamedTuple):
    cleaned_text: str
    corrections: list[str]
    notes: str         # 失败时填失败原因,成功时填"ok"
```

#### `quality/checker.py`

```python
class QualityChecker:
    PROMPT: str  # 见配置节

    def __init__(self, llm_client: LLMClient): ...

    def check(
        self,
        text: str,
        title: str,
        duration_sec: float,
        model_size: str,
    ) -> QualityResult:
        """
        流程:
          1. 启发式:字/秒 异常 → 立即返回 fail
          2. 截前 3000 字 → LLM
          3. 解析 JSON → QualityResult
        """
```

#### `summary/llm_summarizer.py`

```python
class LLMSummarizer:
    def __init__(self, llm_client: LLMClient, notes_file: Path): ...

    def summarize(
        self,
        title: str,
        text: str,
        quality: QualityResult,
    ) -> str:
        """
        步骤:
          1. 加载历史笔记(同标题的已有总结)
          2. LLM prompt:基于标题提取知识点 + 合并去重
          3. 返回 500-800 字 Markdown
          4. 调用方负责追加到文件
        """
```

#### `ui/macos_notify.py`

```python
class MacOSNotifier:
    """2026-09-03 重构 v3(方案 C):仅保留 B/C 级通知,**删除** `ask_open_browser` 阻塞 A 级弹窗。"""
    def info(self, title: str, message: str) -> None:
        """B 级:display notification(非阻塞),用于进度性消息 / Tab Audio Recorder 启用提示(FR-2.21 disabled 状态)"""
    def warning(self, title: str, message: str) -> None:
        """B 级:display notification(非阻塞),用于 Tab Audio Recorder 未安装提示(FR-2.21 not_installed 状态)"""
    def alert(
        self, title: str, message: str, buttons: tuple[str, ...] = ("OK",)
    ) -> str:
        """A 级:display dialog(阻塞,带超时降级),仅用于 FR-6.6 失败日志上限汇总"""
    # ~~def ask_open_browser~~ **删除**(2026-09-03,方案 C 落地,见 FR-0)
```

#### `log/transcription_log.py`

```python
class TranscriptionLog:
    def __init__(self, log_dir: Path): ...

    def log_transcribe_fail(
        self, video_id: str, title: str, url: str, stage: str, error: str
    ) -> None: ...

    def log_quality_fail(
        self,
        video_id: str,
        title: str,
        url: str,
        result: QualityResult,
        text: str,
    ) -> None: ...

    def summary(self) -> str: ...
```

---

## 七、数据流

### 单条视频完整数据流

> **关键**:SubtitleStrategy 返回 `None` ≠ 转写失败,而是**降级信号**。
> 主调度器收到 None 后,**走视频源工厂 + Whisper**(策略 ③),不写 transcribe_fail。

```text
输入:VideoTask(id, title, url, expected_duration)
  │
  ├─→ [SubtitleStrategy.get_subtitle]  ←── 三级字幕策略(① API ② 浏览器 ③ Whisper)
  │     │
  │     ├─ ① BilibiliAdapter.fetch_api_subtitle(B站 CC API)
  │     │    ├─ 成功 → SubtitleResult(source="api")
  │     │    └─ 失败/无字幕 ↓
  │     │
  │     ├─ ② BilibiliAdapter.fetch_browser_subtitle(Puppeteer 通用 JS 探测)
  │     │    ├─ 成功 → SubtitleResult(source="browser")
  │     │    └─ 失败/无字幕 ↓
  │     │
  │     └─ ③ BilibiliAdapter.fetch_via_recording
  │           ├─ 路径 ①:yt-dlp -x 抽音频 → logs/audio_raw/<bvid>.wav
  │           │   ├─ 成功 → faster-whisper → SubtitleResult(source="whisper")
  │           │   └─ 失败/视频不可下载 ↓
  │           │
  │           └─ 路径 ②:Tab Audio Recorder 扩展(FR-2.21/2.24)
  │               ├─ probe_status 三态探测
  │               │   ├─ enabled        → start_recording → audio_id → download
  │               │   ├─ disabled       → B 级通知 + quality_skip.csv
  │               │   └─ not_installed  → B 级通知 + quality_skip.csv
  │               └─ faster-whisper → SubtitleResult(source="whisper")
  │
  ├─→ [StreamingTranscriber.transcribe_audio](2026-09-03 重构,集成 Level 1/4)
  │     ├─ 直读 .wav / .webm(ffmpeg 解封装或 chrome.tabCapture 编码)
  │     ├─ faster-whisper → raw_segments
  │     ├─ 写 logs/transcripts/<bvid>.transcript.txt(原始)
  │     ├─ postprocess.clean_transcript(raw_text)           # FR-3.8 Level 1 本地清理(必启用)
  │     │    ├─ merge_short_lines(< min_line_chars=8)
  │     │    └─ dedupe_repeated_segments(LCS ≥ min_overlap_chars=6)
  │     ├─ 写 logs/transcripts/<bvid>.cleaned.txt(Level 1 清理后)
  │     ├─ 若 refine_enabled=true:
  │     │    └─ refiner.refine(cleaned_text, title)         # FR-3.9 Level 4 云端 LLM(可选)
  │     │         ├─ 写 logs/transcripts/<bvid>.refined.txt + corrections 元数据
  │     │         ├─ 失败 fallback → 用 cleaned_text(不抛错)
  │     │         └─ 延迟 ~1-3s
  │     ├─ final_text = refined_text or cleaned_text        # 给质量门控用的最终文本
  │     └─ 删音频源(Whisper 完成后立即 unlink,失败时由主调度保留,FR-3.7)
  │
  ├─→ [QualityChecker.check](接收 final_text,FR-3.9 refined 或 FR-3.8 cleaned)
  │     ├─ 启发式预筛(字/秒 + 重复)
  │     │    ├─ 异常 → QualityResult(passed=False)
  │     │    └─ 正常 ↓
  │     ├─ 云端 LLM 检查(可选)
  │     └─ 返回 QualityResult
  │
  ├─→ [分支]
  │     ├─ passed=True
  │     │    ├─ [StreamingTranscriber.cleanup] 删音频源
  │     │    ├─ [LLMSummarizer.summarize] 500-800 字 Markdown
  │     │    └─ 追加到 notes.md
  │     │
  │     └─ passed=False
  │          ├─ [TranscriptionLog.log_quality_fail]
  │          ├─ 保留音频 + 存 failed_texts/
  │          └─ C 级静默(仅 CSV,不弹窗,FR-6.4)
  │
  └─ 输出:None(继续下一条)
```

### 字幕降级路径(FR-2 关键语义,2026-09-03 重构 v3)

```text
PlatformAdapter.get_subtitle()(由 SubtitleStrategy 调度)
  │
  ├─ ① fetch_api_subtitle 命中 → return SubtitleResult(source="api")
  │
  └─ ① 失败/无字幕 → 进入 ②
        │
        ├─ ② fetch_browser_subtitle 命中 → return SubtitleResult(source="browser")
        │
        └─ ② 失败/无字幕 → 进入 ③
              │
              ├─ 路径 ① yt-dlp -x 抽音频成功 → return SubtitleResult(source="whisper")
              │
              └─ 路径 ① miss → Tab Audio Recorder 探测 + 录制
                    ├─ probe_status = enabled → 录制成功 → return source="whisper"
                    ├─ probe_status = disabled / not_installed → return None(已写 quality_skip.csv)
                    └─ 录制失败 → return None(已写 quality_skip.csv,**不记 transcribe_fail**)
```
              └─ 弹窗超时未响应 → return None
                    │
                    ↓
              主调度器收到 None
                    │
                    └─→ 走视频源工厂 + Whisper
                          │
                          └─→ return SubtitleResult(source="whisper")
```

**返回 None 的语义**:
- ✅ 是"字幕获取失败",但**不是转写失败**
- ❌ 不写 `transcribe_fail.csv`(FR-2.8)
- ✅ 主调度器继续走兜底路径,直到拿到可用的字幕

### 累计时长与配额管理(FR-9)

```text
session 启动
  │
  ├─→ [HistoryManager.load] 读 transcribed_history.jsonl
  │     └─→ 过滤掉已成功的 URL(FR-9.6)
  │
  ├─→ QuotaManager(threshold=21600s, current=0)
  │
  └─→ 循环处理每个 video
        │
        ├─ URL 已在 history → 跳过(skip + 写日志"已转写")
        │
        └─ 未观看 → _process_one(task)
              │
              ├─ 字幕获取 → 质量门控
              │
              ├─ passed=False → 累加器不增 → 继续下一个
              │
              └─ passed=True
                    │
                    ├─ [HistoryManager.record_success] 写一行 jsonl
                    │
                    ├─ 字幕临时存档到"6h 窗口"(内存 list)
                    │
                    ├─ QuotaManager.add(duration_sec)
                    │     │
                    │     ├─ current < threshold → 继续下一个视频
                    │     │
                    │     └─ current ≥ threshold → 触发总结
                    │           │
                    │           ├─ [LLMSummarizer.summarize_batch]
                    │           │     输入:6h 窗口内所有字幕
                    │           │     输出:500-800 字 Markdown
                    │           │
                    │           ├─ 写入 notes.md
                    │           │
                    │           ├─ 清空 6h 窗口
                    │           │
                    │           └─ [QuotaManager.reset]
                    │                 └─→ 通知主循环:session 结束
                    │
                    └─→ continue / stop_session
```

**session 停止语义**(FR-9.7):

```text
on_exhausted: "stop_session"
  ├─ 不再播放下一个视频
  ├─ 不再截图
  ├─ 不再转写
  ├─ 但本轮触发点的总结**正常完成**(因为已经进入 LLM 调用)
  └─ macOS 通知: "🎉 已累计 6 小时,总结已生成,session 结束"

on_exhausted: "continue"
  ├─ 不实现(FR-9.4 默认行为)
  └─ 如果用户改了这个值,后续视频继续看,累加器归零后重新累计
```

**去重 key**(FR-10.6):

```text
history.jsonl 里记的 url_key = "bilibili://group/{group_id}/{bvid}"

例如:
  group_id = python-tutorial-basics
  bvid = BV1xxxxxxxx
  url_key = "bilibili://group/python-tutorial-basics/BV1xxxxxxxx"

下次启动:
  videos.yaml 里某条 url = "https://www.bilibili.com/video/BV1xxxxxxxx"
  → 内部转换成 url_key
  → 在 history.jsonl 里查
  → 命中 → 跳过
```

---

## 八、配置文件

### `config/vla.yaml`

```yaml
storage:
  tmp_dir: "./tmp"
  auto_cleanup_on_pass: true

whisper:
  model: "small"          # tiny | base | small | medium | large-v3
  language: "zh"
  segment_seconds: 30
  compute_type: "int8"    # int8 / int8_float16 / float16

video_source:
  # ~~record / screen_index / fps / crf / audio_input / preset~~ **删除**(2026-09-03,不再录屏)
  # 当前 audio_source 走二级降级(FR-2.14):yt-dlp -x → Tab Audio Recorder
  is_downloadable_probe: true   # yt-dlp --simulate 探测,用于路径 ① 判定

quality_check:
  enabled: true
  model: "gpt-4o-mini"    # 云端订阅
  min_score_to_pass: 70
  min_char_per_second: 1.0
  max_char_per_second: 15.0

extension:
  tab_audio_recorder:
    match_keyword: "tab audio"   # FR-2.15/2.24:_resolve_ext_id 动态匹配关键词
    enabled_required: false       # FR-2.21 probe_status:disabled 也只是 B 级通知,不阻塞

screenshot:
  enabled: true                     # FR-2.28 总开关
  trigger_strategy: "whisper"       # 仅当走策略 ③ 时才截图(有字幕视频截图无意义)
  require_fullscreen: true          # 截图前必须 element.requestFullscreen()
  fullscreen_confirm_timeout_sec: 30 # session 内首次全屏用户确认最长等待(vla doctor 阶段预热)
  end_offset_sec: 30                # 末尾截图时机:结束前 N 秒(FR-2.28 PHASE C)
  end_trigger_buffer_sec: 3         # 末尾截图触发 buffer(提前 N 秒开始截图流程,留截图时间)
  pre_capture_notify: true          # 截图前 B 级通知("准备截图,请稍候",FR-2.28.2d,用户已 review)
  save_dir: "logs/screenshots"
  filename_pattern_start: "{bvid}_{unix_ts_start}_start.png"
  filename_pattern_end:   "{bvid}_{unix_ts_end}_end.png"
  index_file: "logs/screenshots/index.jsonl"   # 截图索引(审计 + 时长校验,FR-2.28.2e)
  embed_in_notes: false             # FR-2.29 P1:截图嵌入 notes.md(默认关闭,避免 notes 膨胀)
  platform:
    macos:
      tool: "screencapture"
      exclude_cursor: true          # -x 隐藏鼠标
    windows:
      tool: "powershell_system_drawing"
      timeout_sec: 15

# ~~browser_plugin / remind_timeout_sec~~ **删除**(2026-09-03,方案 C 不再弹 A 级 dialog)

summary:
  model: "gpt-4o-mini"
  target_words_min: 500
  target_words_max: 800
  notes_file: "./notes/videos.md"
  cross_video_dedup: true
  # FR-9 配额触发
  trigger_mode: "quota"           # quota(累计 6h) | per_video(每条)
  notes_section_header: "## {group_title}"  # 笔记里用什么标题分段

quota:                              # FR-9 累计时长配额
  summary_threshold_sec: 21600    # 6 小时 = 21600 秒
  on_exhausted: "stop_session"     # 达到阈值后行为:stop_session | continue

history:                            # FR-9.5/9.6 去重历史
  file: "./logs/transcribed_history.jsonl"
  # 每行 JSON:
  # {"url": "bilibili://group/python-tutorial-basics/BV1xxx",
  #  "title": "...", "duration_sec": 1800, "transcribed_at": "2026-08-31T10:00:00"}

logging:
  log_dir: "./logs"
  notify_on_fail: false                 # FR-6.4:转写/质量失败不通知
  log_alert_threshold: 50               # FR-6.6:每 50 条失败弹窗汇总一次
  log_alert_enabled: true               # 是否启用失败日志上限弹窗

llm_client:
  provider: "openai"      # openai | qwen | deepseek
  api_key_env: "OPENAI_API_KEY"
  base_url_env: "OPENAI_BASE_URL"
```

### `videos.yaml`(FR-10.5 视频组写法)

```yaml
# 视频组:暂时用 group_id = 视频组名字(FR-10.3)
# 同一 group_id 下的视频共同进入 6h 配额窗口
video_groups:
  - group_id: python-tutorial-basics
    title: "Python 基础教程"
    videos:
      - bvid: BV1xxxxxxxx
        title: "第1集 变量与类型"
        url: "https://www.bilibili.com/video/BV1xxxxxxxx"
        duration_sec: 1800        # 30 分钟
      - bvid: BV1yyyyyyyy
        title: "第2集 控制流"
        url: "https://www.bilibili.com/video/BV1yyyyyyyy"
        duration_sec: 2400        # 40 分钟
      - bvid: BV1zzzzzzzz
        title: "第3集 函数"
        url: "https://www.bilibili.com/video/BV1zzzzzzzz"
        duration_sec: 2100        # 35 分钟

  - group_id: react-deep-dive
    title: "React 深入"
    videos:
      - bvid: BV2aaaaaaaa
        title: "Hooks 原理"
        url: "https://www.bilibili.com/video/BV2aaaaaaaa"
        duration_sec: 3600        # 60 分钟
      - bvid: BV2bbbbbbbb
        title: "Fiber 架构"
        url: "https://www.bilibili.com/video/BV2bbbbbbbb"
        duration_sec: 4200        # 70 分钟
```

**每条视频内部 URL 表示**(FR-10.2):

```text
原始 URL: https://www.bilibili.com/video/BV1xxxxxxxx
内部表示: bilibili://group/python-tutorial-basics/BV1xxxxxxxx
         ↑                    ↑                  ↑
         prefix            group_id            bvid
```

**遍历顺序**(v1.0 简化):
- 按 yaml 文件顺序逐视频组处理
- 每个视频组内的视频按列表顺序处理
- 累计时长跨视频组统一计入 6h 窗口

**v1.0 不做**:
- ❌ 不做视频组内智能调度(按优先级 / 难度)
- ❌ 不做跨视频组并行
- ❌ 不做视频组嵌套(子组)

---

## 九、CLI 接口

### `vla/cli.py`

```bash
# 批量处理
vla batch --config ./my-videos.yaml

# 单条处理
vla process \
  --url "https://www.bilibili.com/video/BV1xxx" \
  --title "Python 入门" \
  --duration 1800

# 查看日志
vla logs show --type quality_fail      # quality_fail | transcribe_fail | all
vla logs show --type all --last 10

# 重试失败
vla retry --from ./logs/quality_fail.csv
vla retry --from ./logs/transcribe_fail.csv

# 检测环境
vla doctor
  # 检查:ffmpeg / yt-dlp / faster-whisper 模型 / LLM API key
```

---

## 十、异常处理矩阵

| 异常场景 | 模块处理 | 日志 | 视频源 | 通知 | 继续下一条? |
|----------|----------|------|--------|------|------------|
| B站 API 返回 404 | catch → 降级到策略 ② | 无 | 不下载 | 无 | ✅ |
| 策略 ① 无字幕(返回 None/空) | 降级到策略 ② | 无 | 不下载 | 无 | ✅ |
| 策略 ② 目录扫描无文件 | 触发弹窗 | 无 | 不下载 | **阻塞弹窗** | ✅ |
| **弹窗用户点"已开启"** | 阻塞等文件 | 无(命中则记 source=plugin) | 不下载 | (弹窗响应) | ✅ |
| **弹窗用户点"跳过该视频"** | **降级到策略 ③**(返回 None) | **不写** transcribe_fail | 不下载 | (弹窗响应) | ✅ |
| **弹窗超时未响应** | **降级到策略 ③**(返回 None) | **不写** transcribe_fail | 不下载 | (弹窗响应) | ✅ |
| **插件启动失败 / 不可用**(FR-2.10) | 标记 session 插件状态 `unavailable` | 无 | 不下载 | 无 | ✅(后续不弹窗) |
| **插件字幕质量不过关**(FR-2.11) | 视同插件路径失败 → 标记 `unavailable` + 走策略 ③ | 写 quality_fail(failure_source="plugin") | 下载/录屏 | **不通知**(FR-6.4) | ✅ |
| yt-dlp 下载失败 | catch → 降级到录屏 | 转写失败 | 不下载 | **不通知**(FR-6.4) | ✅ |
| ffmpeg 录屏失败 | catch → log | 转写失败 | 不存在 | **不通知**(FR-6.4) | ✅ |
| faster-whisper 模型加载失败 | 启动即 fail → 终止 | - | - | - | ❌ 程序终止 |
| faster-whisper 转写异常 | catch → log | 转写失败 | 保留 | **不通知**(FR-6.4) | ✅ |
| LLM API 调用失败 | catch → log | 质量失败 | 保留 | **不通知**(FR-6.4) | ✅ |
| **累计时长达到 6h**(FR-9.4) | 触发总结 + 停止 session | history 标记完成 | - | 非阻塞通知 | ❌ session 结束 |
| **视频 URL 已在 history**(FR-9.6) | 跳过,不观看不转写 | debug 日志 | 不下载 | 无 | ✅ |
| macOS 通知无权限 | catch → 静默降级为 print | - | - | (降级) | ✅ |
| 磁盘满 | 启动前 check → 终止 | - | - | - | ❌ |
| **失败日志达到上限倍数**(FR-6.6) | **阻塞弹窗汇总** | 已是上限来源 | - | **阻塞弹窗** | ✅ |

**关键**:
1. 策略 ①② 失败/超时,**不是写 transcribe_fail**,而是返回 None 让主调度走策略 ③。
2. 插件"一次启动"语义(FR-2.9):整 session 只弹一次,后续根据状态机决定。
3. 累计时长 6h 触发停止:**写笔记**+ **触发总结** + **session 结束**(FR-9.7)。
4. **转写 / 质量失败:不通知用户**(FR-6.4),只写 CSV + 终端 print。
5. 只有**策略 ③ 本身**的转写失败(faster-whisper 异常)才写 `transcribe_fail.csv`(FR-3.5)。
6. **失败日志达上限倍数**才阻塞弹窗汇总(默认每 50 条一次)。

---

## 十一、验收标准(AC)

### AC-1 字幕三级策略(2026-09-03 重构 v3)

- [ ] 给一个**有 CC 字幕的 B站视频**,运行 `vla process`,字幕来源标记为 `api`。
- [ ] 给一个**无 CC + Puppeteer 通用 JS 探测能拿到字幕**的视频,来源标记为 `browser`。
- [ ] 给一个**无 CC + Puppeteer 拿不到 + yt-dlp 可下载**的视频,自动抽音频 → faster-whisper,来源标记为 `whisper`,**且 `transcribe_fail.csv` 不增加行**。
- [ ] 给一个**无 CC + Puppeteer 拿不到 + yt-dlp 不可下载 + Tab Audio Recorder enabled** 的视频,扩展录制 + 转写,来源标记为 `whisper`,**且 `transcribe_fail.csv` 不增加行**。
- [ ] 给一个**Tab Audio Recorder disabled 或 not_installed** 的视频,**不弹 A 级阻塞 dialog**(方案 C),只发 B 级通知,视频写 `quality_skip.csv`,**session 不阻塞**。

### AC-2 磁盘管理

- [ ] 处理 1 小时视频全程,**tmp 目录峰值** < 1 GB。
- [ ] 处理完成后,**通过的字幕对应视频源已被删除**。
- [ ] 失败的字幕对应视频源**仍然保留**(可手动清理)。

### AC-3 质量门控

- [ ] 转写一段**静音视频**,QualityResult.passed=False,记录到 `quality_fail.csv`,视频源未删。
- [ ] 转写一段**正常视频**,QualityResult.passed=True,记录不出现,视频源删除,总结写入笔记。

### AC-4 通知与日志(2026-09-03 重构 v3)

- [ ] `transcribe_fail.csv` 和 `quality_fail.csv` 行数与实际失败数一致。
- [ ] `failed_texts/` 下能找到失败的字幕原文(仅质量不过关时存)。
- [ ] **转写失败 / 质量不过关时,不弹通知**(FR-6.4),仅终端 print + 写 CSV。
- [ ] **Tab Audio Recorder disabled** → `MacOSNotifier.info` B 级通知 + `quality_skip.csv`(不阻塞,FR-2.21)。
- [ ] **Tab Audio Recorder not_installed** → `MacOSNotifier.warning` B 级通知 + `quality_skip.csv`(不阻塞,FR-2.21)。
- [ ] ~~**浏览器插件启用弹窗** 是 session 内唯一的阻塞弹窗~~ **删除**(2026-09-03,方案 C 不再弹 A 级 dialog)。
- [ ] **失败日志达到上限倍数**(默认 50)→ 阻塞弹窗汇总"⚠️ 已积累 N 条失败",含转写失败 / 质量失败计数(FR-6.6)。
- [ ] 日志阈值可在 `logging.log_alert_threshold` 配置。

### AC-5 总结输出

- [ ] **总结触发条件**:累计 6 小时通过质量检查的视频后才生成,**不是**每条视频都总结。
- [ ] 单次总结字数 500–800 之间,覆盖 6 小时窗口内所有通过的视频。
- [ ] 笔记文件追加格式:`## {group_title} — 累计 {N} 分钟({M} 个视频)` + 500-800 字总结内容。
- [ ] 总结触发后,累加器归零,session 停止(默认 `on_exhausted: stop_session`)。

### AC-6 CLI

- [ ] `vla doctor` 给出 ffmpeg / yt-dlp / 模型 / API key 状态报告。
- [ ] `vla logs show --type quality_fail` 表格化输出失败列表。
- [ ] `vla retry --from <csv>` 读取 csv 重跑失败视频。

### AC-7 配额管理(累计时长)

- [ ] 处理通过 1 条 30 分钟视频,`accumulated_duration_sec` 增加 1800。
- [ ] 累计达到 21600s(6 小时)→ 触发总结 → 累加器归零 → session 停止。
- [ ] 触发总结的 6 小时窗口内视频字幕作为 LLM 输入,一次性生成总结。
- [ ] 累计不到 6 小时但 session 结束(用户中断 / 视频耗尽),**不触发**总结,仅写已转写视频的临时笔记。
- [ ] 配额阈值可通过 `quota.summary_threshold_sec` 配置覆盖。

### AC-8 去重与历史

- [ ] session 启动时,加载 `logs/transcribed_history.jsonl`,过滤掉已成功转写的 URL。
- [ ] 已成功视频在主循环里**直接跳过**,不观看、不截图、不转写,debug 日志说明"已转写"。
- [ ] 通过质量门控的视频**追加一行**到 `transcribed_history.jsonl`(含 url / title / duration_sec / transcribed_at)。
- [ ] URL key 格式:`bilibili://group/{group_id}/{bvid}`(FR-10.2),跨视频组互不冲突。
- [ ] **不自动清理** history 文件;用户手动删除才能重新观看。

### AC-9 ~~插件状态机~~ → Tab Audio Recorder 探测(2026-09-03 重构 v3)

> **2026-09-03 重构**:旧"PluginStatus 单例 + session 只弹一次"语义**已删除**(方案 C)。新语义是 `probe_status()` 每次即时探测 + 三态分支处理(FR-2.21)。

- [ ] Tab Audio Recorder **enabled** → 直接 `start_recording`,不通知,不写 skip。
- [ ] Tab Audio Recorder **disabled** → `MacOSNotifier.info` B 级通知 + `quality_skip.csv`,session 继续,不阻塞。
- [ ] Tab Audio Recorder **not_installed** → `MacOSNotifier.warning` B 级通知 + `quality_skip.csv`,session 继续,不阻塞。
- [ ] 探测函数 `probe_status()` **无状态**(每次调用重新执行 `chrome.management.getAll()`),~100ms,无单例、无锁。
- [ ] `probe_status()` 扩展 ID 来源:`_resolve_ext_id()` 动态从 `chrome.management.getAll()` 匹配 `match_keyword`(**不硬编码**)。

### AC-10 视频开头 + 末尾双截图(2026-09-03 新增,FR-2.28)

- [ ] 处理一个**有 CC 的视频**(走策略 ① / ②)→ **不触发截图**(`screenshot.trigger_strategy=whisper` 限定)。
- [ ] 处理一个**无 CC + yt-dlp 可下**的视频(走策略 ③ 路径 ①)→ 触发**开头截图** + **末尾截图**,2 张 PNG 落地 `logs/screenshots/<bvid>_..._start.png` + `<bvid>_..._end.png`。
- [ ] 开头截图**必须在音频录制启动之前完成**(前置硬约束,FR-2.28 PHASE A)。
- [ ] 末尾截图在 `currentTime >= duration_sec - 33` 时触发,`video.pause()` + `currentTime = duration_sec - 30`,截图完成后 `video.play()` 恢复。
- [ ] 末尾截图期间音频录制**不停**(只视频暂停 1-2s)。
- [ ] 截图文件能截到 **macOS 菜单栏时间 / Windows 任务栏时间**(人工 spot check PNG,菜单栏/任务栏区域应可见系统时间戳)。
- [ ] `logs/screenshots/index.jsonl` 写入 `{bvid, start_ts, end_ts, duration_estimate, partial_flags}` 索引行,`end_ts - start_ts` ≈ `duration_sec`(±5s 误差)。
- [ ] 截图失败(权限拒 / 超时)→ log warning + 跳过该张,音频录制 / 视频播放不阻塞。

### AC-11 截图前抢焦点 + `vla doctor` 验证(2026-09-03 新增,FR-2.28.2a/c)

- [ ] **`vla doctor` 阶段**:创建一个临时 `<video>` 元素 + 调 `requestFullscreen()`,验证返回值 Promise 不报错,**提前暴露 fullscreen 权限问题**。
- [ ] **`vla doctor` 阶段**:macOS 跑一次 `screencapture -x /tmp/vla-doctor.png`,验证屏幕录制 TCC 权限已授权(失败 → 提示用户去系统设置授权)。
- [ ] 截图前 `prepare_for_screenshot(page)` 执行顺序:`bring_to_front()` → `window.focus()` → `window.moveTo(0,0)` + `window.resizeTo(screen.width, screen.height)` → `await sleep(0.3)`。
- [ ] **禁用 macOS 原生全屏**(F11 / 绿点按钮),必须用 `element.requestFullscreen()`(避免切 Space)。
- [ ] 截图前 0.5s 发 `MacOSNotifier.info("准备截图", ...)` B 级通知(用户已 review 接受)。
- [ ] 如果用户前台是其他 APP,截图完成后**不主动切回**,用户可手动 alt+tab 回到原 APP。
- [ ] 抢焦点失败 → 仍尝试截图,标记 `partial=menu_bar_only`(只截到菜单栏时间),保留供审计。

---

## 十二、风险与限制

| 风险                 | 说明                | 缓解                       |
| macOS 通知权限          | `display notification`(B 级)首次需用户授权    | README 写明首次启动步骤          |
| macOS 辅助功能         | osascript A 级弹窗首次需授权  | README 写明首次启动步骤          |
| macOS 屏幕录制          | `screencapture -x` 单帧截图(FR-2.28)首次需用户授权 | `vla doctor` 阶段预热,失败 → 引导系统设置 → 隐私与安全性 → 屏幕录制 |
| 截图抢焦点打扰用户        | 系统截图需把 Chrome 拉到前台,打断用户其他操作 | 截图前 0.5s 发 B 级通知;每条视频仅 2 次抢焦点(~1s/次),用户可接受 |
| 多显示器 / 窗口最小化    | 截图前窗口不在主显示器 / 被最小化 → 截不到视频画面 | `prepare_for_screenshot()` 强制 `moveTo(0,0) + resizeTo(screen.width, screen.height)` |
| fullscreen 首次确认    | session 内首次 `element.requestFullscreen()` 浏览器弹"按 Esc 退出"提示 | `vla doctor` 阶段预热一次,提前暴露权限问题 |
| B站风控               | 频繁下载 / 高频请求可能触发限流 | 下载间隔 + UA / Referer 完整   |
| Whisper 模型体积       | large-v3 约 3 GB   | 默认 small(500 MB),按需切换    |
| 云端 API 余额          | 总结调用耗 token       | GPT-4o-mini 极便宜,可忽略      |
| 字幕版权               | 单帧截图含视频画面         | 仅供个人学习,不得传播              |

---

## 十三、未来扩展(Out of Scope for v1.0)

- 弹幕分析
- 视频分段笔记(按时间戳切分章节)
- 多语言字幕(英文/日文)
- 浏览器扩展形式部署(避开 CLI)
- WebUI 管理界面
- 接入 Anki 自动制卡

---

## 十四、术语表

| 术语 | 含义 |
|------|------|
| BV 号 | B站视频唯一标识,以 `BV` 开头的字符串 |
| CC 字幕 | B站官方字幕,UP 主上传 |
| VideoTrans | 开源浏览器插件,本地 Whisper 转写 |
| faster-whisper | CTranslate2 加速版 Whisper,本地运行 |
| 策略 B | 本项目录屏方案:录浏览器窗口 + 系统音频 |
| 启发式预筛 | 不调 LLM,基于字/秒判断字幕可信度 |
| 质量门控 | 删除视频源前必经的检查环节 |
