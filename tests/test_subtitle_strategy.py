"""SubtitleStrategy 编排器测试(SSOT: requirements.md FR-2.5/2.6/2.8/2.9/2.10/2.11)。

状态机:
  - ① 命中 → SubtitleResult(source="official")
  - ① 失败 → 看 plugin_status:
      available → ② find_subtitle / wait_for_subtitle, 命中则 SubtitleResult(source="plugin")
      unavailable → 立即降级(返回 None → 由调用方走 ③)
      unknown → 弹窗:
        "opened" → ② wait → 命中 → source="plugin";超时 → mark unavailable
        "skip"   → 返回 SKIP
        "timeout" → mark unavailable, 降级

返回值约定:
  SubtitleResult  → 策略命中(官方 / 插件)
  SkipSignal      → 用户主动跳过该视频
  None            → ① ② 都失败 → 调用方走 ③ 转写
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vla.config import VLAConfig
from vla.models import SubtitleResult
from vla.state.plugin_status import PluginStatus
from vla.subtitle.strategy import SKIP, SubtitleStrategy
from vla.ui.macos_notify import MacOSNotifier


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
        "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
        "video_source": {
            "prefer_download": True,
            "download": {"format": "worst"},
            "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"},
        },
        "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
        "browser_plugin": {"name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30, "plugin_paths": [str(tmp_path / "vt")]},
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./notes/v.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


@pytest.fixture
def strategy(cfg: VLAConfig) -> SubtitleStrategy:
    return SubtitleStrategy(
        official=MagicMock(),
        plugin=MagicMock(),
        plugin_status=PluginStatus(),
        notifier=MagicMock(spec=MacOSNotifier),
        config=cfg,
    )


# ---------------- FR-2.6 ① 命中 ----------------


class TestOfficialHit:
    def test_official_hit_returns_official_result(self, strategy: SubtitleStrategy):
        """① 命中 → SubtitleResult(source='official'),不调用 ②。"""
        strategy.official.get_subtitle.return_value = (
            "官方字幕",
            {"language": "zh-Hans", "bvid": "BV1xxx"},
        )
        result = strategy.get_subtitle("https://www.bilibili.com/video/BV1xxx")
        assert isinstance(result, SubtitleResult)
        assert result.source == "official"
        assert result.text == "官方字幕"
        strategy.plugin.find_subtitle.assert_not_called()


# ---------------- FR-2.8 ② 命中(plugin already available) ----------------


class TestPluginHit:
    def test_official_miss_then_plugin_available_hit(self, strategy: SubtitleStrategy):
        """① 返回 None + plugin_status=available → ② find_subtitle,不弹窗。"""
        strategy.official.get_subtitle.return_value = None
        strategy.plugin_status.mark_available()
        strategy.plugin.find_subtitle.return_value = Path("/tmp/vt/x_BV1xxx.srt")
        strategy.plugin.parse.return_value = "插件字幕"

        result = strategy.get_subtitle("https://www.bilibili.com/video/BV1xxx")
        assert isinstance(result, SubtitleResult)
        assert result.source == "plugin"
        assert result.text == "插件字幕"
        strategy.notifier.ask_open_browser.assert_not_called()
        strategy.plugin.wait_for_subtitle.assert_not_called()


# ---------------- FR-2.11 ② unavailable → 直接降级 ----------------


class TestPluginUnavailable:
    def test_unavailable_skips_dialog_and_falls_through(self, strategy: SubtitleStrategy):
        """plugin_status=unavailable → 不弹窗,直接返回 None(由调用方走 ③)。"""
        strategy.official.get_subtitle.return_value = None
        strategy.plugin_status.mark_unavailable("用户上轮 timeout")

        result = strategy.get_subtitle("https://www.bilibili.com/video/BV1xxx")
        assert result is None
        strategy.notifier.ask_open_browser.assert_not_called()
        strategy.plugin.find_subtitle.assert_not_called()


# ---------------- FR-2.9 ② unknown → 弹窗:三态分支 ----------------


class TestDialogWhenUnknown:
    """plugin_status=unknown 时,弹窗 ask_open_browser。"""

    def _prep_no_subtitle(self, strategy):
        strategy.official.get_subtitle.return_value = None
        # status 保持 unknown

    def test_dialog_skip_returns_skip_signal(self, strategy: SubtitleStrategy):
        """用户点"跳过该视频" → 返回 SKIP。"""
        self._prep_no_subtitle(strategy)
        strategy.notifier.ask_open_browser.return_value = "skip"

        result = strategy.get_subtitle("https://www.bilibili.com/video/BV1xxx")
        assert result is SKIP
        # status 仍然 unknown(用户没启用,也没拒绝)
        assert strategy.plugin_status.get() == "unknown"

    def test_dialog_opened_then_plugin_hits(self, strategy: SubtitleStrategy, tmp_path: Path):
        """用户点"已开启" → mark_available + ② wait → 命中。"""
        self._prep_no_subtitle(strategy)
        strategy.notifier.ask_open_browser.return_value = "opened"
        hit = tmp_path / "vt" / "x_BV1xxx.srt"
        strategy.plugin.wait_for_subtitle.return_value = hit
        strategy.plugin.parse.return_value = "插件后续命中"

        result = strategy.get_subtitle("https://www.bilibili.com/video/BV1xxx")
        assert isinstance(result, SubtitleResult)
        assert result.source == "plugin"
        assert result.text == "插件后续命中"
        assert strategy.plugin_status.get() == "available"

    def test_dialog_opened_then_plugin_misses(self, strategy: SubtitleStrategy):
        """用户点"已开启" 但插件一直没文件 → mark_unavailable + 返回 None。"""
        self._prep_no_subtitle(strategy)
        strategy.notifier.ask_open_browser.return_value = "opened"
        strategy.plugin.wait_for_subtitle.return_value = None

        result = strategy.get_subtitle("https://www.bilibili.com/video/BV1xxx")
        assert result is None
        assert strategy.plugin_status.get() == "unavailable"

    def test_dialog_timeout_marks_unavailable(self, strategy: SubtitleStrategy):
        """弹窗超时 → mark_unavailable + 返回 None(FR-2.10)。"""
        self._prep_no_subtitle(strategy)
        strategy.notifier.ask_open_browser.return_value = "timeout"

        result = strategy.get_subtitle("https://www.bilibili.com/video/BV1xxx")
        assert result is None
        assert strategy.plugin_status.get() == "unavailable"


# ---------------- ① 失败 + ② available 但 find_subtitle 也没命中 → wait 然后 None ----------------


class TestPluginAvailableMiss:
    def test_find_returns_none_then_wait_returns_none(self, strategy: SubtitleStrategy):
        """available 但 find_subtitle 没命中 → wait 也没命中 → 返回 None。"""
        strategy.official.get_subtitle.return_value = None
        strategy.plugin_status.mark_available()
        strategy.plugin.find_subtitle.return_value = None
        strategy.plugin.wait_for_subtitle.return_value = None

        result = strategy.get_subtitle("https://www.bilibili.com/video/BV1xxx")
        assert result is None
        # 既然 find 没命中,不该再弹窗
        strategy.notifier.ask_open_browser.assert_not_called()
