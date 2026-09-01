"""BrowserPluginSubtitle 测试(SSOT: requirements.md FR-2.4 + implementation-plan.md Phase 3.7)。

Phase 3.7:仅保留 parse 四种格式测试;.find_subtitle / .wait_for_subtitle 已删除。
"""

import json
from pathlib import Path

import pytest

from vla.subtitle.browser_plugin import BrowserPluginSubtitle


@pytest.fixture
def plugin() -> BrowserPluginSubtitle:
    return BrowserPluginSubtitle()


# ---------------- parse 四种格式 ----------------


class TestParse:
    def test_parse_srt(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        f = tmp_path / "x.srt"
        f.write_text("1\n00:00:00,000 --> 00:00:02,000\n你好世界\n\n2\n00:00:02,000 --> 00:00:04,000\n第二行")
        assert plugin.parse(f) == "你好世界\n第二行"

    def test_parse_vtt(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        f = tmp_path / "x.vtt"
        f.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n你好\n\n00:00:02.000 --> 00:00:04.000\n世界")
        assert plugin.parse(f) == "你好\n世界"

    def test_parse_json(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        f = tmp_path / "x.json"
        f.write_text(json.dumps({"body": [{"content": "abc"}, {"content": "def"}]}, ensure_ascii=False))
        assert "abc" in plugin.parse(f)
        assert "def" in plugin.parse(f)

    def test_parse_ass(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        f = tmp_path / "x.ass"
        f.write_text(
            "[Script Info]\n"
            "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,你好{ \\an8}世界\n"
            "Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,第二行\n"
        )
        text = plugin.parse(f)
        assert "你好" in text
        assert "世界" in text
        assert "第二行" in text
        assert "{ \\an8}" not in text

    def test_unsupported_suffix_raises(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_text("...")
        with pytest.raises(ValueError):
            plugin.parse(f)