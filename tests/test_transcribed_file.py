"""transcribed_file.write/read 测试(SSOT: requirements.md FR-7.7 + R-06)。

设计:
- TranscribedItem dataclass 是字幕原文的 SSOT(标题/来源/质量/时长/正文)
- write() 按统一格式落盘(header + 正文),read() 完整往返
- read() 对缺失字段用默认值兜底(向后兼容旧文件)
"""

from pathlib import Path

from vla.log.transcribed_file import TranscribedItem, write, read


class TestRoundTrip:
    def test_basic_roundtrip(self, tmp_path: Path):
        item = TranscribedItem(
            title="Python 装饰器",
            source="whisper",
            quality_score=85,
            duration_sec=1800,
            text="这是正文内容。",
            path=tmp_path / "x.txt",
            mtime=0.0,
        )
        path = write(tmp_path / "x.txt", item)
        loaded = read(path)
        assert loaded.title == "Python 装饰器"
        assert loaded.source == "whisper"
        assert loaded.quality_score == 85
        assert loaded.duration_sec == 1800
        assert loaded.text == "这是正文内容。"

    def test_unicode_in_text(self, tmp_path: Path):
        item = TranscribedItem(
            title="T", source="api", quality_score=90,
            duration_sec=120, text="深度学习\n\n神经网络原理", path=tmp_path / "t.txt", mtime=0.0,
        )
        path = write(tmp_path / "t.txt", item)
        assert "深度学习" in read(path).text
        assert "\n\n" in read(path).text  # paragraph separator preserved

    def test_zero_duration(self, tmp_path: Path):
        item = TranscribedItem(
            title="T", source="browser", quality_score=50,
            duration_sec=0, text="hi", path=tmp_path / "t.txt", mtime=0.0,
        )
        path = write(tmp_path / "t.txt", item)
        assert read(path).duration_sec == 0


class TestReadEdgeCases:
    def test_minimal_file(self, tmp_path: Path):
        """只有标题 + 正文也能读(元数据缺失时用默认值)。"""
        path = tmp_path / "min.txt"
        path.write_text("# Minimal\n\nBody text", encoding="utf-8")
        loaded = read(path)
        assert loaded.title == "Minimal"
        assert loaded.text == "Body text"
        assert loaded.source == "whisper"  # default
        assert loaded.quality_score == 0  # default
        assert loaded.duration_sec == 0  # default

    def test_no_blank_separator_falls_back_to_strip(self, tmp_path: Path):
        path = tmp_path / "weird.txt"
        path.write_text("# T\nsource:whisper\nall in one", encoding="utf-8")
        loaded = read(path)
        # 没有 \n\n 分隔,read 把整段除标题外当 text
        assert loaded.title == "T"


class TestWriteCreatesDirectory:
    def test_creates_parent_dir(self, tmp_path: Path):
        path = tmp_path / "nested" / "x.txt"
        item = TranscribedItem(
            title="T", source="api", quality_score=80,
            duration_sec=10, text="hi", path=path, mtime=0.0,
        )
        result = write(path, item)
        assert result.exists()