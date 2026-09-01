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
3. **最小化云端 API 成本**:字幕转写**只用本地开源模型**;云端模型**只用于质量检查 + 最终总结**。

---

## 二、功能需求(FR)

### FR-1 视频源管理

| ID     | 描述                                                      | 优先级 |
| ------ | ------------------------------------------------------- | --- |
| FR-1.1 | 支持从 `videos.yaml` 批量加载视频任务                              | P0  |
| FR-1.2 | 支持 CLI 单条处理 `--url` `--title`                           | P0  |
| FR-1.3 | 视频 URL 支持 B站(`www.bilibili.com/video/BVxxx`)及 b23.tv 短链 | P0  |
| FR-1.4 | 自动检测视频可下载性(yt-dlp simulate)                             | P0  |
| FR-1.5 | 可下载 → 下载;不可下载 → 系统录屏降级                                  | P0  |
| FR-1.6 | 下载仅取最低画质,节约磁盘                                           | P0  |

### FR-2 字幕提取(平台无关三级策略)

> **设计原则**:项目目标是支持多种视频网站(B站、公司内部学习平台、未来 YouTube 等)。
> 通过 `PlatformAdapter` 抽象实现**平台无关**,每个平台实现自己的适配器。
> 三级降级在每个平台内部独立执行。

**三级降级**(每个平台内部):

| 优先级 | 名称 | 通道 | 输出 |
|---|---|---|---|
| ① | 平台 API | 平台公开 API(httpx) | `SubtitleResult(source="api")` |
| ② | Puppeteer 通用浏览器 | 用户 Chrome `--remote-debugging-port` | `SubtitleResult(source="browser")` |
| ③ | 浏览器录屏 + Whisper | Screen Recorder 扩展 + ffmpeg + faster-whisper | `SubtitleResult(source="whisper")` |

**降级规则**:① miss → ②;② miss → ③;③ fail → `None`(走 transcribe_fail 记录)。

