"""InternalSiteAdapter stub 测试(SSOT: requirements.md FR-2.18 + implementation-plan.md Phase 3.4 + F2-7)。

公司内部视频网站 adapter stub:目前无 API 格式/无字幕提取逻辑(等账号下发),
三个 fetch 方法全部返回 None;只验证 match + Registry 集成。

后续等公司下发账号 / 拿到页面结构后,逐步实现。
"""

from unittest.mock import MagicMock

import pytest

from vla.subtitle.internal_site_adapter import InternalSiteAdapter


def _stub_deps():
    """F2-7 → F2-10 简化:InternalSiteAdapter 必填 3 deps(测试无关其内容,MagicMock 即可)。

    2026-09-10 轻量化:tab_recorder 已从 deps 删除(原 F2-7 4 deps 缩为 3 deps)。
    """
    return dict(
        audio_factory=MagicMock(),
        transcriber=MagicMock(),
        screenshot_controller=MagicMock(),
    )


class TestMatch:
    @pytest.mark.parametrize(
        "url",
        [
            "https://internal.example.com/v/123",
            "https://video.corp.local/play/abc",
            # Phase 9.6:bill-jc 内部学习平台
            "https://b-learning.bill-jc.com/learn/kng-001",
            "https://b-learning.bill-jc.com/learn/kng-002?foo=bar",
            # B站不算内部
        ],
    )
    def test_matches_configured_domains(self, url: str):
        """match() 应该命中预定义的内部域名集合(可在配置/实现里扩展)。"""
        assert InternalSiteAdapter.match(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.bilibili.com/video/BV1xxx",
            "https://www.youtube.com/watch?v=xxx",
            # Phase 9.6:bill-jc 父域不在 _INTERNAL_DOMAINS 时不进 match
            # (当前实现用 substring 匹配,所以 bill-jc 任何子串都命中 —
            #  主要保护是后面 /learn/<id> 解析 + cookie 域过滤。)
            "https://api.example.com/v/123",
            "https://google.com/",
        ],
    )
    def test_does_not_match_external_sites(self, url: str):
        assert InternalSiteAdapter.match(url) is False


class TestFetchStubs:
    def test_fetch_api_subtitle_returns_none(self):
        """stub: 无 API 实现,返回 None。"""
        adapter = InternalSiteAdapter(**_stub_deps())
        assert adapter.fetch_api_subtitle("https://internal.example.com/v/1") is None

    def test_fetch_browser_subtitle_returns_none(self):
        adapter = InternalSiteAdapter(**_stub_deps())
        assert adapter.fetch_browser_subtitle(driver=None, url="https://internal.example.com/v/1") is None

    def test_fetch_via_recording_returns_none(self):
        adapter = InternalSiteAdapter(**_stub_deps())
        assert adapter.fetch_via_recording(driver=None, url="https://internal.example.com/v/1", duration_sec=30) is None


class TestRegistryIntegration:
    def test_can_be_registered(self):
        """InternalSiteAdapter 应该能注册到 PlatformAdapterRegistry。"""
        from vla.subtitle.platform_adapter import PlatformAdapterRegistry

        reg = PlatformAdapterRegistry()
        reg.register(InternalSiteAdapter)
        # Registry `cls()` 调用不带 kwargs → InternalSiteAdapter 必须支持无参实例化
        # (实际 F2-7 改了 __init__,这里需要 stub_deps 的另一种路径)
        # 解决:用 register_instance 预构建一个 stub adapter
        instance = InternalSiteAdapter(**_stub_deps())
        reg.register_instance(instance)
        adapter = reg.get_for_url("https://internal.example.com/v/1")
        assert adapter is instance
        assert isinstance(adapter, InternalSiteAdapter)


