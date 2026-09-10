"""VideoLearningAgent 主调度(SSOT: requirements.md 第七章 数据流 + Phase 8)。

设计(2026-09 收敛):
- 主流程:去重 → fetch_asset(输入链) → process_asset(处理链) → 通知
- 配额触发:6h → summarize_batch(transcribed_dir) → 写 notes_file → 清空 transcribed
- 依赖注入:checker / log / history / quota / summarizer / plugin_status / notifier 全是 Protocol
- 输入链 + 处理链(spec §4.4):
  fetch_asset: VideoTask → Asset | None  (字幕策略 / 抽音 / 兜底 scan)
  process_asset: (Asset, VideoTask) → ProcessResult | None  (转写 / 质量 / Refine / save / cleanup)

v3.2.1 (F2-6 root cause fix):run / _process_one / _start_chrome_session /
_stop_chrome_session 全部 async。ScreenshotController 的 phase_a_start / phase_c_end
也是 async(直接 await,不通过 SyncScreenshotWrapper)。BrowserDriver.connect /
disconnect 仍是 sync Playwright API,内部用 asyncio.to_thread 包一下不阻塞 loop。

v3.3 (Task 10 / 2026-09-09 asset-pipeline-refactor):
text_provider 单 callable 拆成 fetch_asset + process_asset 双 callable。
main.py 不再直接调 quality_check / Refine / save_transcribed / cleanup — 这些都
下沉到 main_provider.process_asset 内部。main.py 只负责调度(Phase A/C 截图 +
通知 + 配额 + history)。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from vla.config import VLAConfig
from vla.log.failure_alert import FailureAlert
from vla.models import Asset, ProcessResult, QualityResult, VideoTask
from vla.state.history import HistoryManager
from vla.state.plugin_status import PluginStatus
from vla.state.quota import QuotaManager


logger = logging.getLogger(__name__)


# ---------------- 依赖协议 ----------------


class NotifierLike(Protocol):
    """通知(NotifierLike — MacOSNotifier on darwin / NullNotifier 其他平台)。

    2026-09-10 轻量化:跨平台 notifier 由 `vla.ui.notifier.create_notifier()` 装配,
    Windows/Linux 走 NullNotifier(静默 + 默认按钮)。
    """

    def info(self, title: str, message: str) -> None: ...

    def warning(self, title: str, message: str) -> None: ...


class SubtitleRefinerLike(Protocol):
    """v3.2:字幕 Refiner 子集(用于 quality.passed 后调优)。

    改前:StreamingTranscriber 内部注入 refiner,不论质量都跑
    改后:由 main.py 显式调,quality_fail → 不调(节省 L4 token)
    """

    def refine(self, text: str, title: str): ...


class BrowserDriverLike(Protocol):
    """v3.2 (F2-6.1):Session 级 Chrome driver 子集。

    v3.2.1:connect / disconnect 仍是 sync Playwright 实现,
    VideoLearningAgent 内部用 asyncio.to_thread 包一下不阻塞 loop。
    """

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...


class ScreenshotControllerLike(Protocol):
    """v3.2.1 (F2-6.1):Phase A/C 截图控制子集(async)。

    原 SyncScreenshotWrapper 已删除 — VideoLearningAgent 整体 async,
    直接 await controller.phase_a_start / phase_c_end。
    v3.2.1 (F2-6.1.1):接受 url,controller 自己创建 page,避开 Playwright
    sync/async loop 冲突。
    """

    async def phase_a_start(self, *, url: str, video_id: str, title: str, duration_sec: int) -> None: ...
    async def phase_c_end(self, *, url: str, video_id: str, title: str, duration_sec: int) -> None: ...


# 输入链 + 处理链(spec §4.4)
FetchAssetFn = Callable[[VideoTask], Awaitable[Asset | None]]
ProcessAssetFn = Callable[[Asset, VideoTask], Awaitable[ProcessResult | None]]


# ---------------- 主类 ----------------


class VideoLearningAgent:
    """主调度:处理 list[VideoTask],带去重/配额/质量门控。"""

    def __init__(
        self,
        cfg: VLAConfig,
        log: "TranscriptionLogLike",
        history: HistoryManager,
        quota: QuotaManager,
        summarizer: "LLMSummarizerLike",
        notifier: NotifierLike,
        fetch_asset: FetchAssetFn,
        process_asset: ProcessAssetFn,
        plugin_status: PluginStatus | None = None,
        failure_alert: FailureAlert | None = None,
        refiner: SubtitleRefinerLike | None = None,
        browser_driver: BrowserDriverLike | None = None,
        screenshot_controller: ScreenshotControllerLike | None = None,
    ) -> None:
        self.cfg = cfg
        self.log = log
        self.history = history
        self.quota = quota
        self.summarizer = summarizer
        self.notifier = notifier
        # 输入链 + 处理链(v3.3 / Task 10)
        self.fetch_asset = fetch_asset
        self.process_asset = process_asset
        self.plugin_status = plugin_status or PluginStatus()
        # v3.2 (FR-3.9):Refiner 在 main.py 显式调,quality.passed 后才跑
        self.refiner = refiner
        # v3.2.1 (F2-6.1):截图关键路径(FR-2.28),async 协议
        self._browser_driver = browser_driver
        self._screenshot = screenshot_controller
        self._chrome_session_ready = False  # _start_chrome_session 后才置 True
        # transcribed_dir(本次写盘,今日 transcripts/)+ transcribed_root(整棵树,总结读盘)
        self.transcribed_dir = log.transcribed_dir
        self.transcribed_root = log.transcribed_root
        # FR-6.6:失败上限弹窗(默认按 cfg.logging 构造)
        self.failure_alert = failure_alert or FailureAlert(
            threshold=cfg.logging.log_alert_threshold,
            log=log,  # type: ignore[arg-type]  # duck typing:TranscriptionLog 满足 _TranscriptionLogLike
            notifier=notifier,  # type: ignore[arg-type]  # NotifierLike 满足 _NotifierLike
            enabled=cfg.logging.log_alert_enabled,
        )

    # ---------------- Session 启停 ----------------

    async def _start_chrome_session(self) -> None:
        """F2-6.1 (FR-2.28):Session 启 Chrome(失败 → 降级无截图)。

        v3.2.1.3 fix: connect() 必须不在 main asyncio thread 跑 — Playwright
        sync API 创建的 `_UnixSelectorEventLoop` 会劫持 thread-local asyncio
        policy,撞 asyncio loop → 死锁。但 connect 又必须和后续 page 操作走
        同一个 thread(driver.executor worker),否则 dispatcher fiber 不一致。

        修法: 把 connect() submit 到 driver 自己的 executor (`loop.run_in_executor`)
        — driver.executor 是 single-worker ThreadPoolExecutor,sequential submit
        复用同一 worker → dispatcher fiber 与 page 操作同 thread。
        """
        if not self.cfg.chrome_session.enabled:
            return
        if self._browser_driver is None:
            logger.info("📸 chrome_session.enabled=true 但未注入 browser_driver,跳过截图")
            return
        try:
            # v3.2.1.3: BrowserDriver.connect() 内部已启专用后台 thread +
            # connect_in_thread,然后同步等 startup 信号。在 main asyncio thread
            # 同步调 connect() 不会撞 asyncio loop,因为真正的 Playwright 调用在
            # 那个后台 daemon thread 里跑。
            await asyncio.to_thread(self._browser_driver.connect)
            self._chrome_session_ready = True
            logger.info("✓ Chrome Session 已连接,启用截图关键路径")
        except Exception as e:
            logger.warning("⚠️ Chrome Session 启动失败,降级无截图模式继续:%s", e)

    async def _stop_chrome_session(self) -> None:
        if self._browser_driver is None:
            return
        try:
            await asyncio.to_thread(self._browser_driver.disconnect)
        except Exception:
            pass
        self._chrome_session_ready = False

    # ---------------- 主流程 ----------------

    async def run(self, tasks: list[VideoTask]) -> dict[str, int]:
        """主流程。返回统计 {processed, passed, failed, skipped, summarized}。

        v3.2.1:async,因为 Step 0/5 的截图调用是 await。
        """
        stats = {"processed": 0, "passed": 0, "failed": 0, "skipped": 0, "summarized": 0}

        # 0. F2-6.1:Session 启 Chrome(失败降级无截图)
        await self._start_chrome_session()

        # 1. 去重(FR-9.6)
        pending = [t for t in tasks if not self.history.is_already_done(self._url_key(t))]
        skipped = len(tasks) - len(pending)
        stats["skipped"] = skipped
        if skipped:
            logger.info("⏭️ 跳过 %d 个已转写视频", skipped)
        if not pending:
            logger.info("✅ 所有视频都已转写,无需处理")
            await self._stop_chrome_session()
            return stats

        for task in pending:
            stats["processed"] += 1

            # 2. 处理单条
            passed = await self._process_one(task)

            # 3. 通过 → 写 history + 累加配额
            if passed:
                stats["passed"] += 1
                self.history.record_success(
                    url_key=self._url_key(task),
                    title=task.title,
                    duration_sec=task.expected_duration,
                    group_id=task.group_id,
                    source=passed,
                )
                # 配额判断
                triggered = self.quota.add(task.expected_duration)
                if triggered:
                    summarized = self._trigger_summary(group_title=task.group_title)
                    if summarized:
                        stats["summarized"] += 1
                    # 4. 触发后判断是否继续(检查 on_exhausted 策略)
                    if self._should_stop_after_trigger():
                        logger.info("🛑 配额已满且 on_exhausted=stop_session,session 结束")
                        break
            else:
                stats["failed"] += 1

        # F2-6.1:Session 清理 Chrome
        await self._stop_chrome_session()

        return stats

    # ---------------- 单条处理 ----------------

    async def _process_one(self, task: VideoTask) -> str | None:
        """处理单条视频(spec §4.4 改薄后版本)。

        v3.3 (Task 10) 流水线:
          Step 0: Phase A 截图(失败 → 跳过视频,决策 A)
          Step 1: fetch_asset(task) → Asset | None(输入链)
          Step 2: process_asset(asset, task) → ProcessResult | None(处理链)
                  — 内部已含质量门控 / Refine / save_transcribed / wav cleanup
          Step 3: 通知 + Phase C 截图(失败 → log warning 不阻塞)

        Returns:
            source 字符串("whisper_scan" / "api" / "browser" / "whisper_download" /
            "whisper_internal_download")表示成功;
            None 表示失败(已 log 到 log_quality_fail 或 log_transcribe_fail)
        """
        # Step 0: 关键路径 Phase A 开头截图(FR-2.28 决策 A)
        if self._screenshot is not None and self._chrome_session_ready:
            try:
                await self._screenshot.phase_a_start(
                    url=str(task.url),
                    video_id=task.id,
                    title=task.title,
                    duration_sec=task.expected_duration,
                )
            except Exception as e:
                # 决策 A:Phase A 失败 → 跳过本视频
                logger.warning(
                    "📸 Phase A 截图失败,跳过视频 %s:%s", task.title, e,
                )
                self.failure_alert.check_after_write()
                return None

        # Step 1: 输入链 — fetch_asset 返回 Asset 或 None(全失败)
        asset = await self.fetch_asset(task)
        if asset is None:
            self.log.log_transcribe_fail(
                task.id, task.title, str(task.url),
                stage="fetch_asset", error="all paths exhausted",
            )
            # FR-6.6:累计失败倍数边界检查
            self.failure_alert.check_after_write()
            return None

        # Step 2: 处理链 — 转写 / 质量 / Refine / save / cleanup 都在 process_asset 内
        # 失败分支(质量 / 转写)已在 process_asset 内部 log_quality_fail / log_transcribe_fail
        result = await self.process_asset(asset, task)
        if result is None:
            # FR-6.6:累计失败倍数边界检查
            self.failure_alert.check_after_write()
            return None

        # Step 3: 进度通知(B级)
        self.notifier.info(
            "✓ 质量通过",
            f"{task.title}({result.qr.score}分),已加入总结队列",
        )

        # Step 4: 关键路径 Phase C 末尾截图(FR-2.28)
        # 失败 → log warning,不阻塞(决策 A 只针对 Phase A)
        if self._screenshot is not None and self._chrome_session_ready:
            try:
                await self._screenshot.phase_c_end(
                    url=str(task.url),
                    video_id=task.id,
                    title=task.title,
                    duration_sec=task.expected_duration,
                )
            except Exception as e:
                logger.warning(
                    "📸 Phase C 截图失败,主流程继续 %s:%s", task.title, e,
                )

        return result.source

    # ---------------- 总结触发 ----------------

    def _trigger_summary(self, group_title: str | None) -> bool:
        """配额触发时调 summarize_batch,写盘 + 清空 transcribed。"""
        self.quota.drain()
        content = self.summarizer.summarize_batch(
            self.transcribed_root,
            group_title=group_title,
            clear_after=True,
        )
        if not content:
            logger.info("📭 transcribed/ 为空,跳过总结")
            return False
        self.summarizer.write_to_notes(content)
        logger.info("📝 总结已写入 %s", self.cfg.summary.notes_file)
        self.notifier.info(
            "🎉 已累计 6 小时",
            f"总结已生成 → {self.cfg.summary.notes_file}",
        )
        return True

    # ---------------- 工具 ----------------

    def _url_key(self, task: VideoTask) -> str:
        return HistoryManager.make_url_key(task.group_id, task.id)

    def _should_stop_after_trigger(self) -> bool:
        """配额刚被触发过 → 是否停止?根据 on_exhausted 配置。

        注意:drain() 在 _trigger_summary 里已经把 current 清零,所以这里不能直接用
        quota.should_summarize();改成用配额配置 + 触发过的事实判断。
        """
        return self.cfg.quota.on_exhausted == "stop_session"


# ---------------- 协议(测试 stub 用) ----------------


class TranscriptionLogLike(Protocol):
    """TranscriptionLog 子集(避免 main.py 反向依赖具体类)。"""

    @property
    def transcribed_dir(self) -> Path: ...

    @property
    def transcribed_root(self) -> Path: ...

    def log_quality_fail(self, video_id: str, title: str, url: str, result: QualityResult, text: str) -> None: ...
    def save_transcribed(self, video_id: str, title: str, text: str, quality: QualityResult, source: str, duration_sec: int) -> Path: ...
    def log_transcribe_fail(self, video_id: str, title: str, url: str, stage: str, error: str) -> None: ...


class LLMSummarizerLike(Protocol):
    """LLMSummarizer 子集。"""

    def summarize_batch(self, transcribed_dir: Path, group_title: str | None = None, clear_after: bool = True) -> str: ...
    def write_to_notes(self, content: str) -> None: ...