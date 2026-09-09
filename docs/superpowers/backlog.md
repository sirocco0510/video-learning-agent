# Backlog — 视频学习代理未实现的扩展项

> 配合 2026-09-09 Asset Pipeline 拆法重构(详见 `specs/2026-09-09-asset-pipeline-design.md`)。
> 本轮 producer-consumer 拆法刻意不做这些;列在这里方便后续 review 时直接挑起。
> 排序按"用户当前关注度"降序:ready 检测(已纳入本轮 spec §4.5)> worker 池 > 3 路径清理 > 截图异步。
>
> **状态更新(2026-09-09)**:
> - **F2-10 ready 检测(原项 1)** → **移到 spec §4.5**,本轮落地(方案 A 简化版:mtime 宽限默认 5s,可配置;`cfg.audio.scan_grace_period_sec`)
> - 截图异步化(项 4)→ **user deferred**(本轮不做,等 worker 池一起评估)
> - **新增 §6 内部源 start / near-end 截图**(用户 2026-09-09 提出,spec §4.7 截图需求处理段)
> - 本 backlog 现在 5 项 + §1 已纳入:worker 池 / 3 路径清理 / 截图异步 / 工厂统一 / 内部源截图

---

## 1. ~~F2-10 scan_today_dir ready 检测~~ → **已纳入 spec §4.5**

本轮实施,见 `specs/2026-09-09-asset-pipeline-design.md` §4.5。
- `cfg.audio.scan_grace_period_sec`(默认 5)
- `audio_scan.py::scan_untranscribed_audio` 加 mtime 宽限参数
- 后续 v2 升级:接 `.ready` sidecar 方案作为更稳的兜底(见 backlog §2 新编号)

---

## 2. FR-2.27 Worker 池异步化(中优,producer-consumer 上限)

**问题**:
- 当前 `main.run()` 串行循环,一条视频的"抽音 + 转写 + 质量 + LLM"全阻塞下一条
- 文档已写(F2-7 阶段),`audio/queue.py` + `audio/worker_pool.py` 文件框架在,但实际未接入
- 串行下用户跑 10 条视频可能要 30 分钟;并发 2 worker 可压到 15 分钟

**方案**:
- 沿用 FR-2.27 spec:`AudioQueue(asyncio.Queue, 容量 10)` + `WhisperWorkerPool(默认 2 worker,cfg.whisper.concurrent_workers 可配)`
- 主调度 `main.run()` 改为调度器:`fetch_asset` 后入队,不等 worker 直接接下一条
- 每个 worker 跑完整的处理链(`process_asset` 内部逻辑):transcribe + quality + refine + save + cleanup
- quality LLM 调用需限流(LLM API 有 rate limit,worker 池不能无脑并发)

**为什么本轮不做**:
- 用户选"不并发,纯拆" — Asset 接口是数据类,已为入队留口子
- worker 池是独立大改,跨 quality LLM 限流、screenshot 顺序、配额统计多个点
- 单独 PR 更好 review

**触发条件**:
- 跑 >5 条视频 / session 时
- LLM API 限流不是瓶颈时(否则先限流再上池)

**优先级**:P1(优化,非必需)

**关联文件**:
- `audio/queue.py`(框架)
- `audio/worker_pool.py`(框架)
- `transcribe/streaming.py`(已可入队)
- `quality/checker.py`(LLM 限流 hook)

---

## 3. FR-2.22 三路径音频清理(中优,与 worker 池解耦)

**问题**:
- 当前实现只覆盖 FR-3.7 两条:pass → unlink,fail → keep
- FR-2.22 文档写的三路径:
  - pass → 立即 unlink ✓(已实现)
  - quality_fail → 保留 24h 后由 `failure_alert.py` 后台清理线程删 ✗(未实现,现在永远留)
  - transcribe_fail → 文件进 `logs/audio_failed/` 永久归档 ✗(未实现,现在直接被下一次 fetch_asset 覆盖)
- 副作用:长期跑会话会累积 1GB+ 残留 wav/webm

**方案**:
- `process_asset` 内根据结果路由:
  - pass → `audio_path.unlink()`(当前逻辑)
  - quality_fail → `audio_path` 不动,标记 `mtime`,`failure_alert` 后台线程 24h 清
  - transcribe_fail → `audio_path.rename(audio_failed_dir / audio_path.name)`,永久归档
- 目录约定:`logs/audio_raw/<bvid>.wav`(活跃)/ `logs/audio_failed/<bvid>.wav`(永久)
- 24h 清理线程走 `failure_alert.py` 已有结构

**为什么本轮不做**:
- 不影响主链路(本轮拆法只是把 audio_path 决策挪到 process_asset,决策规则本身不动)
- 三路径涉及目录结构 + 后台线程,需单独 PR
- 与 ready 检测关联:扫描已转写过的 wav 时,可能踩到 audio_failed 里的同名文件

