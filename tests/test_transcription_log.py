"""TranscriptionLog 测试(SSOT: requirements.md 6.1 + implementation-plan.md Phase 6)。

设计:
- log_transcribe_fail() → 追加到 transcribe_fail.csv(列:timestamp, id, title, url, stage, error)
- log_quality_fail() → 追加到 quality_fail.csv + 存原文到 failed_texts/<id>_<title短>.txt
- save_transcribed() → 存原文到 transcribed/<id>_<title短>.txt(FR-7.7,2026-09 新增)
- save_failed_text() → 存原文到 failed_texts/<id>_<title短>.txt(FR-7.3,显式调用场景)
- 目录不存在时自动创建
- 标题做 safe_filename 清洗(/ \\ : * ? \" < > | → _)
"""

import csv
import re
from pathlib import Path

import pytest

from vla.log.transcription_log import TranscriptionLog
from vla.models import QualityResult


# ---------------- Fixtures ----------------


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs"


@pytest.fixture
def transcriber_log(log_dir: Path) -> TranscriptionLog:
    return TranscriptionLog(log_dir)


@pytest.fixture
def sample_quality() -> QualityResult:
    return QualityResult(
        passed=False,
        score=45,
        issues=["重复段落过多", "专业术语不准确"],
        suggestion="建议重新转写或人工修正",
        char_count=600,
    )


# ---------------- 构造 + 目录创建 ----------------


class TestConstruct:
    def test_creates_subdirs_on_init(self, log_dir):
        """构造时自动创建 transcribed/ 和 failed_texts/ 子目录。"""
        assert not log_dir.exists()
        TranscriptionLog(log_dir)
        assert (log_dir / "transcribed").is_dir()
        assert (log_dir / "failed_texts").is_dir()

    def test_accepts_existing_log_dir(self, log_dir):
        """已存在的 log_dir 不报错。"""
        log_dir.mkdir(parents=True)
        log = TranscriptionLog(log_dir)
        assert log.log_dir == log_dir


# ---------------- log_transcribe_fail ----------------


class TestLogTranscribeFail:
    def test_creates_csv_with_header(self, transcriber_log, log_dir):
        """首次调用 → 写 header + 1 行数据。"""
        transcriber_log.log_transcribe_fail(
            video_id="BV1xxx",
            title="测试视频",
            url="https://www.bilibili.com/video/BV1xxx",
            stage="whisper",
            error="模型未找到",
        )
        csv_path = log_dir / "transcribe_fail.csv"
        assert csv_path.exists()
        content = csv_path.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        assert len(lines) == 2  # header + 1 row
        # header
        assert "timestamp" in lines[0]
        assert "video_id" in lines[0]
        assert "title" in lines[0]
        assert "url" in lines[0]
        assert "stage" in lines[0]
        assert "error" in lines[0]
        # row
        assert "BV1xxx" in lines[1]
        assert "whisper" in lines[1]
        assert "模型未找到" in lines[1]

    def test_appends_multiple_rows(self, transcriber_log, log_dir):
        """多次调用 → header 1 次,数据行累加。"""
        for i in range(3):
            transcriber_log.log_transcribe_fail(
                video_id=f"v{i}",
                title=f"t{i}",
                url=f"https://x/{i}",
                stage="download",
                error=f"err{i}",
            )
        csv_path = log_dir / "transcribe_fail.csv"
        lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 4  # header + 3 rows

    def test_timestamp_is_iso8601(self, transcriber_log, log_dir):
        """timestamp 列是 ISO8601 格式。"""
        transcriber_log.log_transcribe_fail(
            video_id="v1", title="t", url="https://x", stage="x", error="x"
        )
        first_row = (log_dir / "transcribe_fail.csv").read_text(encoding="utf-8").splitlines()[1]
        # ISO8601:2026-09-02T10:30:45
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", first_row.split(",")[0])

    def test_csv_quote_special_chars(self, transcriber_log, log_dir):
        """标题含逗号 / 引号 → 走 CSV quoting,不破坏列结构。"""
        transcriber_log.log_transcribe_fail(
            video_id="v1",
            title='测试"含逗号,的,标题',
            url="https://x",
            stage="whisper",
            error="err",
        )
        csv_path = log_dir / "transcribe_fail.csv"
        # csv 模块能正确解析回来
        with csv_path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            row = next(reader)
        assert row["title"] == '测试"含逗号,的,标题'