| ID | 描述 | 优先级 |
|---|---|---|
| FR-2.0 | **平台适配器抽象**:`PlatformAdapter` Protocol 含 `match(url)` + 3 个 fetch 方法;每个平台实现一个 adapter | P0 |
| FR-2.1 | **策略 ①**(B站):B站官方 CC 字幕 API,httpx 调 `api.bilibili.com/x/player/v2` | P0 |
| FR-2.2 | **Puppeteer 连接**:用 playwright `connect_over_cdp("http://localhost:9222")` 连用户 Chrome,**复用用户登录态** | P0 |
| FR-2.3 | **后台标签页**:`context.new_page()` 创建后台标签页,**不抢用户焦点**;完成即 `page.close()` | P0 |
| FR-2.4 | **策略 ②**(通用):Puppeteer 通用 JS 探测,**4 种方法按优先级尝试**,首个命中即返回 | P0 |
| FR-2.5 | **JS 探测方法 1**:HTML5 `<track kind="subtitles" / kind="captions">` 标签,拿 `src` 下载解析 | P0 |
| FR-2.6 | **JS 探测方法 2**:`window.__INITIAL_STATE__` / `window.__INITIAL_DATA__` 递归找字幕 URL 字段 | P0 |
| FR-2.7 | **JS 探测方法 3**:`window.player.getSubtitle()` / `window.player.subtitle` / `window.player.on('subtitle_update')` | P1 |
| FR-2.8 | **JS 探测方法 4**:DOM 选择器扫描字幕文本(`[class*="subtitle"]`、`[class*="caption"]` 等) | P2 |
| FR-2.9 | **B站语言优先级**:`zh-Hans > zh-CN > zh-Hant > en-US > en > ai-zh`;`ai-zh` 是 B站 AI 实时字幕,质量次于官方 CC | P0 |
| FR-2.10 | **跨域处理**:`page.evaluate(fetch)` 只用于同 origin(B站 page 取 page 自身 API);跨 origin 字幕 URL 用 `context.request.get()`(走浏览器 network stack,带 cookie,无 CORS 限制) | P0 |
| FR-2.11 | **字幕格式统一**:无论 API 返回什么,统一 dump 成 `.srt` 后用 `pysrt` 解析;Puppeteer 取数据时已是 `.srt` / `.vtt` / `.json` / `.ass` 之一 | P0 |
| FR-2.12 | **字幕来源记录**:metadata.source `api` / `browser` / `whisper` | P0 |
| FR-2.13 | **失败日志区分**:策略 ② miss 不记 transcribe_fail(走下一级);策略 ③ Whisper 失败才记 transcribe_fail.csv | P0 |
| FR-2.14 | **策略 ③**(录屏触发):Puppeteer 触发 Screen Recorder for Google Chrome 扩展,使用预设快捷键 `Control+Shift+R` | P0 |
| FR-2.15 | **策略 ③**(录屏停止):视频时长结束或定时器触发,再次模拟同一快捷键停止;等待 WebM 文件落地到 Chrome 下载目录 | P0 |
| FR-2.16 | **策略 ③**(音频输入):Screen Recorder 扩展自带抽音,输出文件(WebM / OGG / MP3)直接送 `faster-whisper` 转写;**我们不调 ffmpeg** | P0 |
| FR-2.17 | **BilibiliAdapter**:实现 FR-2.1/2.2/2.4,`match` 域名匹配 `bilibili.com` / `b23.tv` | P0 |
| FR-2.18 | **InternalSiteAdapter**(占位):接口留 stub,等公司下发账号后实现 `match` 域名 + fetch 方法;当前抛 `NotImplementedError` | P1 |
| FR-2.19 | **通用 fallback adapter**:未知 URL 域名时,跳过策略 ①,直接走 ② + ③ | P1 |
| FR-2.20 | **降级路径**:任一策略失败都降级到下一级,不跳过当前视频;仅 ③ 失败才算"字幕提取失败" | P0 |
| FR-2.21 | **弹窗语义变更**:不再有"启用浏览器插件"弹窗(改成 Puppeteer 自动连用户 Chrome);保留失败日志上限弹窗(FR-6.6) | P0 |
| FR-2.22 | **录屏文件清理**:Whisper 转写成功后,WebM + WAV 立即删除;失败保留供排查 | P0 |

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
  └─ ③ adapter.fetch_via_recording(driver, url, duration_sec)
       ├─ page.goto(url)
       ├─ page.keyboard.press("Control+Shift+R")  # Screen Recorder 开始
       ├─ await asyncio.sleep(duration_sec + 5)
       ├─ page.keyboard.press("Control+Shift+R")  # 停止
       ├─ 监听 Chrome 下载目录,等 .webm 落地
       ├─ Screen Recorder 输出文件(WebM/OGG)
       ├─ faster-whisper 直读(内部 ffmpeg 解码)
       └─ SubtitleResult(source="whisper")
```

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
        """策略 ③:Puppeteer 触发录屏扩展 + Whisper。"""
```

**已验证 spike**(2026-09-01,`scripts/spike_browser_subtitle.py`):

| 项 | 结果 |
|---|---|
| `playwright.connect_over_cdp("http://localhost:9222")` | ✅ 通(独立 user-data-dir=`/tmp/vla-chrome-debug`) |
| `page.goto(B站 URL)` 后台标签页 | ✅ 不抢焦点 |
| `page.evaluate(fetch player/v2)` | ✅ 拿到 `subtitles count=1` |
| `context.request.get(subtitle_url)` | ✅ status 200,跨 origin 通过 |
| body[] 长度 | 1143 条中文 AI 字幕(`ai-zh`) |
| dump 到 `.srt` | ✅ 72947 bytes / 4571 行 |
| 独立 profile 没 B站登录 | ⚠️ 字幕是 `ai-zh`(AI 实时字幕)非官方 CC;但 spike 验证了通道 |

**安全性约束**(沿用):
- 字幕永远本地(策略 ①② 完全本地调用,策略 ③ 完全本地 Whisper)
- 不引入云端转写
- 云端 LLM 仅用于:① 字幕质量检查(FR-4) ② 6h 批量总结(FR-5)