class TestPhase96SpiderHook:
    """Phase 9.6:InternalSiteAdapter 接入 InternalSiteSpider,fetch_via_spider 返回 m3u8 元数据。"""

    def test_fetch_via_spider_returns_m3u8_metadata(self, monkeypatch):
        """fetch_via_spider 解析 URL → kng_id → 调 spider.fetch_m3u8 → 返回
        (None, {"video_url": m3u8, "via": "internal_spider"})。

        注意:adapter._run_spider_fetch 用 asyncio.run 桥接;测试里直接
        monkeypatch 该方法绕过 event loop 创建(本测试只验 shape + URL 解析)。
        """
        fake_spider = MagicMock()
        adapter = InternalSiteAdapter(**_stub_deps(), spider=fake_spider)
        fake_m3u8 = "https://video.bill-jc.com/a_720p.m3u8"

        def fake_run(spider_obj, kng_id):
            assert spider_obj is fake_spider
            assert kng_id == "kng-001"
            return fake_m3u8

        monkeypatch.setattr(adapter, "_run_spider_fetch", fake_run)

        text, meta = adapter.fetch_via_spider(
            "https://b-learning.bill-jc.com/learn/kng-001"
        )

        assert text is None
        assert meta == {"video_url": fake_m3u8, "via": "internal_spider"}

    def test_fetch_via_spider_returns_none_without_learn_path(self):
        """URL 不含 /learn/<id> → 返回 None(spider 不该被打扰)。"""
        adapter = InternalSiteAdapter(**_stub_deps(), spider=MagicMock())

        assert adapter.fetch_via_spider("https://b-learning.bill-jc.com/") is None
        assert adapter.fetch_via_spider(
            "https://b-learning.bill-jc.com/catalog/123"
        ) is None

    def test_fetch_via_spider_returns_none_without_spider(self):
        """spider 未注入(向后兼容 _stub_deps())→ 返回 None,不抛。"""
        adapter = InternalSiteAdapter(**_stub_deps())
        assert adapter._spider is None  # type: ignore[attr-defined]
        assert adapter.fetch_via_spider(
            "https://b-learning.bill-jc.com/learn/kng-001"
        ) is None

    def test_fetch_via_spider_handles_spider_exception(self, monkeypatch):
        """spider.fetch_m3u8 抛错 → 返回 None(已 log warning,不抛给 strategy)。"""
        adapter = InternalSiteAdapter(**_stub_deps(), spider=MagicMock())

        def fake_run(_spider, _kng_id):
            raise RuntimeError("kngPlay 失败 status=401")

        monkeypatch.setattr(adapter, "_run_spider_fetch", fake_run)
        assert adapter.fetch_via_spider(
            "https://b-learning.bill-jc.com/learn/kng-401"
        ) is None

    def test_constructor_accepts_spider_kwarg(self):
        """spider kwarg 注入后属性可见(无 type 约束,MagicMock 即可)。"""
        fake_spider = MagicMock()
        adapter = InternalSiteAdapter(**_stub_deps(), spider=fake_spider)
        assert adapter._spider is fake_spider  # type: ignore[attr-defined]

    def test_constructor_spider_defaults_to_none(self):
        """spider 缺省 = None(_stub_deps 不传也不报错,向后兼容)。"""
        adapter = InternalSiteAdapter(**_stub_deps())
        assert adapter._spider is None  # type: ignore[attr-defined]

    def test_constructor_accepts_no_args(self):
        """Round 3 修复:`InternalSiteAdapter()` 不报 TypeError,支持
        PlatformAdapterRegistry 的 `register(cls)` 无参实例化 fallback。

        历史背景:F2-7 引入 4 个 REQUIRED deps(audio_factory / tab_recorder /
        transcriber / screenshot_controller),Registry 的 `get_for_url` 走
        `cls()` 无参构造时会抛 TypeError。Phase 9.6 把所有 deps 改成 None-default
        后,class-registration 在 internal_spider=None 的生产路径不再炸。
        2026-09-10 轻量化:tab_recorder 已从 deps 删除,3 deps 缩为 3 deps(原 4 → 3)。
        """
        adapter = InternalSiteAdapter()
        assert adapter._audio_factory is None  # type: ignore[attr-defined]
        assert adapter._transcriber is None  # type: ignore[attr-defined]
        assert adapter._screenshot_controller is None  # type: ignore[attr-defined]
        assert adapter._spider is None  # type: ignore[attr-defined]
        # match 仍能用;fetch_via_spider 因 _spider=None → None(预期)。
        assert InternalSiteAdapter.match("https://b-learning.bill-jc.com/learn/x") is True
        assert adapter.fetch_via_spider("https://b-learning.bill-jc.com/learn/x") is None

    def test_registry_class_register_no_args_does_not_typeerror(self):
        """`PlatformAdapterRegistry.register(InternalSiteAdapter)` + get_for_url
        走 `cls()` 无参实例化必须不抛 TypeError(否则 fetch_asset 静默降级)。
        """
        from vla.subtitle.platform_adapter import PlatformAdapterRegistry

        reg = PlatformAdapterRegistry()
        reg.register(InternalSiteAdapter)  # class-register
        # 无 internal_site spider 注入的生产路径;get_for_url 调 InternalSiteAdapter()
        adapter = reg.get_for_url("https://b-learning.bill-jc.com/learn/kng-1")
        assert isinstance(adapter, InternalSiteAdapter)
        # fetch_via_spider 在 _spider=None 时返 None(不抛)
        assert adapter.fetch_via_spider("https://b-learning.bill-jc.com/learn/kng-1") is None