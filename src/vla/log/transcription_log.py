"""转写 / 质量日志(SSOT: requirements.md 第六章 6.1 + implementation-plan.md Phase 6)。

职责:
- log_transcribe_fail()  → transcribe_fail.csv(转写失败,FR-7.1)
- log_quality_fail()     → quality_fail.csv + failed_texts/<id>_<title>.txt(FR-7.2/7.3)
- save_transcribed()     → transcribed/<id>_<title>.txt(FR-7.7,2026-09 新增,Phase 7 总结读盘)
- save_failed_text()     → failed_texts/<id>_<title>.txt(FR-7.3,显式保存)
- summary()              → 人类可读计数

设计:
- CSV 写入用 stdlib csv 模块,正确处理逗号/引号/换行
- 时间戳 ISO8601 本地时区,精确到秒
- safe_title 静态方法做文件名清洗
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from ..models import QualityResult


# 转写失败 CSV 列
_TRANSCRIBE_FAIL_HEADER = ["timestamp", "video_id", "title", "url", "stage", "error"]

# 质量失败 CSV 列
_QUALITY_FAIL_HEADER = [
    "timestamp", "video_id", "title", "url", "score", "issues", "suggestion"
]

# 文件名禁用字符(Windows + macOS + Linux)
_INVALID_FILENAME_CHARS = re.compile(r'[\\\/:\*\?"<>\|\x00-\x1f]')

# 文件名最大长度(避免 OS 限制)
_MAX_TITLE_LEN = 30


def _safe_title(title: str, max_chars: int = _MAX_TITLE_LEN) -> str:
    """清洗标题使其可作为文件名:
    - 替换 / \\ : * ? \" < > | 控制字符 → _
    - 折叠连续空白为单空格
    - 截断到 max_chars(默认 30)
    """
    cleaned = _INVALID_FILENAME_CHARS.sub("_", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(".")
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned or "untitled"


def _now_iso() -> str:
    """本地时区 ISO8601 时间戳,秒精度。"""
    return datetime.now().isoformat(timespec="seconds")


class TranscriptionLog:
    """转写日志 + 字幕原文落盘。"""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = Path(log_dir)
        self.transcribed_dir = self.log_dir / "transcribed"
        self.failed_texts_dir = self.log_dir / "failed_texts"
        # 初始化时建好子目录,让 save_* 路径上不存在不需要 mkdir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.transcribed_dir.mkdir(parents=True, exist_ok=True)
        self.failed_texts_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 失败日志 ----------------

    def log_transcribe_fail(
        self,
        video_id: str,
        title: str,
        url: str,
        stage: str,
        error: str,
    ) -> None:
        """追加一行到 transcribe_fail.csv(FR-3.5/7.1)。"""
        path = self.log_dir / "transcribe_fail.csv"
        row = [_now_iso(), video_id, title, url, stage, error]
        self._append_csv(path, _TRANSCRIBE_FAIL_HEADER, row)

    def log_quality_fail(
        self,
        video_id: str,
        title: str,
        url: str,
        result: QualityResult,
        text: str,
    ) -> None:
        """追加一行到 quality_fail.csv(FR-7.2)+ 存原文到 failed_texts/(FR-7.3)。"""
        # CSV
        csv_path = self.log_dir / "quality_fail.csv"
        row = [
            _now_iso(),
            video_id,
            title,
            url,
            result.score,
            "; ".join(result.issues),
            result.suggestion,
        ]
        self._append_csv(csv_path, _QUALITY_FAIL_HEADER, row)
        # 失败原文(冗余保底:即使没有显式 save_failed_text 也存了)
        reason = f"质量分数 {result.score}/100(< 阈值)"
        if result.issues:
            reason += f";问题:{';'.join(result.issues[:3])}"
        self.save_failed_text(video_id, title, text, reason)

    # ---------------- 字幕原文落盘 ----------------

    def save_transcribed(
        self,
        video_id: str,
        title: str,
        text: str,
        quality: QualityResult,
        source: str,
        duration_sec: int,
    ) -> Path:
        """委托给 transcribed_file.write,保留 FR-7.7 落盘行为。"""
        from vla.log.transcribed_file import TranscribedItem, write as write_transcribed
        safe = _safe_title(title)
        path = self.transcribed_dir / f"{video_id}_{safe}.txt"
        item = TranscribedItem(
            title=title,
            source=source,
            quality_score=quality.score,
            duration_sec=duration_sec,
            text=text,
            path=path,
            mtime=0.0,
        )
        return write_transcribed(path, item)

    def save_failed_text(
        self,
        video_id: str,
        title: str,
        text: str,
        reason: str,
    ) -> Path:
        """存失败字幕原文到 failed_texts/(FR-7.3,供人工审核)。

        Returns:
            写入的文件路径
        """
        safe = _safe_title(title)
        path = self.failed_texts_dir / f"{video_id}_{safe}.txt"
        header = f"# {title}\n失败原因:{reason}\n\n"
        path.write_text(header + text, encoding="utf-8")
        return path

    # ---------------- 汇总 ----------------

    def summary(self) -> str:
        """返回人类可读的计数摘要。"""
        n_transcribe_fail = self._count_csv_rows(self.log_dir / "transcribe_fail.csv")
        n_quality_fail = self._count_csv_rows(self.log_dir / "quality_fail.csv")
        n_transcribed = len(list(self.transcribed_dir.glob("*.txt")))
        n_failed_texts = len(list(self.failed_texts_dir.glob("*.txt")))
        return (
            f"transcribe_fail: {n_transcribe_fail} | "
            f"quality_fail: {n_quality_fail} | "
            f"transcribed: {n_transcribed} | "
            f"failed_texts: {n_failed_texts}"
        )

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _append_csv(path: Path, header: list[str], row: list[str]) -> None:
        """追加一行 CSV,文件不存在时先写 header。"""
        is_new = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(header)
            writer.writerow(row)

    @staticmethod
    def _count_csv_rows(path: Path) -> int:
        """数据行数(不含 header);文件不存在返回 0。"""
        if not path.exists():
            return 0
        with path.open(encoding="utf-8") as f:
            # 用 csv reader 跳过 header
            reader = csv.reader(f)
            try:
                next(reader)  # header
            except StopIteration:
                return 0
            return sum(1 for _ in reader)

    # ---------------- 计数(FR-6.6) ----------------

    def count_total_failures(self) -> int:
        """FR-6.6:所有失败条数 = transcribe_fail + quality_fail。"""
        return self.transcribe_fail_count() + self.quality_fail_count()

    def transcribe_fail_count(self) -> int:
        return self._count_csv_rows(self.log_dir / "transcribe_fail.csv")

    def quality_fail_count(self) -> int:
        return self._count_csv_rows(self.log_dir / "quality_fail.csv")