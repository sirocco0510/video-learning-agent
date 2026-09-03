"""字幕归一化测试(SSOT: spec §E Sub-1/Sub-2,2026-09-03)。

测试覆盖:
- SRT/VTT/ASS 三种格式的基础解析
- 自动格式检测(扩展名 + 内容启发式)
- Segment 模型验证(start/end 顺序)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vla.subtitle.normalize import FormatHint, Segment, parse_subtitle


# ---------------- Fixtures ----------------


SRT_FIXTURE = """1
00:00:01,000 --> 00:00:04,000
Hello world.

2
00:00:05,000 --> 00:00:08,000
第二行
中文测试
"""


VTT_FIXTURE = """WEBVTT

00:00:01.000 --> 00:00:04.000
Hello world.

00:00:05.000 --> 00:00:08.000
第二行
"""


ASS_FIXTURE = """[Script Info]
ScriptType: v4.00+

[Events]
Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,Hello world.
Dialogue: 0,0:00:05.00,0:00:08.00,Default,,0,0,0,,第二行
"""


# ---------------- SRT ----------------


class TestSrtParse:
    def test_basic_srt(self, tmp_path: Path):
        p = tmp_path / "x.srt"
        p.write_text(SRT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p, format_hint="srt")
        assert len(segs) == 2
        assert segs[0].start == 1.0
        assert segs[0].end == 4.0
        assert segs[0].text == "Hello world."

    def test_multiline_text_concatenated(self, tmp_path: Path):
        p = tmp_path / "x.srt"
        p.write_text(SRT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p, format_hint="srt")
        assert segs[1].text == "第二行\n中文测试"


# ---------------- VTT ----------------


class TestVttParse:
    def test_basic_vtt(self, tmp_path: Path):
        p = tmp_path / "x.vtt"
        p.write_text(VTT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p, format_hint="vtt")
        assert len(segs) == 2
        assert segs[1].text == "第二行"


# ---------------- ASS ----------------


class TestAssParse:
    def test_basic_ass(self, tmp_path: Path):
        p = tmp_path / "x.ass"
        p.write_text(ASS_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p, format_hint="ass")
        assert len(segs) == 2
        assert segs[0].text == "Hello world."


# ---------------- Format Detection ----------------


class TestFormatDetection:
    def test_auto_detect_srt_by_extension(self, tmp_path: Path):
        p = tmp_path / "x.srt"
        p.write_text(SRT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p)
        assert len(segs) == 2

    def test_auto_detect_vtt_by_header(self, tmp_path: Path):
        p = tmp_path / "no_ext.txt"
        p.write_text(VTT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p)
        assert segs[0].text == "Hello world."

    def test_unknown_format_raises(self, tmp_path: Path):
        p = tmp_path / "x.xyz"
        p.write_text("garbage", encoding="utf-8")
        with pytest.raises(ValueError, match="format"):
            parse_subtitle(p)


# ---------------- Segment Model ----------------


class TestSegmentModel:
    def test_segment_validation(self):
        s = Segment(start=1.0, end=2.0, text="hi")
        assert s.start == 1.0
        with pytest.raises(ValueError):
            Segment(start=3.0, end=2.0, text="hi")  # end < start


# ---------------- FormatHint enum sanity ----------------


class TestFormatHintEnum:
    def test_values(self):
        assert FormatHint.SRT.value == "srt"
        assert FormatHint.VTT.value == "vtt"
        assert FormatHint.ASS.value == "ass"
        assert FormatHint.AUTO.value == "auto"
        assert FormatHint.JSON.value == "json"
