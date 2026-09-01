"""转写 / 质量日志(SSOT: requirements.md 第六章 6.1 log/transcription_log.py)。

⚠️ 本文件是 **Phase 2 stub**,只暴露接口 + no-op 实现,
让 Phase 2 VideoSourceFactory 可以正常 inject。

完整实现(CSV 写入、failure_alert 触发等)在 Phase 6 落地。
"""

from pathlib import Path

from ..models import QualityResult


class TranscriptionLog:
    """Phase 2 stub:接口 + no-op 实现。Phase 6 填完整功能。"""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    def log_transcribe_fail(
        self,
        video_id: str,
        title: str,
        url: str,
        stage: str,
        error: str,
    ) -> None:
        """Phase 2 stub:Phase 6 实现 CSV 写入。"""
        return None

    def log_quality_fail(
        self,
        video_id: str,
        title: str,
        url: str,
        result: QualityResult,
        text: str,
    ) -> None:
        """Phase 2 stub:Phase 6 实现 CSV 写入。"""
        return None

    def summary(self) -> str:
        """Phase 2 stub:返回空串;Phase 6 实现真实汇总。"""
        return ""
