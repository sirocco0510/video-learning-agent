"""配额管理(SSOT: requirements.md FR-9 + implementation-plan.md Phase 7.5)。

设计(2026-09 收敛):
- 2026-09 前:维护 in-memory window 列表 + drain()
- 2026-09 后:Phase 7 从磁盘 transcribed/ 读盘,QuotaManager 只负责累加秒数 + 触发判断,
  不再持有 window list(drain() 返回总秒数,主调度用 transcribed_dir 调 summarize_batch)
"""

from __future__ import annotations

import logging

from vla.config import VLAConfig


logger = logging.getLogger(__name__)


class QuotaManager:
    """累计时长配额 + 触发判断。

    Attributes:
        threshold_sec: 触发阈值(默认 21600 = 6h)
        current: 当前窗口已累加秒数
    """

    def __init__(self, cfg: VLAConfig) -> None:
        self._threshold: int = cfg.quota.summary_threshold_sec
        self._on_exhausted: str = cfg.quota.on_exhausted
        self.current: int = 0

    @property
    def threshold(self) -> int:
        return self._threshold

    def add(self, duration_sec: int) -> bool:
        """累加时长,返回是否触发总结。

        Args:
            duration_sec: 当前视频时长(秒)

        Returns:
            True → 触发总结(主调度调 summarize_batch)
            False → 未触发,继续下一个视频
        """
        self.current += duration_sec
        return self.current >= self._threshold

    def should_summarize(self) -> bool:
        """当前累加是否 >= 阈值(给 UI 用,不修改状态)。"""
        return self.current >= self._threshold

    def should_continue(self) -> bool:
        """触发后是否继续 session。

        Returns:
            True → 继续(on_exhausted=summary_then_continue)
            False → 停止(on_exhausted=stop_session,默认)
        """
        if not self.should_summarize():
            return True
        return self._on_exhausted == "summary_then_continue"

    def drain(self) -> int:
        """取出累计时长并清零。返回的是本窗口的总秒数(供主调度日志/统计用)。

        总结后的清理由 LLMSummarizer.clear_after 控制(transcribed_dir/*.txt),
        QuotaManager 只管秒数计数器。
        """
        total = self.current
        self.current = 0
        logger.info("📊 配额窗口已重置(本窗口总计 %d 秒)", total)
        return total

    @property
    def progress(self) -> float:
        """当前进度(0.0 ~ 1.0),供 UI 显示。"""
        if self._threshold == 0:
            return 1.0
        return min(self.current / self._threshold, 1.0)