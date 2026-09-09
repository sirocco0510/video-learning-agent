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


def _fake_client(post):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = post
    return client


@pytest.mark.asyncio
async def test_fetch_m3u8_calls_preinit_then_kngplay(monkeypatch):
    """fetch_m3u8 顺序调 preinit + kngPlay, 从 playDetails 选 resolution 匹配的 url。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid-1", resolution="720p")

    async def fake_borrow():
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}]

    monkeypatch.setattr(spider, "_borrow_cookies", fake_borrow)

    kngplay_payload = {
        "playDetails": [
            {"url": "https://video.bill-jc.com/a_720p.m3u8", "desc": "720p", "vertical": False},
            {"url": "https://video.bill-jc.com/a_480p.m3u8", "desc": "480p", "vertical": False},
            {"url": "https://video.bill-jc.com/a_360p.m3u8", "desc": "360p", "vertical": False},
        ],
        "fileId": "uuid-1",
    }
    responses = [
        MagicMock(status_code=200, json=lambda: {"code": 0, "msg": "success"}),
        MagicMock(status_code=200, json=lambda: kngplay_payload),
    ]
    call_log = []

    async def fake_post(url, json=None, headers=None, **kwargs):
        call_log.append((url, json, headers))
        return responses[len(call_log) - 1]

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        url = await spider.fetch_m3u8("kng-id-abc")

    assert url == "https://video.bill-jc.com/a_720p.m3u8"
    assert len(call_log) == 2
    assert "preinit" in call_log[0][0]
    assert "kngPlay" in call_log[1][0]
    assert call_log[0][1]["kngId"] == "kng-id-abc"
    assert call_log[1][1]["kngId"] == "kng-id-abc"
    # cookie 带上了
    assert call_log[0][2]["Cookie"] == "tk=tv"
    assert call_log[0][2]["Origin"] == "https://b-learning.bill-jc.com"


@pytest.mark.asyncio
async def test_fetch_m3u8_falls_back_to_first_if_resolution_missing(monkeypatch):
    """resolution 不存在时 fallback 到 playDetails[0]。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid", resolution="720p")

    async def fake_borrow():
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}]

    monkeypatch.setattr(spider, "_borrow_cookies", fake_borrow)

    kngplay_payload = {"playDetails": [{"url": "https://video.bill-jc.com/a_480p.m3u8", "desc": "480p"}]}
    responses = [
        MagicMock(status_code=200, json=lambda: {"code": 0}),
        MagicMock(status_code=200, json=lambda: kngplay_payload),
    ]
    call_log = []

    async def fake_post(url, json=None, headers=None, **kwargs):
        call_log.append(url)
        return responses[len(call_log) - 1]

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        url = await spider.fetch_m3u8("kng-x")
    assert url == "https://video.bill-jc.com/a_480p.m3u8"


@pytest.mark.asyncio
async def test_fetch_m3u8_raises_on_401(monkeypatch):
    """preinit 401 → RuntimeError 提示 preinit 失败, 且不再调 kngPlay。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")

    async def fake_borrow():
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}]

    monkeypatch.setattr(spider, "_borrow_cookies", fake_borrow)

    fake_post = AsyncMock(return_value=MagicMock(status_code=401, text="Unauthorized"))
    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        with pytest.raises(RuntimeError, match="preinit 失败"):
            await spider.fetch_m3u8("kng-x")
    assert fake_post.await_count == 1