**Phase 3 代码改动**:
- `subtitle/bilibili_official.py`:保留,作为 `BilibiliAdapter.fetch_api_subtitle` 实现
- `subtitle/browser_plugin.py`:废弃(原"扫描 VideoTrans 目录"设计作废),仅保留 `parse()` 方法给 Puppeteer 取到字幕文件时用
- `subtitle/strategy.py`:重写,从"扫描 + 弹窗"改为"adapter 三级降级"
- 新增 `subtitle/platform_adapter.py`:Protocol + Registry
- 新增 `subtitle/bilibili_adapter.py`:BilibiliAdapter 实现
- 新增 `subtitle/internal_site_adapter.py`:InternalSiteAdapter stub
- 新增 `subtitle/browser_driver.py`:Puppeteer driver + 通用 JS 探测
- 新增 `source/browser_record.py`:录屏触发 + 监听下载 + Whisper 直读(扩展自带抽音)

### FR-3 流式转写与磁盘管理

| ID     | 描述                                                | 优先级 |
| ------ | ------------------------------------------------- | --- |
| FR-3.1 | Whisper 引擎使用 **faster-whisper**(开源本地)             | P0  |
| FR-3.2 | 模型可选 `tiny/base/small/medium/large-v3`,默认 `small` | P0  |
| FR-3.3 | 边转写边清理:转写完的音频段不再保留原片                              | P0  |
| FR-3.4 | 磁盘峰值占用 ≤ 1 GB(远低于 256 GB 总容量)                     | P0  |
| FR-3.5 | 转写失败必须记录到 `transcribe_fail.csv`,**不删除**视频源        | P0  |

### FR-4 质量门控

| ID     | 描述                                               | 优先级 |
| ------ | ------------------------------------------------ | --- |
| FR-4.1 | 字幕转写后调用**云端订阅模型**做质量检查                           | P0  |
| FR-4.2 | 检查项:通顺度、完整性、准确性、重复异常                             | P0  |
| FR-4.3 | 启发式预筛:语速 < 1 字/秒 或 > 15 字/秒直接判失败                 | P0  |
| FR-4.4 | 质量分 ≥ 70 才算通过                                    | P0  |
| FR-4.5 | **通过** → 删除视频源 + 进入总结队列                          | P0  |
| FR-4.6 | **未通过** → 保留视频源 + 记录到 `quality_fail.csv` + 单独存文本 | P0  |

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

| 类型 | 触发 | 方式 |
|------|------|------|
| **A. 阻塞弹窗** | ① 启用浏览器插件(用户必须介入);② 失败日志达到上限(汇总告知) | `display dialog` 阻塞 |
| **B. 非阻塞通知** | 进度类(质量通过 / session 开始 / 总结触发 / session 结束) | `display notification` 横幅 |
| **C. 静默(仅日志)** | ① 转写失败 ② 质量不过关 | **不通知**,只写 CSV + 终端 print |

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
| FR-7.3 | `logs/failed_texts/*.txt` - 失败的字幕原文 | P0 |
| FR-7.4 | 每条日志含:时间戳、视频ID、标题、URL、阶段、错误 | P0 |
| FR-7.5 | 提供 `vla logs show` 查看失败摘要 | P1 |
| FR-7.6 | 提供 `vla retry --from <csv>` 重试失败视频 | P1 |

### FR-8 录屏与音频

| ID | 描述 | 优先级 |
|----|------|--------|
| FR-8.1 | 使用 `ffmpeg` + `avfoundation` 录屏 | P0 |
| FR-8.2 | **同时录制系统音频**(策略 B:录浏览器窗口 + 系统音频) | P0 |
| FR-8.3 | 编码 `libx264 preset=ultrafast CRF=28` 节约空间 | P0 |
| FR-8.4 | 自动检测屏幕 index,配置文件可覆盖 | P1 |

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
| NFR-5  | 云端 API 仅用于:① 字幕质量检查 ② 最终总结               |
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
│   └── video_source.py     # 视频源工厂(下载 OR 录屏)
│
├── subtitle/
│   ├── __init__.py
│   ├── strategy.py         # 三级策略调度
│   ├── bilibili_official.py# 策略 ① B站官方 CC
│   └── browser_plugin.py   # 策略 ② VideoTrans 导出文件
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
          2. 可下载 → _download()
          3. 不可下载 → _record_screen()(异步启动)
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

#### `transcribe/streaming.py`

