"""SubtitleStrategy 三级降级测试(SSOT: requirements.md FR-2.5/2.6/2.8 + implementation-plan.md Phase 3.5)。

覆盖:
- ① 命中 → source=api, 不调 ② ③
- ① 失败 / ② 命中 → source=browser, 不调 ③
- ① ② 失败 / ③ 命中 → source=whisper
- 全失败 → None
- 无匹配 adapter → FallbackAdapter 兜底
- 任一 fetch 抛异常 → 当作 miss 降级
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vla.models import SubtitleResult
from vla.subtitle.strategy import FallbackAdapter, SubtitleStrategy


# ---------------- Fake adapter ----------------


class FakeAdapter:
    """Mock adapter,每个 fetch 方法可独立配置返回值 / 异常。"""

    def __init__(self):
        self.api_calls = 0
        self.browser_calls = 0
        self.recording_calls = 0
        self.api_return = None
        self.browser_return = None
        self.recording_return = None
        self.api_exception: Exception | None = None
        self.browser_exception: Exception | None = None
        self.recording_exception: Exception | None = None

    @classmethod
    def match(cls, url: str) -> bool:
        return True  # 测试用,总是命中

    def fetch_api_subtitle(self, url: str):
        self.api_calls += 1
        if self.api_exception:
            raise self.api_exception
        return self.api_return

    def fetch_browser_subtitle(self, driver, url: str):
        self.browser_calls += 1
        if self.browser_exception:
            raise self.browser_exception
        return self.browser_return

    def fetch_via_recording(self, driver, url: str, duration_sec: int):
        self.recording_calls += 1
        if self.recording_exception:
            raise self.recording_exception
        return self.recording_return


class StubRegistry:
    """测试用 registry:固定返回指定 adapter。"""

    def __init__(self, adapter):
        self._adapter = adapter

    def get_for_url(self, url: str):
        return self._adapter


class EmptyRegistry:
    """get_for_url 永远返回 None(测试 FallbackAdapter 路径)。"""

    def get_for_url(self, url: str):
        return None


# ---------------- Fixtures ----------------


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def driver() -> MagicMock:
    d = MagicMock()
    # 默认 connect() 返回自身 driver(BrowserDriver 实例)
    d.connect.return_value = d
    return d


@pytest.fixture
def recorder() -> MagicMock:
    return MagicMock()


@pytest.fixture
def log() -> MagicMock:
    return MagicMock()


@pytest.fixture
def notifier() -> MagicMock:
    """Stub notifier — 默认 ask_open_browser 返回 'enabled'(让流程走到 ② retry)。

    测试可单独覆盖 return_value 测试 skip/timeout 路径。
    """
    n = MagicMock()
    n.ask_open_browser.return_value = "enabled"
    return n


@pytest.fixture
def plugin_status() -> MagicMock:
    """Stub PluginStatus — 默认 unknown/available(由具体测试覆盖 unavailable)。"""
    ps = MagicMock()
    ps.is_unavailable.return_value = False
    ps.is_known.return_value = False
    return ps


@pytest.fixture
def strategy(adapter, driver, recorder, notifier, plugin_status, log) -> SubtitleStrategy:
    return SubtitleStrategy(
        registry=StubRegistry(adapter),
        driver=driver,
        recorder=recorder,
        notifier=notifier,
        plugin_status=plugin_status,
        remind_timeout_sec=30,
        log=log,
    )


@pytest.fixture
def url() -> str:
    return "https://www.bilibili.com/video/BV1xxx"


# ---------------- ① 命中 ----------------


class TestApiHit:
    def test_returns_api_source(self, strategy, adapter, url):
        adapter.api_return = ("API字幕", {"lang": "zh-CN"})

        result = strategy.get_subtitle(url)

        assert isinstance(result, SubtitleResult)
        assert result.source == "api"
        assert result.text == "API字幕"
        assert result.metadata == {"lang": "zh-CN"}
        assert adapter.api_calls == 1

    def test_does_not_call_browser_or_recording(self, strategy, adapter, url):
        adapter.api_return = ("x", {})

        strategy.get_subtitle(url)

        assert adapter.browser_calls == 0
        assert adapter.recording_calls == 0


# ---------------- ① miss / ② 命中 ----------------


class TestBrowserHit:
    def test_api_miss_then_browser_hit(self, strategy, adapter, url):
        adapter.api_return = None
        adapter.browser_return = ("浏览器字幕", {"method": "track", "lang": "zh"})

        result = strategy.get_subtitle(url)

        assert result.source == "browser"
        assert result.text == "浏览器字幕"
        assert result.metadata["method"] == "track"
        assert adapter.recording_calls == 0


class TestApiExceptionFallsThrough:
    def test_api_raises_then_browser_hit(self, strategy, adapter, url):
        adapter.api_exception = RuntimeError("api down")
        adapter.browser_return = ("x", {"method": "initial_state"})

        result = strategy.get_subtitle(url)

        assert result.source == "browser"


# ---------------- ① ② miss / ③ 命中 ----------------


class TestRecordingHit:
    def test_api_browser_miss_then_recording_hit(
        self, strategy, adapter, url
    ):
        adapter.api_return = None
        adapter.browser_return = None
        adapter.recording_return = ("whisper字幕", {"method": "recording"})

        result = strategy.get_subtitle(url)

        assert result.source == "whisper"
        assert result.text == "whisper字幕"

    def test_api_hit_browser_exception_then_recording_hit(
        self, strategy, adapter, url
    ):
        """异常也应降级到下一级。"""
        adapter.api_return = None
        adapter.browser_exception = RuntimeError("browser down")
        adapter.recording_return = ("y", {})

        result = strategy.get_subtitle(url)

        assert result.source == "whisper"


# ---------------- 全失败 ----------------


class TestAllMiss:
    def test_returns_none_when_all_miss(self, strategy, adapter, url):
        adapter.api_return = None
        adapter.browser_return = None
        adapter.recording_return = None

        result = strategy.get_subtitle(url)

        assert result is None

    def test_returns_none_when_recording_exception(self, strategy, adapter, url):
        adapter.recording_exception = RuntimeError("whisper failed")

        result = strategy.get_subtitle(url)

        assert result is None


# ---------------- 无匹配 adapter → FallbackAdapter ----------------


class TestFallbackAdapter:
    @pytest.fixture
    def fb_strategy(self, driver, recorder, notifier, plugin_status, log) -> SubtitleStrategy:
        return SubtitleStrategy(
            registry=EmptyRegistry(),
            driver=driver,
            recorder=recorder,
            notifier=notifier,
            plugin_status=plugin_status,
            remind_timeout_sec=30,
            log=log,
        )

    def test_uses_fallback_when_no_adapter(self, fb_strategy, driver, recorder):
        """无匹配 adapter → FallbackAdapter(直接用 driver/recorder)。"""
        fb_strategy.get_subtitle("https://unknown.example.com/v/1")
        # driver.new_background_page 应该被调用过(② ③)
        assert driver.new_background_page.called

    def test_fallback_api_always_miss(self, fb_strategy, driver, recorder, url):
        """FallbackAdapter.fetch_api_subtitle 总是 None → 降级到 ②。"""
        result = fb_strategy.get_subtitle(url)
        # FallbackAdapter ② ③ 都 miss → None
        assert result is None
        assert driver.new_background_page.call_count >= 1

    def test_fallback_browser_hit(self, fb_strategy, driver, url):
        """FallbackAdapter 通过 BrowserDriver.fetch_subtitle_via_browser 拿字幕。"""
        driver.fetch_subtitle_via_browser.return_value = (
            "fallback字幕",
            {"method": "dom_selector"},
        )

        result = fb_strategy.get_subtitle(url)

        assert result is not None
        assert result.source == "browser"
        assert result.text == "fallback字幕"
        assert result.metadata["platform"] == "fallback"


# ---------------- FallbackAdapter 单元测试 ----------------


class TestFallbackAdapterUnit:
    def test_api_subtitle_returns_none(self):
        fb = FallbackAdapter(driver=MagicMock(), recorder=MagicMock())
        assert fb.fetch_api_subtitle("any url") is None

    def test_browser_subtitle_returns_none_on_miss(self):
        driver = MagicMock()
        page = MagicMock()
        driver.new_background_page.return_value = page
        driver.fetch_subtitle_via_browser.return_value = (None, None)

        fb = FallbackAdapter(driver=driver, recorder=MagicMock())
        result = fb.fetch_browser_subtitle(driver, "url")
        assert result is None

    def test_browser_subtitle_returns_text_and_meta(self):
        driver = MagicMock()
        page = MagicMock()
        driver.new_background_page.return_value = page
        driver.fetch_subtitle_via_browser.return_value = (
            "x",
            {"method": "track", "lang": "zh"},
        )

        fb = FallbackAdapter(driver=driver, recorder=MagicMock())
        text, meta = fb.fetch_browser_subtitle(driver, "url")
        assert text == "x"
        assert meta["platform"] == "fallback"
        assert meta["method"] == "track"

    def test_recording_returns_none_without_recorder(self):
        driver = MagicMock()
        fb = FallbackAdapter(driver=driver, recorder=None)
        result = fb.fetch_via_recording(driver, "url", 30)
        assert result is None


def _write_transcript(tmp_path: Path, name: str, text: str) -> Path:
    """造一个 transcript 文件,返回路径(recorder 新规:返回 Path 不返回 text)。"""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestFallbackRecorderReturnsPath:
    """recorder 新规:返回 transcript 文件路径,不是 text。
    兜底要读文件 + metadata 暴露 transcript_path。"""

    def test_recording_uses_recorder(self, tmp_path):
        driver = MagicMock()
        recorder = MagicMock()
        path = _write_transcript(tmp_path, "transcript.txt", "whisper 转写文本")
        recorder.record_and_transcribe.return_value = path
        page = MagicMock()
        driver.new_background_page.return_value = page

        fb = FallbackAdapter(driver=driver, recorder=recorder)
        text, meta = fb.fetch_via_recording(driver, "url", 30)

        # 读文件读到 text
        assert text == "whisper 转写文本"
        assert meta["method"] == "recording"
        # metadata 暴露 transcript_path 给下游
        assert meta["transcript_path"] == str(path)
        recorder.record_and_transcribe.assert_called_once()

    def test_recording_reuses_video_page_when_set(self, tmp_path):
        """set_video_page 注入 page 后,③ 不开新页、不 goto,直接复用。"""
        driver = MagicMock()
        recorder = MagicMock()
        path = _write_transcript(tmp_path, "transcript.txt", "whisper")
        recorder.record_and_transcribe.return_value = path
        existing_page = MagicMock(name="existing_bilibili_page")

        fb = FallbackAdapter(driver=driver, recorder=recorder)
        fb.set_video_page(existing_page)

        text, meta = fb.fetch_via_recording(driver, "https://x", 60)

        assert text == "whisper"
        # 关键断言:没开新页、没 goto
        driver.new_background_page.assert_not_called()
        existing_page.goto.assert_not_called()
        # recorder 拿到的是已加载 B站 的 existing_page
        recorder.record_and_transcribe.assert_called_once()
        call_args = recorder.record_and_transcribe.call_args
        assert call_args.args[0] is existing_page

    def test_recording_falls_back_to_goto_when_no_video_page(self, tmp_path):
        """_video_page=None → 新开 page + goto URL(避免录到空白页静音)。"""
        driver = MagicMock()
        recorder = MagicMock()
        path = _write_transcript(tmp_path, "transcript.txt", "whisper")
        recorder.record_and_transcribe.return_value = path
        new_page = MagicMock()
        driver.new_background_page.return_value = new_page

        fb = FallbackAdapter(driver=driver, recorder=recorder)
        text, meta = fb.fetch_via_recording(driver, "https://x/video", 60)

        assert text == "whisper"
        driver.new_background_page.assert_called_once()
        new_page.goto.assert_called_once()
        # goto URL 是 B站
        assert "video" in new_page.goto.call_args.args[0]
        # Fix 3:跳转后立即暂停视频
        # pause_page_video 通过 evaluate 调,新 page 应收到 evaluate 调用
        assert new_page.evaluate.called

    def test_recording_continues_when_goto_fails(self, tmp_path):
        """goto 失败(超时 / 网络)→ 仍跑录屏,只是可能录到静音。"""
        driver = MagicMock()
        recorder = MagicMock()
        path = _write_transcript(tmp_path, "transcript.txt", "whisper")
        recorder.record_and_transcribe.return_value = path
        new_page = MagicMock()
        new_page.goto.side_effect = RuntimeError("net timeout")
        driver.new_background_page.return_value = new_page

        fb = FallbackAdapter(driver=driver, recorder=recorder)
        # 不抛
        text, meta = fb.fetch_via_recording(driver, "https://x", 30)

        assert text == "whisper"
        recorder.record_and_transcribe.assert_called_once()

    def test_recording_returns_none_when_transcript_file_missing(self, tmp_path):
        """recorder 返回的 transcript 路径不存在 / 读失败 → 返回 None(不抛)。"""
        driver = MagicMock()
        recorder = MagicMock()
        recorder.record_and_transcribe.return_value = tmp_path / "nope.txt"
        page = MagicMock()
        driver.new_background_page.return_value = page

        fb = FallbackAdapter(driver=driver, recorder=recorder)
        result = fb.fetch_via_recording(driver, "https://x", 30)
        assert result is None


# ---------------- duration_sec 传递给 ③ ----------------


class TestDurationSecPassed:
    def test_duration_passed_to_recording(self, strategy, adapter, url):
        adapter.api_return = None
        adapter.browser_return = None
        adapter.recording_return = ("x", {})
        recorded: list[int] = []
        original = adapter.fetch_via_recording

        def spy(driver, u, d):
            recorded.append(d)
            return original(driver, u, d)

        adapter.fetch_via_recording = spy  # type: ignore[method-assign]

        strategy.get_subtitle(url, duration_sec=120)

        assert recorded == [120]