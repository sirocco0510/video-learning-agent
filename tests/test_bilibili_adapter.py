"""BilibiliAdapter 测试(SSOT: requirements.md FR-2.0/2.1/2.17 + implementation-plan.md Phase 3.3)。

BilibiliAdapter 实现 PlatformAdapter Protocol:
- match(url) 匹配 bilibili.com / b23.tv
- fetch_api_subtitle(url) → 委托 BilibiliOfficialSubtitle.get_subtitle()
- fetch_browser_subtitle(driver, url) → 用 BrowserDriver 4 种 JS 探测
- fetch_via_recording(driver, url, duration_sec) → 用 BrowserRecorder
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vla.subtitle.bilibili_adapter import BilibiliAdapter
from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
from vla.subtitle.browser_record import BrowserRecorder
from vla.subtitle.browser_driver import BrowserDriver


# ---------------- Fixtures ----------------


@pytest.fixture
def official() -> MagicMock:
    return MagicMock(spec=BilibiliOfficialSubtitle)


@pytest.fixture
def recorder() -> MagicMock:
    return MagicMock(spec=BrowserRecorder)


@pytest.fixture
def driver() -> MagicMock:
    return MagicMock(spec=BrowserDriver)


@pytest.fixture
def adapter(official, recorder) -> BilibiliAdapter:
    return BilibiliAdapter(official=official, recorder=recorder)


# ---------------- match() ----------------


class TestMatch:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.bilibili.com/video/BV1dzui69EYV",
            "https://bilibili.com/video/BV1xxx",
            "https://www.bilibili.com/bangumi/play/ep123",
            "https://b23.tv/abc123",
        ],
    )
    def test_matches_bilibili_urls(self, url: str):
        assert BilibiliAdapter.match(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=xxx",
            "https://vimeo.com/123456",
            "https://internal.example.com/v/1",
        ],
    )
    def test_does_not_match_other_platforms(self, url: str):
        assert BilibiliAdapter.match(url) is False


# ---------------- fetch_api_subtitle() ----------------


class TestFetchApi:
    def test_delegates_to_official_subtitle(self, adapter: BilibiliAdapter, official: MagicMock):
        official.get_subtitle.return_value = ("官方字幕", {"language": "zh-CN"})

        text, meta = adapter.fetch_api_subtitle("https://www.bilibili.com/video/BV1xxx")

        assert text == "官方字幕"
        assert meta == {"language": "zh-CN"}
        official.get_subtitle.assert_called_once_with("https://www.bilibili.com/video/BV1xxx")

    def test_returns_none_when_official_returns_none(self, adapter: BilibiliAdapter, official: MagicMock):
        official.get_subtitle.return_value = None

        result = adapter.fetch_api_subtitle("https://www.bilibili.com/video/BV1xxx")

        assert result is None

    def test_works_without_recorder(self, official: MagicMock):
        """recorder 是可选的,纯 API 调用不需要。"""
        official.get_subtitle.return_value = ("a", {})
        adapter = BilibiliAdapter(official=official)  # no recorder

        text, meta = adapter.fetch_api_subtitle("https://www.bilibili.com/video/BV1xxx")

        assert text == "a"


# ---------------- fetch_browser_subtitle() ----------------


class TestFetchBrowser:
    def test_creates_page_and_calls_browser_driver(self, adapter, driver: MagicMock):
        driver.fetch_subtitle_via_browser.return_value = ("browser字幕", {"method": "track"})

        text, meta = adapter.fetch_browser_subtitle(driver, "https://www.bilibili.com/video/BV1xxx")

        assert text == "browser字幕"
        assert meta["method"] == "track"
        assert meta["platform"] == "bilibili"
        driver.new_background_page.assert_called_once()

    def test_returns_none_when_browser_miss(self, adapter, driver: MagicMock):
        driver.fetch_subtitle_via_browser.return_value = (None, None)

        result = adapter.fetch_browser_subtitle(driver, "https://www.bilibili.com/video/BV1xxx")

        assert result is None

    def test_preserves_method_in_meta(self, adapter, driver: MagicMock):
        driver.fetch_subtitle_via_browser.return_value = ("x", {"method": "initial_state", "lang": "zh"})

        _, meta = adapter.fetch_browser_subtitle(driver, "url")

        assert meta["method"] == "initial_state"
        assert meta["lang"] == "zh"
        assert meta["platform"] == "bilibili"


# ---------------- fetch_via_recording() ----------------


class TestFetchRecording:
    def test_returns_none_when_no_recorder(self, official: MagicMock, driver: MagicMock):
        adapter = BilibiliAdapter(official=official, recorder=None)

        result = adapter.fetch_via_recording(driver, "url", 30)

        assert result is None
        driver.new_background_page.assert_not_called()

    def test_uses_recorder_to_record_and_transcribe(self, adapter, recorder: MagicMock, driver: MagicMock):
        recorder.record_and_transcribe.return_value = "录屏字幕"

        text, meta = adapter.fetch_via_recording(driver, "https://www.bilibili.com/video/BV1xxx", 30)

        assert text == "录屏字幕"
        assert meta["method"] == "recording"
        assert meta["platform"] == "bilibili"
        recorder.record_and_transcribe.assert_called_once()
        driver.new_background_page.assert_called_once()

    def test_passes_duration_to_recorder(self, adapter, recorder: MagicMock, driver: MagicMock):
        recorder.record_and_transcribe.return_value = "x"

        adapter.fetch_via_recording(driver, "url", 45)

        # duration_sec=45 应传给 recorder
        args, kwargs = recorder.record_and_transcribe.call_args
        # 位置参数: page, url, duration_sec, save_dir
        assert args[2] == 45
        assert "url" in args[1]

    def test_save_dir_under_storage_tmp(self, adapter, recorder: MagicMock, driver: MagicMock):
        """save_dir 默认在 config.storage.tmp_dir/recordings 下。"""
        recorder.record_and_transcribe.return_value = "x"

        adapter.fetch_via_recording(driver, "url", 10)

        args, _ = recorder.record_and_transcribe.call_args
        save_dir: Path = args[3]
        assert save_dir.name == "recordings"