```python
class StreamingTranscriber:
    def __init__(self, model_size: str, log: TranscriptionLog): ...

    def transcribe_video(
        self,
        video_path: Path,
        duration_sec: int,
    ) -> str:
        """
        步骤:
          1. ffmpeg 切音轨 → .wav(单声道,16kHz)
          2. faster-whisper.transcribe()
          3. 删视频源 .mp4(无论后续如何,音频保留)
        """

    def cleanup(self, *paths: Path) -> None:
        """质量通过后,删音频"""
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
    def info(self, title: str, message: str) -> None: ...
    def warning(self, title: str, message: str) -> None: ...
    def alert(
        self, title: str, message: str, buttons: tuple[str, ...] = ("OK",)
    ) -> str: ...
    def ask_open_browser(self, title: str, url: str) -> bool:
        """阻塞弹窗,用户点'已开启'返回 True"""
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
  ├─→ [SubtitleStrategy.get_subtitle]  ←── 内含弹窗逻辑
  │     │
  │     ├─ ① B站官方 CC API
  │     │    ├─ 成功 → SubtitleResult(source="official")
  │     │    └─ 失败/无字幕 ↓
  │     │
  │     ├─ ② 浏览器插件:扫描目录
  │     │    ├─ 已有文件 → SubtitleResult(source="plugin", trigger=scan_hit)
  │     │    └─ 无文件 ↓
  │     │
  │     ├─ ② 浏览器插件:macOS 弹窗
  │     │    ├─ 用户"已开启" → 阻塞等文件出现
  │     │    │    ├─ 文件出现 → SubtitleResult(source="plugin", trigger=user_opened)
  │     │    │    └─ 超时无文件 → return None
  │     │    ├─ 用户"跳过该视频" → return None
  │     │    └─ 弹窗超时未响应 → return None
  │     │
  │     └─ return None(降级信号,**非转写失败**)
  │
  ├─→ [若 strategy 返回 None]
  │     │
  │     └─→ [VideoSourceFactory.get]
  │           ├─ yt-dlp simulate OK → yt-dlp download(最低画质)
  │           └─ yt-dlp fail → ffmpeg + avfoundation 录屏(策略 B)
  │
  ├─→ [StreamingTranscriber.transcribe_video]
  │     ├─ ffmpeg 切音轨 → .wav
  │     ├─ 删视频源 .mp4(立即,无论后续)
  │     └─ faster-whisper → text
  │
  ├─→ [QualityChecker.check]
  │     ├─ 启发式预筛(字/秒 + 重复)
  │     │    ├─ 异常 → QualityResult(passed=False)
  │     │    └─ 正常 ↓
  │     ├─ 云端 LLM 检查(可选)
  │     └─ 返回 QualityResult
  │
  ├─→ [分支]
  │     ├─ passed=True
  │     │    ├─ [StreamingTranscriber.cleanup] 删 .wav
  │     │    ├─ [LLMSummarizer.summarize] 500-800 字 Markdown
  │     │    └─ 追加到 notes.md
  │     │
  │     └─ passed=False
  │          ├─ [TranscriptionLog.log_quality_fail]
  │          ├─ 保留 .wav + 存 failed_texts/
  │          └─ macOS warning 弹窗
  │
  └─ 输出:None(继续下一条)
```

### 字幕降级路径(FR-2 关键语义)