**触发条件**:
- 磁盘占用监测告警
- 任何"我跑了很久 vla,磁盘怎么满了"的用户反馈

**优先级**:P1(磁盘友好,NFR)

**关联文件**:
- `log/failure_alert.py`(扩展后台线程)
- `transcribe/streaming.py` / `process_asset` 内部 unlink 逻辑

---

## 4. Phase C 截图异步化(低优,与 worker 池绑定) — **user deferred**

**问题**:
- Phase C 截图当前在 `main._process_one` 同步 await,失败 log warning 不阻塞
- 截图是纯 side effect(不参与后续逻辑),理论上可 fire-and-forget
- 跟 Phase A 不同:Phase A 失败 → 决策 A(跳视频),必须等结果

**方案**:
- 单任务内:`asyncio.create_task(self._screenshot.phase_c_end(...))`,不 await
- 主调度 `main.run()` 末尾 `await asyncio.gather(*pending_phase_c_tasks)` 等所有截图收尾
- 跨任务并发:等 FR-2.27 worker 池后,screenshot scheduler 作为独立 worker 跑,观察 `processing_queue` 状态触发

**为什么本轮不做**:
- 截图本身 <1s,省不下多少时间
- 单任务内 create_task 不 await,会跟下一条 Phase A 争 screenshot index.jsonl / 浏览器 page
- 真并发需 worker 池配套,本轮没启

**触发条件**:
- 跑 worker 池时一起做
- 截图变成瓶颈(基本不会)

**优先级**:P2(架构整洁,非性能瓶颈)

**关联文件**:
- `main.py::_process_one`
- `capture/screenshot_phase_controller.py`

---

## 5. video_source vs audio_source 工厂统一(低优,代码整洁)

**问题**:
- `vla/source/video_source.py::VideoSourceFactory`(老,下载完整 MP4)与 `vla/audio/source_factory.py::AudioSourceFactory`(新,yt-dlp -x 抽音)**并存**
- `main_provider.py::RealTextProvider` 兜底分支仍用 `VideoSourceFactory`
- FR-2.14 重构 v3 计划砍掉 MP4 下载,统一走 AudioSourceFactory,**未完全落地**

**方案**:
- `RealTextProvider` 兜底分支改用 `AudioSourceFactory`
- 删 `VideoSourceFactory`(或留作 legacy import,标 deprecated)
- 同步改 `main.py` / `spike_f26_pipeline.py` 装配

**为什么本轮不做**:
- 拆法 1 不动工厂层,只动 text_provider 边界
- 老工厂仍在用,e2e 测试还依赖,删前要补新测试
- 工厂统一属于 cleanup,不是新功能

**触发条件**:
- 任何"我应该用哪个工厂"的开发困惑
- e2e 测试重构时顺手做

**优先级**:P3(代码整洁,无功能影响)

**关联文件**:
- `src/vla/source/video_source.py`(legacy)
- `src/vla/audio/source_factory.py`(新)
- `src/vla/main_provider.py::RealTextProvider`

---

## 6. 内部源 start / near-end 截图(2026-09-09 用户提出,P2)

**问题**:
- 内部学习平台(`b-learning.bill-jc.com`)走 API-driven spider(详见 spec §4.7),
  本轮为它实现的是纯 m3u8 抽音路径,**不开浏览器**
- 用户提出后续希望截图:开始学习后(start)+ 即将结束前(near-end),用于:
  - start:记录学习起点(参考 B 站 Phase A)
  - near-end:记录学习结尾(参考 B 站 Phase C)
- 截图目的:留证学习过程、debug 抽取质量、检测页面卡死

**方案**(待实施):
- InternalSiteSpider 加 `screenshot_start` / `screenshot_near_end` 子流程
- 需要新开 BrowserDriver + page(目前 spec §4.7 明确"不开浏览器")
- 截图落 `tmp/screenshots/<kngId>/{start,near_end}.png`,索引到 `screenshots/index.jsonl`
- 复用 B 站截图链路:`ScreenshotPhaseController.phase_a_start` + `phase_c_end` 抽象
- main.py `_process_one` Step 0/4 加 internal 源分支(URL scheme 或 source 字段判定)

**为什么本轮不做**:
- spec §4.7 明确"内部源现 spec 不为内部源实现截图;若以后需要,可在 InternalSiteSpider 加 browser 子流程(再开 BrowserDriver)"
- 用户明示 2026-09-09 本轮不做,等本轮落地后单开 PR
- 拆法 1 不动 InternalSiteSpider,只扩 fetch_asset 接住 metadata contract

**触发条件**:
- 内部源日播放量 > 5 时(截图才有 debug 价值)
- 用户提"我想看到学习进度的截图"

**优先级**:P2(用户体验,非必需)

**关联文件**:
- `src/vla/subtitle/internal_site_spider.py`(扩展 browser 子流程)
- `src/vla/capture/screenshot_phase_controller.py`(复用抽象)
- `src/vla/main.py::_process_one`(Step 0/4 加 internal 源分支)
