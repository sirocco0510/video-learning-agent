"""VideoLearningAgent 主调度(SSOT: requirements.md 第七章 数据流 + Phase 8)。

设计(2026-09 收敛):
- 主流程:去重 → 取字幕 → 质量 → 通过(save_transcribed + cleanup)/ 失败(save_failed_text + 保留 audio)
- 配额触发:6h → summarize_batch(transcribed_dir) → 写 notes_file → 清空 transcribed
- 依赖注入:checker / log / history / quota / summarizer / plugin_status / notifier 全是 Protocol
- text_provider 可调用对象负责"取字幕"(Phase 3 字幕三级策略 + Phase 2 转写在更外层组装)
  签名: (task: VideoTask) → (text: str, source: str, audio_path: Path | None)
  返回 audio_path 让主调度知道质量失败时音频是否需要保留
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Protocol

from vla.config import VLAConfig
from vla.models import QualityResult, VideoTask
from vla.state.history import HistoryManager
from vla.state.plugin_status import PluginStatus
from vla.state.quota import QuotaManager


logger = logging.getLogger(__name__)


# ---------------- 依赖协议 ----------------


class QualityCheckerLike(Protocol):
    """质量门控(QualityChecker)。"""

    def check(self, text: str, title: str, duration_sec: int, model_size: str) -> QualityResult: ...


class NotifierLike(Protocol):
    """通知(MacOSNotifier — info / warning)。"""

    def info(self, title: str, message: str) -> None: ...

    def warning(self, title: str, message: str) -> None: ...


# text_provider 返回类型(text, source, audio_path_or_None)
SubtitleResult = tuple[str, str, Path | None]
TextProvider = Callable[[VideoTask], SubtitleResult]


# ---------------- 主类 ----------------


class VideoLearningAgent:
    """主调度:处理 list[VideoTask],带去重/配额/质量门控。"""

    def __init__(
        self,
        cfg: VLAConfig,
        checker: QualityCheckerLike,
        log: "TranscriptionLogLike",
        history: HistoryManager,
        quota: QuotaManager,
        summarizer: "LLMSummarizerLike",
        notifier: NotifierLike,
        text_provider: TextProvider,
        plugin_status: PluginStatus | None = None,
    ) -> None:
        self.cfg = cfg
        self.checker = checker
        self.log = log
        self.history = history
        self.quota = quota
        self.summarizer = summarizer
        self.notifier = notifier
        self.text_provider = text_provider
        self.plugin_status = plugin_status or PluginStatus()
        # transcribed_dir(Phase 7 读盘需要)
        self.transcribed_dir = log.transcribed_dir

    # ---------------- 主流程 ----------------

    def run(self, tasks: list[VideoTask]) -> dict[str, int]:
        """主流程。返回统计 {processed, passed, failed, skipped, summarized}。"""
        stats = {"processed": 0, "passed": 0, "failed": 0, "skipped": 0, "summarized": 0}

        # 1. 去重(FR-9.6)
        pending = [t for t in tasks if not self.history.is_already_done(self._url_key(t))]
        skipped = len(tasks) - len(pending)
        stats["skipped"] = skipped
        if skipped:
            logger.info("⏭️ 跳过 %d 个已转写视频", skipped)
        if not pending:
            logger.info("✅ 所有视频都已转写,无需处理")
            return stats

        for task in pending:
            stats["processed"] += 1

            # 2. 处理单条
            passed = self._process_one(task)

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

        return stats

    # ---------------- 单条处理 ----------------

    def _process_one(self, task: VideoTask) -> str | None:
        """处理单条视频。

        Returns:
            source 字符串("whisper" / "api" / "browser")表示成功
            None 表示失败(已 log 到 log_quality_fail 或 log_transcribe_fail)
        """
        # 1. 取字幕 + (可选)音频路径
        try:
            text, source, audio_path = self.text_provider(task)
        except Exception as e:
            # 取字幕完全失败(网络 / 录屏异常等)
            self.log.log_transcribe_fail(
                task.id, task.title, str(task.url),
                "text_provider", str(e),
            )
            return None

        # 2. 质量门控
        qr = self.checker.check(
            text=text,
            title=task.title,
            duration_sec=task.expected_duration,
            model_size=self.cfg.whisper.model,
        )

        # 3. 失败分支
        if not qr.passed:
            self.log.log_quality_fail(
                task.id, task.title, str(task.url), qr, text,
            )
            # FR-2.11:插件字幕质量不过关 → 标 unavailable
            # (source 取值:"api"/"browser"/"whisper";浏览器源 = 插件字幕)
            if source == "browser":
                self.plugin_status.mark_unavailable(reason="plugin_quality_fail")
                logger.warning("⚠️ 插件字幕质量不过关,降级到 Whisper")
            return None

        # 4. 通过 → save_transcribed(FR-4.5 + FR-7.7)
        self.log.save_transcribed(
            video_id=task.id,
            title=task.title,
            text=text,
            quality=qr,
            source=source,
            duration_sec=task.expected_duration,
        )
        # 5. 清理音频(FR-3.7 + FR-4.5)
        if audio_path is not None and audio_path.exists():
            audio_path.unlink()
            logger.info("🗑️ 清理音频: %s", audio_path)

        # 6. 进度通知(B级)
        self.notifier.info(
            "✓ 质量通过",
            f"{task.title}({qr.score}分),已加入总结队列",
        )
        return source

    # ---------------- 总结触发 ----------------

    def _trigger_summary(self, group_title: str | None) -> bool:
        """配额触发时调 summarize_batch,写盘 + 清空 transcribed。"""
        self.quota.drain()
        content = self.summarizer.summarize_batch(
            self.transcribed_dir,
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

    def log_quality_fail(self, video_id: str, title: str, url: str, result: QualityResult, text: str) -> None: ...
    def save_transcribed(self, video_id: str, title: str, text: str, quality: QualityResult, source: str, duration_sec: int) -> Path: ...
    def log_transcribe_fail(self, video_id: str, title: str, url: str, stage: str, error: str) -> None: ...


class LLMSummarizerLike(Protocol):
    """LLMSummarizer 子集。"""

    def summarize_batch(self, transcribed_dir: Path, group_title: str | None = None, clear_after: bool = True) -> str: ...
    def write_to_notes(self, content: str) -> None: ...