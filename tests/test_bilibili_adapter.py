"""BilibiliAdapter 测试(SSOT: requirements.md FR-2.0/2.1/2.17 + implementation-plan.md Phase 3.3 + F2-7)。

BilibiliAdapter 继承 PlatformAdapter:
- match(url) 匹配 bilibili.com / b23.tv
- fetch_api_subtitle(url) → 委托 BilibiliOfficialSubtitle.get_subtitle()
- fetch_browser_subtitle(driver, url) → 用 BrowserDriver 4 种 JS 探测
- fetch_via_recording(driver, url, duration_sec, **kwargs) → 转发到 PlatformAdapter
  默认实现(FR-2.14 v3:path ① yt-dlp → path ② Tab Audio Recorder,Q7 Silent fallback)
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from vla.subtitle.bilibili_adapter import BilibiliAdapter
from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
from vla.subtitle.browser_driver import BrowserDriver
from vla.subtitle.platform_adapter import PlatformAdapter


# ---------------- Fixtures ----------------


@pytest.fixture
def official() -> MagicMock:
    return MagicMock(spec=BilibiliOfficialSubtitle)


@pytest.fixture
def driver() -> MagicMock:
    return MagicMock(spec=BrowserDriver)


# F2-7:4 个 REQUIRED deps(audio_factory / tab_recorder / transcriber / screenshot_controller)
@pytest.fixture
def audio_factory() -> MagicMock:
    af = MagicMock()
    af.is_downloadable = MagicMock(return_value=False)  # 默认 path ① miss
    af.extract = MagicMock(return_value=MagicMock(
        audio_path=Path("/tmp/audio.wav"),
        source="yt-dlp",
        duration_sec=300,
    ))
    return af


@pytest.fixture
def tab_recorder() -> MagicMock:
    tr = MagicMock()
    tr.probe_status = AsyncMock(return_value="disabled")
    tr.start_recording = AsyncMock(return_value="audio_id_x")
    tr.click_download = AsyncMock(return_value=Path("/tmp/audio_id_x.webm"))
    return tr


@pytest.fixture
def transcriber() -> MagicMock:
    tx = MagicMock()
    tx.transcribe = MagicMock(return_value="unused")
    tx.cleanup = MagicMock()
    return tx


@pytest.fixture
def screenshot_controller() -> MagicMock:
    sc = MagicMock()
    sc.phase_a_start = AsyncMock(return_value=0.0)
    sc.phase_b_then_c = AsyncMock(return_value=0.0)
    sc.phase_d_write_index = MagicMock()
    return sc


@pytest.fixture
def adapter(
    official,
    audio_factory,
    tab_recorder,
    transcriber,
    screenshot_controller,
) -> BilibiliAdapter:
    return BilibiliAdapter(
        official=official,
        audio_factory=audio_factory,
        tab_recorder=tab_recorder,
        transcriber=transcriber,
        screenshot_controller=screenshot_controller,
    )


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


# ---------------- Inheritance ----------------


class TestInheritance:
    def test_is_platform_adapter_subclass(self):
        """F2-7:BilibiliAdapter 继承 PlatformAdapter(不再是 duck typing)。"""
        assert issubclass(BilibiliAdapter, PlatformAdapter)

    def test_instance_is_platform_adapter(self, adapter: BilibiliAdapter):
        assert isinstance(adapter, PlatformAdapter)


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


# ---------------- fetch_via_recording() — F2-7:转发到 base impl ----------------


class TestFetchViaRecording:
    """F2-7:BilibiliAdapter.fetch_via_recording 转发到 PlatformAdapter base impl。

    验证:
    - 不再依赖旧 Screen Recorder 模块(F2-8 后已删;本 adapter F2-7 已切到 4 deps 架构)
    - 转发 self 持有的 4 deps 到 super().fetch_via_recording
    - 调用方可覆盖 kwargs(setdefault 语义)
    - B站 _make_stem override 用 bvid
    """

    def test_returns_none_when_path12_both_fail(
        self,
        adapter,
        audio_factory: MagicMock,
        tab_recorder: MagicMock,
        transcriber: MagicMock,
    ):
        """默认 fixture:audio_factory.is_downloadable=False,tab_recorder probe=disabled
        → path ① miss,path ② miss → return None。
        """
        result = adapter.fetch_via_recording(MagicMock(), "https://x.com/v/1", 30)
        assert result is None
        # 验证确实调到 base impl 的两条路径
        audio_factory.is_downloadable.assert_called_once()
        tab_recorder.probe_status.assert_awaited_once()

    def test_path_one_hit_returns_yt_dlp_meta(
        self,
        adapter,
        audio_factory: MagicMock,
        transcriber: MagicMock,
    ):
        """path ① 命中 → return (text, {"via": "yt-dlp", "platform": "bilibili"})。

        注意:meta 不含 "platform"=bilibili,因为 base impl 不加 platform 标记
        (策略 ② 才加);本测试只验证 base impl 转发行为。
        """
        audio_factory.is_downloadable.return_value = True
        audio_factory.extract.return_value = MagicMock(
            audio_path=Path("/tmp/bvid.wav"),
            source="yt-dlp",
            duration_sec=300,
        )
        transcriber.transcribe.return_value = "yt-dlp text"

        text, meta = adapter.fetch_via_recording(MagicMock(), "https://www.bilibili.com/video/BV1xx", 300)

        assert text == "yt-dlp text"
        assert meta["via"] == "yt-dlp"
        # cleanup 调了(audio_path 已用完)
        transcriber.cleanup.assert_called_once()

    def test_forwards_4_deps_to_base_impl(
        self,
        adapter,
        audio_factory: MagicMock,
        tab_recorder: MagicMock,
        transcriber: MagicMock,
        screenshot_controller: MagicMock,
    ):
        """super().fetch_via_recording 应收到 self 持有的 4 deps。

        path ② 命中时 screenshot_controller 被调到 phase_*(screenshot_controller
        fixture 默认 path ② 命中后调)。
        """
        # 触发 path ② 命中(让 screenshot_controller 被调)
        audio_factory.is_downloadable.return_value = False
        tab_recorder.probe_status = AsyncMock(return_value="enabled")
        transcriber.transcribe.return_value = "tab text"

        adapter.fetch_via_recording(MagicMock(), "https://x.com/v/2", 100)

        # path ② 命中 → screenshot_controller.phase_a_start 应被调
        screenshot_controller.phase_a_start.assert_awaited_once()
        screenshot_controller.phase_b_then_c.assert_awaited_once()
        screenshot_controller.phase_d_write_index.assert_called_once()

    def test_caller_can_override_kwarg(
        self,
        adapter,
        transcriber: MagicMock,
    ):
        """kwargs.setdefault 语义:调用方传 audio_factory 覆盖 self 持有的。

        路径 ① 命中时,caller 传的 audio_factory 应被用(self 持有的不被用)。
        """
        caller_af = MagicMock()
        caller_af.is_downloadable = MagicMock(return_value=True)
        caller_af.extract = MagicMock(return_value=MagicMock(
            audio_path=Path("/tmp/caller.wav"),
            source="yt-dlp",
            duration_sec=10,
        ))

        adapter.fetch_via_recording(
            MagicMock(), "https://x.com/v/3", 10,
            audio_factory=caller_af,
        )

        caller_af.is_downloadable.assert_called_once()
        caller_af.extract.assert_called_once()

    def test_bvid_stem_override(self, adapter, audio_factory: MagicMock, transcriber: MagicMock):
        """_make_stem override 用 bvid(避免 url hash 抖动)。

        路径 ① 命中时 extract 的 stem 应该是 bvid。
        """
        audio_factory.is_downloadable.return_value = True
        audio_factory.extract.return_value = MagicMock(
            audio_path=Path("/tmp/stem.wav"),
            source="yt-dlp",
            duration_sec=10,
        )

        adapter.fetch_via_recording(MagicMock(), "https://www.bilibili.com/video/BV1abc123?p=1", 10)

        # extract 的 stem 应该是 bvid="BV1abc123"
        args, kwargs = audio_factory.extract.call_args
        # stem 通过位置或 kwargs 传(看 base impl)
        stem = args[1] if len(args) > 1 else kwargs.get("stem")
        assert stem == "BV1abc123"
