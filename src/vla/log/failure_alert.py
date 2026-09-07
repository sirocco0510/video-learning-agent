"""FailureAlert(SSOT: requirements.md FR-6.6 + implementation-plan.md Phase 7.6)。

累计失败条数(transcribe_fail + quality_fail)达到 threshold 整数倍时
弹一次阻塞式汇总,只在跨过倍数边界时才弹,避免每条都打扰用户。
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable, Protocol


logger = logging.getLogger(__name__)


class _TranscriptionLogLike(Protocol):
    """TranscriptionLog 子集(避免反向依赖具体类)。"""
    @property
    def log_dir(self) -> Path: ...

    def count_total_failures(self) -> int: ...
    def transcribe_fail_count(self) -> int: ...
    def quality_fail_count(self) -> int: ...


class _NotifierLike(Protocol):
    """MacOSNotifier 子集。"""
    def alert_blocking(
        self,
        title: str,
        message: str,
        detail_button: str | None = None,
        detail_action: Callable[[], None] | None = None,
        timeout_sec: int = 60,
    ) -> None: ...


class FailureAlert:
    """FR-6.6:失败日志上限弹窗(累计监控 + 倍数边界)。

    用法:
        alert = FailureAlert(threshold=50, log=tlog, notifier=notifier)
        tlog.log_transcribe_fail(...)
        alert.check_after_write()  # 在每次 log_*_fail 后调用

    阈值倍数语义:
        - total_fail = count_total_failures()
        - current_multiple = total_fail // threshold
        - current_multiple > last_alerted_multiple → 弹窗(并更新)
        - 否则静默

    跨进程持久化:目前不持久化 last_alerted_multiple(每次进程启动重置);
    改进路径:写到 log_dir/.alert_state.json,下次启动读回。
    """

    def __init__(
        self,
        threshold: int,
        log: _TranscriptionLogLike,
        notifier: _NotifierLike,
        enabled: bool = True,
    ) -> None:
        if threshold <= 0:
            raise ValueError(f"threshold must be > 0, got {threshold}")
        self.threshold = threshold
        self.log = log
        self.notifier = notifier
        self.enabled = enabled
        # 跨过的最大倍数边界(避免每条都弹)
        self.last_alerted_multiple: int = 0

    def check_after_write(self) -> None:
        """在每次写 transcribe_fail / quality_fail 后调用。

        累计失败 >= threshold * k → 弹窗(仅第一次跨过 k)
        0 ≤ k * threshold 区间内 → 静默
        """
        if not self.enabled:
            return
        total_fail = self.log.count_total_failures()
        current_multiple = total_fail // self.threshold
        if current_multiple > self.last_alerted_multiple:
            self.last_alerted_multiple = current_multiple
            self._alert(total_fail)

    def reset(self) -> None:
        """重置 last_alerted_multiple(主要用于测试)。"""
        self.last_alerted_multiple = 0

    def _alert(self, total_fail: int) -> None:
        """阻塞式弹窗 + 提供查看日志入口。"""
        n_transcribe = self.log.transcribe_fail_count()
        n_quality = self.log.quality_fail_count()
        breakdown = (
            f"转写失败 {n_transcribe} 条 + "
            f"质量失败 {n_quality} 条"
        )
        self.notifier.alert_blocking(
            title="⚠️ 失败积累过多",
            message=(
                f"已积累 {total_fail} 条失败({breakdown}),"
                f"请检查 logs/ 目录下的 CSV 与原文。"
            ),
            detail_button="查看 logs/",
            detail_action=self._reveal_logs,
            timeout_sec=60,
        )

    def _reveal_logs(self) -> None:
        """macOS Finder 打开 logs 目录。"""
        try:
            subprocess.run(
                ["open", str(self.log.log_dir)],
                check=False, capture_output=True, text=True, timeout=5,
            )
        except Exception as e:
            logger.warning("FailureAlert 打开 logs 目录失败: %s", e)