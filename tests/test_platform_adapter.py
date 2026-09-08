"""PlatformAdapter Protocol + Registry 测试(SSOT: requirements.md FR-2.0 + implementation-plan.md Phase 3.0)。

测试 register / get_for_url / match 域名 / 找不到返回 None / 多 adapter 注册顺序。
"""

from vla.subtitle.platform_adapter import PlatformAdapterRegistry


class FakeBilibiliAdapter:
    """测试用 adapter,只匹配 bilibili.com / b23.tv。"""

    def __init__(self):
        self.created_with = None

    @classmethod
    def match(cls, url: str) -> bool:
        return "bilibili.com" in url or "b23.tv" in url

    def fetch_api_subtitle(self, url: str):
        return None

    def fetch_browser_subtitle(self, driver, url: str):
        return None

    def fetch_via_recording(self, driver, url: str, duration_sec: int):
        return None


class FakeInternalAdapter:
    """测试用 adapter,只匹配 internal.example.com。"""

    @classmethod
    def match(cls, url: str) -> bool:
        return "internal.example.com" in url

    def fetch_api_subtitle(self, url: str):
        return None

    def fetch_browser_subtitle(self, driver, url: str):
        return None

    def fetch_via_recording(self, driver, url: str, duration_sec: int):
        return None


class TestRegister:
    def test_register_adds_adapter_class(self):
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        assert FakeBilibiliAdapter in reg.list_adapters()

    def test_register_dedups_same_class(self):
        """重复注册同一类只算一次。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        reg.register(FakeBilibiliAdapter)
        assert len(reg.list_adapters()) == 1


class TestGetForUrl:
    def test_returns_instance_when_match(self):
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        adapter = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        assert isinstance(adapter, FakeBilibiliAdapter)

    def test_returns_none_when_no_match(self):
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        adapter = reg.get_for_url("https://www.youtube.com/watch?v=xxx")
        assert adapter is None

    def test_matches_b23_short_url(self):
        """b23.tv 短链也要能匹配。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        adapter = reg.get_for_url("https://b23.tv/BV1xxx")
        assert isinstance(adapter, FakeBilibiliAdapter)

    def test_multiple_adapters_first_match_wins(self):
        """多个 adapter 时,第一个匹配返回。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        reg.register(FakeInternalAdapter)
        # bilibili URL → Bilibili adapter
        a1 = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        # internal URL → Internal adapter
        a2 = reg.get_for_url("https://internal.example.com/v/123")
        assert isinstance(a1, FakeBilibiliAdapter)
        assert isinstance(a2, FakeInternalAdapter)

    def test_empty_registry_returns_none(self):
        reg = PlatformAdapterRegistry()
        assert reg.get_for_url("https://www.bilibili.com/video/BV1xxx") is None

    def test_get_for_url_instantiates_fresh_each_call(self):
        """每次 get_for_url 返回新实例(状态隔离)。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        a1 = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        a2 = reg.get_for_url("https://www.bilibili.com/video/BV1yyy")
        assert a1 is not a2  # 不同实例


class TestListAdapters:
    def test_empty_initially(self):
        reg = PlatformAdapterRegistry()
        assert reg.list_adapters() == []

    def test_returns_registered_in_order(self):
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        reg.register(FakeInternalAdapter)
        assert reg.list_adapters() == [FakeBilibiliAdapter, FakeInternalAdapter]


