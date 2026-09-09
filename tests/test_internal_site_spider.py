import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from vla.models import VideoTask
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


async def _fake_cookies():
    """_borrow_cookies 的 async 替身(list_tasks 里是 await 调用)。"""
    return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}]


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


@pytest.mark.asyncio
async def test_list_tasks_walks_tree_then_paginates(monkeypatch):
    """list_tasks 调 tree, 找 leaf, 对每个 leaf 调 pagelist, 转 VideoTask。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", _fake_cookies)

    tree_payload = [
        {"id": "leaf-1", "parentId": "root", "label": "技术分享", "kngCount": 2, "children": []},
        {
            "id": "node-1",
            "parentId": "root",
            "label": "中间",
            "kngCount": 0,
            "children": [
                {
                    "id": "leaf-2",
                    "parentId": "node-1",
                    "label": "叶子2",
                    "kngCount": 1,
                    "children": [],
                }
            ],
        },
    ]
    pagelist_leaf1 = {
        "datas": [
            {"id": "kng-001", "title": "视频1", "coverUrl": "..."},
            {"id": "kng-002", "title": "视频2", "coverUrl": "..."},
        ],
        "totalCount": 2,
    }
    pagelist_leaf2 = {
        "datas": [{"id": "kng-003", "title": "视频3", "coverUrl": "..."}],
        "totalCount": 1,
    }

    tree_payloads = []
    pagelist_calls = []

    async def fake_post(url, json=None, headers=None, **kwargs):
        if "tree" in url:
            tree_payloads.append((json, headers))
            return MagicMock(status_code=200, json=lambda: tree_payload)
        if "pagelist" in url:
            pagelist_calls.append(json["catalogId"])
            body = pagelist_leaf1 if json["catalogId"] == "leaf-1" else pagelist_leaf2
            assert json["collegeId"] == "cid"
            return MagicMock(status_code=200, json=lambda: body)
        raise ValueError(f"unexpected URL: {url}")

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        tasks = await spider.list_tasks(limit=10)

    # tree 请求带上 collegeId + 借来的 cookie
    assert tree_payloads[0][0] == {"pmType": "0", "collegeId": "cid"}
    assert tree_payloads[0][1]["Cookie"] == "tk=tv"
    # leaf-2 应被 visit(它 kngCount=1 > 0);kngCount=0 的中间节点(node-1)不直接查
    assert pagelist_calls == ["leaf-1", "leaf-2"]
    assert len(tasks) == 3
    assert all(isinstance(t, VideoTask) for t in tasks)
    assert tasks[0].id == "kng-001"
    assert tasks[0].title == "视频1"
    assert str(tasks[0].url) == "https://b-learning.bill-jc.com/learn/kng-001"
    assert [t.id for t in tasks] == ["kng-001", "kng-002", "kng-003"]


@pytest.mark.asyncio
async def test_list_tasks_filters_by_root_label(monkeypatch):
    """root_label 只走该子树内的叶子。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", _fake_cookies)
    tree_payload = [
        {"id": "leaf-1", "parentId": "root", "label": "技术分享", "kngCount": 1, "children": []},
        {"id": "leaf-2", "parentId": "root", "label": "财务培训", "kngCount": 1, "children": []},
    ]
    pagelist_calls = []

    async def fake_post(url, json=None, headers=None, **kwargs):
        if "tree" in url:
            return MagicMock(status_code=200, json=lambda: tree_payload)
        if "pagelist" in url:
            pagelist_calls.append(json["catalogId"])
            body = {"datas": [{"id": f"kng-{json['catalogId']}", "title": "t"}]}
            return MagicMock(status_code=200, json=lambda: body)
        raise ValueError(url)

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        tasks = await spider.list_tasks(root_label="技术分享", limit=10)
    assert pagelist_calls == ["leaf-1"]  # 只访问 leaf-1
    assert [t.id for t in tasks] == ["kng-leaf-1"]


@pytest.mark.asyncio
async def test_list_tasks_root_label_not_found_raises(monkeypatch):
    """root_label 找不到 → ValueError。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", _fake_cookies)
    tree_payload = [{"id": "leaf-1", "label": "其他分类", "kngCount": 1, "children": []}]

    async def fake_post(url, json=None, headers=None, **kwargs):
        assert "tree" in url, f"root_label 无匹配时不该继续调 {url}"
        return MagicMock(status_code=200, json=lambda: tree_payload)

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        with pytest.raises(ValueError, match="root_label not found"):
            await spider.list_tasks(root_label="不存在的目录")


@pytest.mark.asyncio
async def test_list_tasks_respects_limit(monkeypatch):
    """limit=3 时只返 3 条, 即使 leaf 内有更多。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", _fake_cookies)
    tree_payload = [{"id": "leaf-1", "label": "L", "kngCount": 5, "children": []}]
    pagelist_body = {"datas": [{"id": f"kng-{i}", "title": f"t{i}"} for i in range(5)]}

    async def fake_post(url, json=None, headers=None, **kwargs):
        if "tree" in url:
            return MagicMock(status_code=200, json=lambda: tree_payload)
        return MagicMock(status_code=200, json=lambda: pagelist_body)

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        tasks = await spider.list_tasks(limit=3)
    assert [t.id for t in tasks] == ["kng-0", "kng-1", "kng-2"]


@pytest.mark.asyncio
async def test_list_tasks_skips_empty_leaves(monkeypatch):
    """kngCount=0 节点不调 pagelist。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", _fake_cookies)
    tree_payload = [
        {"id": "empty-leaf", "label": "空", "kngCount": 0, "children": []},
        {"id": "full-leaf", "label": "满", "kngCount": 1, "children": []},
    ]
    pagelist_calls = []

    async def fake_post(url, json=None, headers=None, **kwargs):
        if "tree" in url:
            return MagicMock(status_code=200, json=lambda: tree_payload)
        pagelist_calls.append(json["catalogId"])
        body = {"datas": [{"id": f"kng-{json['catalogId']}", "title": "t"}]}
        return MagicMock(status_code=200, json=lambda: body)

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        await spider.list_tasks(limit=10)
    assert pagelist_calls == ["full-leaf"]