# ---------------- log_quality_fail ----------------


class TestLogQualityFail:
    def test_creates_csv_and_text_file(self, transcriber_log, log_dir, sample_quality):
        """log_quality_fail → 写 quality_fail.csv + failed_texts/<id>_<title>.txt。"""
        transcriber_log.log_quality_fail(
            video_id="BV1xxx",
            title="Python 教程",
            url="https://www.bilibili.com/video/BV1xxx",
            result=sample_quality,
            text="这是一段转写文本,质量不过关。",
        )
        csv_path = log_dir / "quality_fail.csv"
        assert csv_path.exists()
        # text file
        text_files = list((log_dir / "failed_texts").glob("*.txt"))
        assert len(text_files) == 1
        text_file = text_files[0]
        assert text_file.stem.startswith("BV1xxx_")
        assert "Python" in text_file.stem
        # text 内容
        content = text_file.read_text(encoding="utf-8")
        assert "Python 教程" in content  # 标题在 header
        assert "这是一段转写文本" in content  # 正文

    def test_csv_columns_match_quality_result(self, transcriber_log, log_dir, sample_quality):
        """quality_fail.csv 列含 score / issues / suggestion。"""
        transcriber_log.log_quality_fail(
            video_id="v1", title="t", url="https://x",
            result=sample_quality, text="...",
        )
        csv_path = log_dir / "quality_fail.csv"
        with csv_path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            row = next(reader)
        assert int(row["score"]) == 45
        assert "重复段落过多" in row["issues"]
        assert row["suggestion"] == "建议重新转写或人工修正"

    def test_appends_multiple_rows(self, transcriber_log, log_dir, sample_quality):
        """多次调用 → header 1 次,数据行累加。"""
        for i in range(3):
            transcriber_log.log_quality_fail(
                video_id=f"v{i}", title=f"t{i}", url="https://x",
                result=sample_quality, text="x",
            )
        csv_path = log_dir / "quality_fail.csv"
        lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 4


# ---------------- save_transcribed (FR-7.7,2026-09 新增) ----------------


class TestSaveTranscribed:
    def test_writes_text_to_transcribed_dir(self, transcriber_log, log_dir, sample_quality):
        """save_transcribed → 写 transcribed/<id>_<title短>.txt,header 含来源/质量/时长。"""
        transcriber_log.save_transcribed(
            video_id="BV1xxx",
            title="Python 装饰器教程",
            text="这是通过质量检查的字幕正文。",
            quality=sample_quality,
            source="whisper",
            duration_sec=1800,
        )
        text_files = list((log_dir / "transcribed").glob("*.txt"))
        assert len(text_files) == 1
        f = text_files[0]
        assert f.stem.startswith("BV1xxx_")
        content = f.read_text(encoding="utf-8")
        # header
        assert "# Python 装饰器教程" in content
        assert "whisper" in content
        assert "45/100" in content
        assert "1800s" in content
        # 正文
        assert "这是通过质量检查的字幕正文。" in content

    def test_overwrites_existing_file(self, transcriber_log, log_dir, sample_quality):
        """同名文件存在 → 覆盖(同 id+title 不应该产生两份)。"""
        for _ in range(2):
            transcriber_log.save_transcribed(
                video_id="v1", title="t", text="新文本", quality=sample_quality,
                source="whisper", duration_sec=100,
            )
        text_files = list((log_dir / "transcribed").glob("*.txt"))
        assert len(text_files) == 1

    def test_safe_title_sanitizes_special_chars(self, transcriber_log, log_dir, sample_quality):
        """标题含 / \\ : * ? \" < > | → 替换为 _。"""
        transcriber_log.save_transcribed(
            video_id="v1", title='教程:Python/进阶\\核心*知识?"<>|',
            text="x", quality=sample_quality, source="whisper", duration_sec=100,
        )
        text_files = list((log_dir / "transcribed").glob("*.txt"))
        assert len(text_files) == 1
        # 文件名应该只含字母数字/中文/_/-
        for char in ["/", "\\", ":", "*", "?", '"', "<", ">", "|"]:
            assert char not in text_files[0].name

    def test_safe_title_truncates_long_titles(self, transcriber_log, log_dir, sample_quality):
        """超长标题(>30 字) → 截断,避免文件路径过长。"""
        long_title = "长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长长"  # 50 字
        transcriber_log.save_transcribed(
            video_id="v1", title=long_title, text="x",
            quality=sample_quality, source="whisper", duration_sec=100,
        )
        text_files = list((log_dir / "transcribed").glob("*.txt"))
        assert len(text_files) == 1
        # 文件 stem 应该在合理范围(<80 字符)
        assert len(text_files[0].stem) < 80