class TestRegisterInstance:
    """2026-09-02 新增:支持 pre-built adapter 实例注册(带 deps)。"""

    def test_register_instance_returns_same_object(self):
        """带 deps 的 adapter(构造需要参数)用 register_instance 注册,
        get_for_url 应返回同一实例(共享 deps),不是新实例。"""
        reg = PlatformAdapterRegistry()
        instance = FakeBilibiliAdapter()
        instance.deps = {"injected": True}  # 模拟 deps
        reg.register_instance(instance)

        result = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        assert result is instance
        assert result.deps == {"injected": True}

    def test_instance_takes_priority_over_class(self):
        """实例先匹配;类 fallback。"""
        reg = PlatformAdapterRegistry()
        instance = FakeBilibiliAdapter()
        instance.tag = "instance"
        reg.register_instance(instance)
        reg.register(FakeBilibiliAdapter)  # class also registered

        result = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        assert result is instance  # 实例优先,不是新 class 实例
        assert getattr(result, "tag", None) == "instance"

    def test_list_adapters_excludes_instances(self):
        """list_adapters() 只返回 class,instance 通过 list_instances() 看。
        保持 legacy 测试不破。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        reg.register_instance(FakeBilibiliAdapter())

        assert reg.list_adapters() == [FakeBilibiliAdapter]
        assert len(reg.list_instances()) == 1

    def test_instance_only_no_class_registered(self):
        """只注册实例时,get_for_url 仍能匹配。"""
        reg = PlatformAdapterRegistry()
        instance = FakeInternalAdapter()  # 不通过 register() 注册
        # 显式 instance 注册
        reg._instances.append(instance)

        result = reg.get_for_url("https://internal.example.com/v/123")
        assert result is instance


# ---------------------------------------------------------------------------
# F2-10: PlatformAdapter.fetch_via_recording 默认实现只剩 path ① yt-dlp
# SSOT: docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md §4.1 + §4.5
#
# F2-10 (2026-09-08): path ② Tab Audio Recorder 已删(迁到 strategy._try_browser
# 弹窗 enabled 分支),screenshot_controller 也删(改由 main.py 直接触发)。
# 本测试只验证 path ① yt-dlp 的命中 / 失败行为。
# ---------------------------------------------------------------------------
from pathlib import Path
from unittest.mock import MagicMock

from vla.audio.source_factory import AudioExtractionResult
from vla.subtitle.platform_adapter import PlatformAdapter


def _make_audio_factory(*, downloadable: bool, raise_extract: bool = False) -> MagicMock:
    f = MagicMock()
    f.is_downloadable = MagicMock(return_value=downloadable)
    if raise_extract:
        f.extract = MagicMock(side_effect=Exception("yt-dlp 404"))
    else:
        f.extract = MagicMock(
            return_value=AudioExtractionResult(
                audio_path=Path("/tmp/audio.wav"),
                source="yt-dlp",
                duration_sec=300,
            )
        )
    return f


def _make_transcriber(text: str = "transcribed text") -> MagicMock:
    t = MagicMock()
    t.transcribe = MagicMock(return_value=text)
    t.cleanup = MagicMock()
    return t


class TestPathOneHit:
    """F2-10:PlatformAdapter.fetch_via_recording 只剩 path ① yt-dlp。

    - is_downloadable=True → extract → transcribe → cleanup → return (text, {"via": "yt-dlp", ...})
    - is_downloadable=True 但 extract 抛 → 静默 return None(Q7 Silent)
    - is_downloadable=False → return None(直接走人,没有 path ② 兜底)
    """

    def test_path_one_returns_transcribed_text_when_downloadable(self) -> None:
        """is_downloadable=True → extract → transcribe → return (text, meta)。"""
        af = _make_audio_factory(downloadable=True)
        tx = _make_transcriber("yt-dlp text")
        adapter = PlatformAdapter()
        result = adapter.fetch_via_recording(
            driver=MagicMock(),
            url="https://www.bilibili.com/video/Bv1",
            duration_sec=300,
            audio_factory=af,
            transcriber=tx,
        )
        assert result is not None
        text, meta = result
        assert text == "yt-dlp text"
        assert meta["via"] == "yt-dlp"
        # cleanup 调了(audio_path 已用完)
        tx.cleanup.assert_called_once()

    def test_path_one_returns_none_when_extract_fails(self) -> None:
        """is_downloadable=True 但 extract 抛 → 静默 return None(Q7)。

        F2-10 后没有 path ② 兜底,直接返回 None。
        """
        af = _make_audio_factory(downloadable=True, raise_extract=True)
        tx = _make_transcriber("unused")
        adapter = PlatformAdapter()
        result = adapter.fetch_via_recording(
            driver=MagicMock(), url="https://x.com/v/1", duration_sec=100,
            audio_factory=af, transcriber=tx,
        )
        # Q7: 静默 — 不 raise,只是 None
        assert result is None

    def test_path_one_returns_none_when_not_downloadable(self) -> None:
        """is_downloadable=False → return None(F2-10 后没 path ② 兜底)。

        旧版会 fallback 到 tab_recorder,新版直接 None(由 strategy 层决定后续)。
        """
        af = _make_audio_factory(downloadable=False)
        tx = _make_transcriber("unused")
        result = PlatformAdapter().fetch_via_recording(
            driver=MagicMock(), url="https://x.com/v/2", duration_sec=200,
            audio_factory=af, transcriber=tx,
        )
        assert result is None
        # extract 不应被调(没 download)
        af.extract.assert_not_called()


class TestSignatureGuard:
    """F2-10:fetch_via_recording 不再接受 tab_recorder / screenshot_controller。

    传这些 kwarg 应 TypeError(防止误用)。
    """

    def test_passing_tab_recorder_kwarg_raises(self) -> None:
        af = _make_audio_factory(downloadable=True)
        tx = _make_transcriber()
        tr = MagicMock()
        with pytest_raises_type_error():
            PlatformAdapter().fetch_via_recording(
                driver=MagicMock(), url="https://x.com/v/1", duration_sec=100,
                audio_factory=af, transcriber=tx, tab_recorder=tr,
            )

    def test_passing_screenshot_controller_kwarg_raises(self) -> None:
        af = _make_audio_factory(downloadable=True)
        tx = _make_transcriber()
        sc = MagicMock()
        with pytest_raises_type_error():
            PlatformAdapter().fetch_via_recording(
                driver=MagicMock(), url="https://x.com/v/1", duration_sec=100,
                audio_factory=af, transcriber=tx, screenshot_controller=sc,
            )


def pytest_raises_type_error():
    """Helper:返回一个 context manager,期望 TypeError。"""
    import pytest
    return pytest.raises(TypeError)