```text
SubtitleStrategy.get_subtitle()
  │
  ├─ ① 命中 → return SubtitleResult(source="official")
  │
  └─ ① 失败/无字幕 → 进入 ②
        │
        ├─ ② 扫描命中 → return SubtitleResult(source="plugin")
        │
        └─ ② 扫描无 → 弹窗
              │
              ├─ 用户"已开启" + 文件出现 → return source="plugin"
              ├─ 用户"已开启" + 等文件超时 → return None
              ├─ 用户"跳过该视频" → return None
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
  prefer_download: true
  download:
    format: "worst"       # 最差画质,节约带宽
  record:
    enabled: true
    screen_index: 1       # ffmpeg -list_devices 查
    fps: 30
    crf: 28
    audio_input: "1:0"    # screen:audio(策略 B:带系统音频)
    preset: "ultrafast"

quality_check:
  enabled: true
  model: "gpt-4o-mini"    # 云端订阅
  min_score_to_pass: 70
  min_char_per_second: 1.0
  max_char_per_second: 15.0

browser_plugin:
  name: "VideoTrans"
  enabled: true
  remind_timeout_sec: 30          # 弹窗等用户响应的超时(秒)
  plugin_paths:
    - "~/Documents/VideoTrans/subtitles"
    - "~/Downloads"
  # FR-2.9/2.10 插件状态:整个 session 只弹一次
  #   available   用户确认启动 + 等到文件
  #   unavailable 用户跳过 / 弹窗超时 / 字幕质量不过关
  # 状态保存在内存,下次启动重置

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

### AC-1 字幕三级策略

- [ ] 给一个**有 CC 字幕的 B站视频**,运行 `vla process`,字幕来源标记为 `official`。
- [ ] 给一个**无 CC 字幕 + 模拟插件目录有对应文件**的视频,来源标记为 `plugin`。
- [ ] 给一个**既无 CC 也无插件文件 + 用户点"已开启"后等文件超时**的视频,自动下载或录屏,来源标记为 `whisper`,**且 `transcribe_fail.csv` 不增加行**(关键:FR-2.8)。
- [ ] 给一个**弹窗超时未响应**的视频,自动降级到策略 ③,来源标记为 `whisper`,继续正常走完质量门控与总结。

### AC-2 磁盘管理

- [ ] 处理 1 小时视频全程,**tmp 目录峰值** < 1 GB。
- [ ] 处理完成后,**通过的字幕对应视频源已被删除**。
- [ ] 失败的字幕对应视频源**仍然保留**(可手动清理)。

### AC-3 质量门控

- [ ] 转写一段**静音视频**,QualityResult.passed=False,记录到 `quality_fail.csv`,视频源未删。
- [ ] 转写一段**正常视频**,QualityResult.passed=True,记录不出现,视频源删除,总结写入笔记。

### AC-4 通知与日志

- [ ] 触发插件策略时,macOS 弹出对话框,点"已开启"继续,点"跳过该视频"**降级到策略 ③**(不写 transcribe_fail)。
- [ ] **弹窗超时未响应** → 自动降级到策略 ③,字幕来源标记为 `whisper`,**`transcribe_fail.csv` 不增加记录**。
- [ ] `transcribe_fail.csv` 和 `quality_fail.csv` 行数与实际失败数一致。
- [ ] `failed_texts/` 下能找到失败的字幕原文(仅质量不过关时存)。
- [ ] **转写失败 / 质量不过关时,不弹通知**(FR-6.4),仅终端 print + 写 CSV。
- [ ] **浏览器插件启用弹窗** 是 session 内唯一的阻塞弹窗(用户必须介入)。
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

### AC-9 插件状态机

- [ ] 第一次需要插件时弹窗,后续整 session 不再弹窗(除非显式重置)。
- [ ] 用户"已开启"且等到文件 → 状态 `available`,后续视频直接扫描。
- [ ] 用户"跳过" / 弹窗超时 / 字幕质量不过关 → 状态 `unavailable`,后续视频直接走策略 ③,不弹窗。
- [ ] 状态 `unavailable` 后,**整 session 不再尝试**插件路径。
- [ ] session 重启(下次运行) → 状态重置为 `unknown`。

---

## 十二、风险与限制

| 风险                 | 说明                | 缓解                       |
| ------------------ | ----------------- | ------------------------ |
| macOS 屏幕录制权限       | 首次运行需用户在系统设置授权    | README 写明首次启动步骤          |
| ffmpeg 屏幕 index 变化 | 多屏设备 index 可能改变   | 配置可覆盖,提供 `vla doctor` 检测 |
| B站风控               | 频繁下载 / 高频请求可能触发限流 | 下载间隔 + UA / Referer 完整   |
| Whisper 模型体积       | large-v3 约 3 GB   | 默认 small(500 MB),按需切换    |
| 云端 API 余额          | 总结调用耗 token       | GPT-4o-mini 极便宜,可忽略      |
| 字幕版权               | 录屏可能含视频音轨         | 仅供个人学习,不得传播              |

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