# ---------------- save_failed_text (FR-7.3,显式调用) ----------------


class TestSaveFailedText:
    def test_writes_text_to_failed_texts_dir(self, transcriber_log, log_dir):
        """save_failed_text → 写 failed_texts/<id>_<title>.txt,header 含失败原因。"""
        transcriber_log.save_failed_text(
            video_id="BV1xxx",
            title="失败视频",
            text="质量不过关的字幕。",
            reason="语速异常 cps=0.5",
        )
        text_files = list((log_dir / "failed_texts").glob("*.txt"))
        assert len(text_files) == 1
        f = text_files[0]
        content = f.read_text(encoding="utf-8")
        assert "# 失败视频" in content
        assert "语速异常" in content
        assert "质量不过关的字幕" in content

    def test_log_quality_fail_also_saves_text(self, transcriber_log, log_dir, sample_quality):
        """log_quality_fail 内部已调 save_failed_text(冗余校验)。"""
        transcriber_log.log_quality_fail(
            video_id="v1", title="t", url="https://x",
            result=sample_quality, text="original text",
        )
        text_files = list((log_dir / "failed_texts").glob("*.txt"))
        assert len(text_files) == 1
        assert "original text" in text_files[0].read_text(encoding="utf-8")


# ---------------- summary ----------------


class TestSummary:
    def test_returns_counts(self, transcriber_log, log_dir, sample_quality):
        """summary() 返回带计数的人类可读摘要。"""
        transcriber_log.log_transcribe_fail("v1", "t1", "u", "x", "e")
        transcriber_log.log_transcribe_fail("v2", "t2", "u", "x", "e")
        transcriber_log.log_quality_fail("v3", "t3", "u", sample_quality, "x")
        transcriber_log.save_transcribed("v4", "t4", "x", sample_quality, "whisper", 100)

        s = transcriber_log.summary()
        assert "transcribe_fail: 2" in s
        assert "quality_fail: 1" in s
        assert "transcribed: 1" in s
        assert "failed_texts: 1" in s


# ---------------- safe_title 静态方法 ----------------


class TestSafeTitle:
    def test_replaces_invalid_chars(self):
        """safe_title 替换 / \\ : * ? \" < > | → _。"""
        from vla.log.transcription_log import _safe_title
        result = _safe_title('教程:Python/进阶\\核心*?"<>|')
        for char in ["/", "\\", ":", "*", "?", '"', "<", ">", "|"]:
            assert char not in result

    def test_preserves_chinese_and_alphanumeric(self):
        """safe_title 保留中文/字母/数字/空格/常用符号。"""
        from vla.log.transcription_log import _safe_title
        result = _safe_title("Python 教程 - 进阶篇")
        assert "Python" in result
        assert "教程" in result
        assert "进阶篇" in result