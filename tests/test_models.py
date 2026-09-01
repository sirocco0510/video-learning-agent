"""models.py 测试(SSOT: requirements.md 第六章 6.1)。

四个 pydantic 模型:VideoTask / SubtitleResult / QualityResult / VideoSource。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from vla.models import QualityResult, SubtitleResult, VideoSource, VideoTask


# ---------------- VideoTask ----------------


class TestVideoTask:
    def test_happy_path(self):
        """合法 URL + 必填字段都能构造,字段可访问。"""
        task = VideoTask(
            id="BV1xxx",
            title="测试视频",
            url="https://www.bilibili.com/video/BV1xxx",
            expected_duration=1800,
        )
        assert task.id == "BV1xxx"
        assert task.title == "测试视频"
        assert str(task.url) == "https://www.bilibili.com/video/BV1xxx"
        assert task.expected_duration == 1800

    def test_invalid_url_raises(self):
        """非法 URL 必须抛 ValidationError。"""
        with pytest.raises(ValidationError):
            VideoTask(
                id="BV1xxx",
                title="x",
                url="not-a-url",
                expected_duration=1800,
            )


# ---------------- SubtitleResult ----------------


class TestSubtitleResult:
    def test_happy_path(self):
        """三个 source 取值之一 + metadata dict 都能构造。"""
        result = SubtitleResult(
            text="你好世界",
            source="official",
            metadata={"lang": "zh-CN"},
        )
        assert result.text == "你好世界"
        assert result.source == "official"
        assert result.metadata == {"lang": "zh-CN"}

    @pytest.mark.parametrize("source", ["official", "plugin", "whisper"])
    def test_all_valid_sources(self, source):
        """FR-2 定义的三个 source 取值都能构造。"""
        SubtitleResult(text="t", source=source, metadata={})


# ---------------- QualityResult ----------------


class TestQualityResult:
    def test_happy_path(self):
        """passed + score + issues + suggestion + char_count 都能构造。"""
        result = QualityResult(
            passed=True,
            score=85,
            issues=[],
            suggestion="无",
            char_count=1234,
        )
        assert result.passed is True
        assert result.score == 85
        assert result.issues == []
        assert result.suggestion == "无"
        assert result.char_count == 1234


# ---------------- VideoSource ----------------


class TestVideoSource:
    def test_happy_path(self):
        """Path + mode + duration_sec 都能构造。"""
        src = VideoSource(
            path=Path("/tmp/v.mp4"),
            mode="download",
            duration_sec=1800.5,
        )
        assert src.path == Path("/tmp/v.mp4")
        assert src.mode == "download"
        assert src.duration_sec == 1800.5

    @pytest.mark.parametrize("mode", ["download", "record"])
    def test_all_valid_modes(self, mode):
        """FR-1 视频源两种 mode 都能构造。"""
        VideoSource(path=Path("/tmp/x"), mode=mode, duration_sec=1.0)
