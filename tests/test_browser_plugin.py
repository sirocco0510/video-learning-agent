"""BrowserPluginSubtitle 测试(策略 ②,SSOT: requirements.md FR-2.2 + implementation-plan.md Phase 3)。

find_subtitle / wait_for_subtitle / parse 四种格式(.srt/.vtt/.json/.ass)。
"""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from vla.config import VLAConfig
from vla.subtitle.browser_plugin import BrowserPluginSubtitle


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    """构造一个 VLAConfig,plugin_paths 指向 tmp_path。"""
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
        "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
        "video_source": {
            "prefer_download": True,
            "download": {"format": "worst"},
            "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"},
        },
        "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
        "browser_plugin": {
            "name": "VideoTrans",
            "enabled": True,
            "remind_timeout_sec": 30,
            "plugin_paths": [str(tmp_path / "vt"), str(tmp_path / "dl")],
        },
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./notes/v.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


@pytest.fixture
def plugin(cfg: VLAConfig, tmp_path: Path) -> BrowserPluginSubtitle:
    (tmp_path / "vt").mkdir()
    (tmp_path / "dl").mkdir()
    return BrowserPluginSubtitle(cfg)


# ---------------- find_subtitle ----------------


class TestFindSubtitle:
    def test_exact_srt_match(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        (tmp_path / "vt" / "测试视频_BV1xxx.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n你好")
        result = plugin.find_subtitle("BV1xxx", "测试视频")
        assert result is not None
        assert result.name == "测试视频_BV1xxx.srt"

    def test_fuzzy_match_when_no_exact(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        """没精确 → 模糊 *{bvid}*。"""
        (tmp_path / "vt" / "随便起的名字_BV1xxx_some.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n好")
        result = plugin.find_subtitle("BV1xxx", "不匹配的名字")
        assert result is not None
        assert "BV1xxx" in result.name
        assert result.suffix == ".vtt"

    def test_returns_none_when_not_found(self, plugin: BrowserPluginSubtitle):
        assert plugin.find_subtitle("BV1xxx", "随便") is None

    def test_iterates_paths_in_order(self, cfg: VLAConfig, tmp_path: Path):
        """plugin_paths 按顺序扫描;先命中先返回。"""
        (tmp_path / "vt").mkdir()
        (tmp_path / "dl").mkdir()
        # 第一个目录和第二个目录都有
        (tmp_path / "vt" / "video_BV1zzz.srt").write_text("x")
        (tmp_path / "dl" / "video_BV1xxx.srt").write_text("y")
        plugin = BrowserPluginSubtitle(cfg)
        result = plugin.find_subtitle("BV1xxx", "video")
        assert result is not None
        # BV1xxx 只在 dl 里,所以返回 dl
        assert "dl" in str(result)

    def test_skips_nonexistent_path(self, cfg: VLAConfig, tmp_path: Path):
        """plugin_paths 中路径不存在应跳过,不抛。"""
        cfg_paths = tmp_path / "cfg"
        cfg_paths.mkdir()
        cfg_path = cfg_paths / "vla.yaml"
        cfg_path.write_text(f"""
storage: {{tmp_dir: "./tmp", auto_cleanup_on_pass: true}}
whisper: {{model: "small", language: "zh", segment_seconds: 30, compute_type: "int8"}}
video_source:
  prefer_download: true
  download: {{format: "worst"}}
  record: {{enabled: true, screen_index: 2, fps: 30, crf: 28, audio_input: "0", preset: "ultrafast"}}
quality_check: {{enabled: true, model: "x", min_score_to_pass: 70, min_char_per_second: 1.0, max_char_per_second: 15.0}}
browser_plugin:
  name: "VideoTrans"
  enabled: true
  remind_timeout_sec: 30
  plugin_paths:
    - "{tmp_path}/does_not_exist"
    - "{tmp_path}/exists"
summary: {{model: "x", target_words_min: 500, target_words_max: 800, notes_file: "./notes/v.md", cross_video_dedup: true, trigger_mode: "quota", notes_section_header: "## x"}}
quota: {{summary_threshold_sec: 21600, on_exhausted: "stop_session"}}
history: {{file: "./logs/h.jsonl"}}
logging: {{log_dir: "./logs", notify_on_fail: false, log_alert_threshold: 50, log_alert_enabled: true}}
llm_client: {{provider: "openai", api_key_env: "OPENAI_API_KEY", base_url_env: "OPENAI_BASE_URL"}}
""")
        from vla.config import VLAConfig as VC
        c = VC.from_yaml(cfg_path)
        (tmp_path / "exists").mkdir()
        (tmp_path / "exists" / "vid_BV1a.srt").write_text("x")
        p = BrowserPluginSubtitle(c)
        result = p.find_subtitle("BV1a", "vid")
        assert result is not None


# ---------------- wait_for_subtitle ----------------


class TestWaitForSubtitle:
    def test_returns_path_when_file_appears(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        """第一次 poll 没找到,第二次 poll 文件出现 → 返回。"""
        call_count = {"n": 0}

        def fake_find(bvid, title):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                # 创建文件
                target = tmp_path / "vt" / f"vid_{bvid}.srt"
                target.write_text("x")
                return target
            return None

        with patch.object(plugin, "find_subtitle", side_effect=fake_find), \
             patch("vla.subtitle.browser_plugin.time.sleep"):
            result = plugin.wait_for_subtitle("BV1xxx", "vid", timeout=60)
        assert result is not None
        assert "BV1xxx" in result.name

    def test_returns_none_on_timeout(self, plugin: BrowserPluginSubtitle):
        """文件始终不出现,timeout 内一直返回 None。"""
        with patch.object(plugin, "find_subtitle", return_value=None), \
             patch("vla.subtitle.browser_plugin.time.sleep"), \
             patch("vla.subtitle.browser_plugin.time.time", side_effect=[0, 0, 1000, 1000]):
            result = plugin.wait_for_subtitle("BV1xxx", "vid", timeout=600)
        assert result is None


# ---------------- parse ----------------


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
        # 简单实现:递归收集所有 string
        assert "abc" in plugin.parse(f)
        assert "def" in plugin.parse(f)

    def test_parse_ass(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        # ASS Dialogue 格式:9 个逗号 + Text
        f = tmp_path / "x.ass"
        f.write_text(
            "[Script Info]\n"
            "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,你好{ \\an8}世界\n"
            "Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,第二行\n"
        )
        text = plugin.parse(f)
        # 应该有 2 行,过滤 {\an8} 之类的 override code
        assert "你好" in text
        assert "世界" in text
        assert "第二行" in text
        # {\an8} 应该被剥离
        assert "{ \\an8}" not in text

    def test_unsupported_suffix_raises(self, plugin: BrowserPluginSubtitle, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_text("...")
        with pytest.raises(ValueError):
            plugin.parse(f)
