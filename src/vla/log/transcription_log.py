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
import logging
import re
from datetime import datetime
from pathlib import Path

from ..models import QualityResult


logger = logging.getLogger(__name__)


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


# ---------------- 落盘目录(唯一来源,2026-09-10) ----------------
#
# 日期分组逻辑此前散在两处:`TranscriptionLog.__init__` 写
# `<log_dir>/transcribed/<date>/transcripts|summaries`,而
# `transcribe/streaming.py` 另写一套**扁平**的 `<log_dir>/transcripts/` ——
# 于是 `.transcript.txt` / `.refined.txt` 与正式产物落在两棵不同的树上。
# 统一为下面三个函数,调用方一律引用,不再各算各的日期。


def dated_root_for(log_dir: Path, when: datetime | None = None) -> Path:
    """`<log_dir>/transcribed/<YYYY-MM-DD>` —— 日期分组根。"""
    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    return Path(log_dir) / "transcribed" / stamp


def transcripts_dir_for(log_dir: Path, when: datetime | None = None) -> Path:
    """`<log_dir>/transcribed/<YYYY-MM-DD>/transcripts` —— 字幕落盘目录。

    正式产物 `<id>_<title>.txt`、原始 `.transcript.txt`、Level 4 `.refined.txt`
    **全部**落这里。注意 `LLMSummarizer._load_items` 对该目录是无条件
    `glob("*.txt")`,所以后两者必须由 summarizer 侧排除(见其 `_DERIVED_SUFFIXES`)。
    """
    return dated_root_for(log_dir, when) / "transcripts"


def summaries_dir_for(log_dir: Path, when: datetime | None = None) -> Path:
    """`<log_dir>/transcribed/<YYYY-MM-DD>/summaries` —— 摘要落盘目录。"""
    return dated_root_for(log_dir, when) / "summaries"


class TranscriptionLog:
    """转写日志 + 字幕原文落盘。

    目录布局(2026-09-10 改为 date + type 双层分组,参考 audio_downloads/):
        <log_dir>/transcribed/<YYYY-MM-DD>/
            ├── transcripts/<id>_<title>.txt         # 转写 + 质量门控后的字幕原文
            └── summaries/<id>_<title>.summary.txt   # 长视频单视频摘要(FR-2.15d)

    字段语义:
    - `transcribed_root` = `<log_dir>/transcribed` (整棵树,总结读盘走 rglob)
    - `transcribed_dir`  = `<log_dir>/transcribed/<today>/transcripts` (本次写盘目录)
    - `summaries_dir`    = `<log_dir>/transcribed/<today>/summaries` (summary 写盘目录)
    """

    # 转写链路的中间产物后缀(2026-09-10)。与 `llm_summarizer._DERIVED_SUFFIXES`
    # 是同一批文件的两端:那边是「读 6h 总结时要排除什么」,这里是「质量通过后要丢弃什么」。
    _INTERMEDIATE_SUFFIXES = (".transcript.txt", ".refined.txt")

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = Path(log_dir)
        self.transcribed_root = self.log_dir / "transcribed"
        # 同一个 when 派生两棵子树:避免跨零点时 transcripts 落今天、summaries 落明天
        when = datetime.now()
        self.transcribed_dir = transcripts_dir_for(self.log_dir, when)
        self.summaries_dir = summaries_dir_for(self.log_dir, when)
        self.failed_texts_dir = self.log_dir / "failed_texts"
        # 初始化时建好子目录,让 save_* 路径上不存在不需要 mkdir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.transcribed_dir.mkdir(parents=True, exist_ok=True)
        self.summaries_dir.mkdir(parents=True, exist_ok=True)
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

    def discard_transcribe_intermediates(self, stem: str) -> list[Path]:
        """质量**通过**后丢弃 `<stem>.transcript.txt` / `<stem>.refined.txt`。

        2026-09-10 用户裁定:中间产物只在质量**未通过**时留作诊断证据
        (`.refined.txt` 可能带 Refiner 降级的 `# notes: <原因>`,failed_texts/
        里没有这个信息 —— 失败时要答得上「是转写烂还是 Refiner 烂」)。
        通过后正式产物已含最终文本,二者纯冗余 ⇒ 丢弃(磁盘友好)。

        **本方法只该在质量通过、且正式产物写盘成功之后调用** —— 调用方
        (`main_provider.process_asset` Step 5)负责这个时序。写盘失败时先删,
        中间产物就成了唯一剩下的副本。

        Args:
            stem: 转写时的音频文件名(不含后缀),即 `asset.audio_path.stem`;
                  内部站路径下等于 task.id,与 `streaming.transcribe` 的
                  `stem = audio_path.stem` 同源。

        Returns:
            实际被删的路径列表(文件本就不存在则为空 —— **幂等,不抛**)。
        """
        # 用 transcripts_dir_for **当场算**,不用 self.transcribed_dir(那是
        # __init__ 快照):批量跨零点时快照会指向昨天,而 transcriber 是转写那刻
        # 现算的,两者不一致就删不中。
        transcripts_dir = transcripts_dir_for(self.log_dir)
        removed: list[Path] = []
        for suffix in self._INTERMEDIATE_SUFFIXES:
            path = transcripts_dir / f"{stem}{suffix}"
            try:
                if path.exists():
                    path.unlink()
                    removed.append(path)
            except OSError as e:
                # 清理失败不该影响主流程(字幕已通过质量并落盘)
                logger.warning("丢弃中间产物失败 %s: %s", path, e)
        return removed

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
        n_summaries = len(list(self.summaries_dir.glob("*.txt")))
        n_failed_texts = len(list(self.failed_texts_dir.glob("*.txt")))
        return (
            f"transcribe_fail: {n_transcribe_fail} | "
            f"quality_fail: {n_quality_fail} | "
            f"transcribed: {n_transcribed} | "
            f"summaries: {n_summaries} | "
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