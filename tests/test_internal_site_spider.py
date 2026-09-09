import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from vla.subtitle.internal_site_spider import InternalSiteSpider


def test_construct_accepts_default_resolution():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    assert s.resolution == "720p"
    assert s._cookie_ttl_sec == 1800


def test_construct_accepts_custom_resolution():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc", resolution="360p")
    assert s.resolution == "360p"


@pytest.mark.asyncio
async def test_borrow_cookies_filters_yunxuetang_domain():
    """_borrow_cookies 只保留 yunxuetang.cn / bill-jc.com 域 cookie。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    fake_cookies = [
        {"name": "tk1", "value": "v1", "domain": ".yunxuetang.cn"},
        {"name": "tk2", "value": "v2", "domain": ".bill-jc.com"},
        {"name": "tk3", "value": "v3", "domain": ".google.com"},  # 应过滤
    ]
    fake_browser = MagicMock()
    fake_browser.contexts = [MagicMock()]
    fake_browser.contexts[0].cookies = AsyncMock(return_value=fake_cookies)
    with patch("vla.subtitle.internal_site_spider.async_playwright") as ap:
        ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        ap.return_value.__aenter__.return_value.chromium = MagicMock()
        ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
        ap.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await spider._borrow_cookies()
    assert len(result) == 2
    domains = {c["domain"] for c in result}
    assert ".yunxuetang.cn" in domains
    assert ".bill-jc.com" in domains
    assert ".google.com" not in domains


@pytest.mark.asyncio
async def test_borrow_cookies_caches_within_ttl():
    """30 min TTL 内不重借 cookie — 验证 connect_over_cdp 只被调用 1 次。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    fake_cookies = [{"name": "tk1", "value": "v1", "domain": ".yunxuetang.cn"}]
    fake_browser = MagicMock()
    fake_browser.contexts = [MagicMock()]
    fake_browser.contexts[0].cookies = AsyncMock(return_value=fake_cookies)
    with patch("vla.subtitle.internal_site_spider.async_playwright") as ap:
        ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        ap.return_value.__aenter__.return_value.chromium = MagicMock()
        ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(
            side_effect=[fake_browser, AssertionError("cached call should not reconnect")]
        )
        ap.return_value.__aexit__ = AsyncMock(return_value=None)
        first = await spider._borrow_cookies()
        second = await spider._borrow_cookies()
    assert first == second
    assert len(second) == 1


@pytest.mark.asyncio
async def test_borrow_cookies_raises_with_chrome_debug_hint():
    """Chrome debug 未启 → RuntimeError 包含 hint。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9999", college_id="abc")
    with patch("vla.subtitle.internal_site_spider.async_playwright") as ap:
        ap.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("ECONNREFUSED"))
        ap.return_value.__aexit__ = AsyncMock(return_value=None)
        with pytest.raises(RuntimeError, match="Chrome debug"):
            await spider._borrow_cookies()
