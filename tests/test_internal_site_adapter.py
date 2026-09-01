"""InternalSiteAdapter stub 测试(SSOT: requirements.md FR-2.18 + implementation-plan.md Phase 3.4)。

公司内部视频网站 adapter stub:目前无 API 格式/无字幕提取逻辑(等账号下发),
三个 fetch 方法全部返回 None;只验证 match + Registry 集成。

后续等公司下发账号 / 拿到页面结构后,逐步实现。
"""

import pytest

from vla.subtitle.internal_site_adapter import InternalSiteAdapter


class TestMatch:
    @pytest.mark.parametrize(
        "url",
        [
            "https://internal.example.com/v/123",
            "https://video.corp.local/play/abc",
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
        ],
    )
    def test_does_not_match_external_sites(self, url: str):
        assert InternalSiteAdapter.match(url) is False


class TestFetchStubs:
    def test_fetch_api_subtitle_returns_none(self):
        """stub: 无 API 实现,返回 None。"""
        adapter = InternalSiteAdapter()
        assert adapter.fetch_api_subtitle("https://internal.example.com/v/1") is None

    def test_fetch_browser_subtitle_returns_none(self):
        adapter = InternalSiteAdapter()
        assert adapter.fetch_browser_subtitle(driver=None, url="https://internal.example.com/v/1") is None

    def test_fetch_via_recording_returns_none(self):
        adapter = InternalSiteAdapter()
        assert adapter.fetch_via_recording(driver=None, url="https://internal.example.com/v/1", duration_sec=30) is None


class TestRegistryIntegration:
    def test_can_be_registered(self):
        """InternalSiteAdapter 应该能注册到 PlatformAdapterRegistry。"""
        from vla.subtitle.platform_adapter import PlatformAdapterRegistry

        reg = PlatformAdapterRegistry()
        reg.register(InternalSiteAdapter)
        adapter = reg.get_for_url("https://internal.example.com/v/1")
        assert isinstance(adapter, InternalSiteAdapter)