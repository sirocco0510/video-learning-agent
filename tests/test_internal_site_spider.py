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
    """_fetch_cookies_and_token 的 async 替身(返回 (cookies, token) 元组)。

    生产代码 list_tasks / fetch_m3u8 走 _borrow_auth → _fetch_cookies_and_token,
    此 helper 让测试绕过真实 Chrome CDP,直接注入假数据。
    """
    return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}], "fake-jwt-token"


@pytest.mark.asyncio
async def test_fetch_m3u8_calls_preinit_then_kngplay(monkeypatch):
    """fetch_m3u8 顺序调 preinit + kngPlay, 从 playDetails 选 resolution 匹配的 url。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid-1", resolution="720p")

    async def fake_borrow():
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}], "fake-jwt-token"

    monkeypatch.setattr(spider, "_fetch_cookies_and_token", fake_borrow)

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
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}], "fake-jwt-token"

    monkeypatch.setattr(spider, "_fetch_cookies_and_token", fake_borrow)

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
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}], "fake-jwt-token"

    monkeypatch.setattr(spider, "_fetch_cookies_and_token", fake_borrow)

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
    monkeypatch.setattr(spider, "_fetch_cookies_and_token", _fake_cookies)

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
    assert str(tasks[0].url) == "https://b-learning.bill-jc.com/kng/#/video/play?kngId=kng-001&projectId=&btid=&gwnlUrl="
    assert [t.id for t in tasks] == ["kng-001", "kng-002", "kng-003"]


@pytest.mark.asyncio
async def test_list_tasks_filters_by_root_label(monkeypatch):
    """root_label 只走该子树内的叶子。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_fetch_cookies_and_token", _fake_cookies)
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
    monkeypatch.setattr(spider, "_fetch_cookies_and_token", _fake_cookies)
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
    monkeypatch.setattr(spider, "_fetch_cookies_and_token", _fake_cookies)
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
    monkeypatch.setattr(spider, "_fetch_cookies_and_token", _fake_cookies)
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


# --- Phase 9.6.3 真实环境适配 (token/source/yxtspanid/catalog_id) ---


@pytest.mark.asyncio
async def test_fetch_cookies_and_token_reads_localstorage_token():
    """_fetch_cookies_and_token 从 bill-jc 页面 localStorage 读 token(非 cookie)。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    fake_browser = MagicMock()
    fake_page = MagicMock()
    fake_page.url = "https://b-learning.bill-jc.com/kng/#/list?cid=xxx"
    fake_page.evaluate = AsyncMock(return_value="fake-jwt-from-localstorage")
    fake_browser.contexts = [MagicMock()]
    fake_browser.contexts[0].cookies = AsyncMock(
        return_value=[{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}]
    )
    fake_browser.contexts[0].pages = [fake_page]
    with patch("vla.subtitle.internal_site_spider.async_playwright") as ap:
        ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        ap.return_value.__aenter__.return_value.chromium = MagicMock()
        ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(
            return_value=fake_browser
        )
        ap.return_value.__aexit__ = AsyncMock(return_value=None)
        cookies, token = await spider._fetch_cookies_and_token()
    assert len(cookies) == 1
    assert cookies[0]["name"] == "tk"
    assert token == "fake-jwt-from-localstorage"
    fake_page.evaluate.assert_awaited_once_with(
        "() => localStorage.getItem('token') || ''"
    )


@pytest.mark.asyncio
async def test_fetch_cookies_and_token_returns_empty_token_when_no_bill_jc_page():
    """bill-jc tab 不存在 → token='' (空字符串,不抛错)。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    fake_browser = MagicMock()
    fake_browser.contexts = [MagicMock()]
    fake_browser.contexts[0].cookies = AsyncMock(
        return_value=[{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}]
    )
    fake_browser.contexts[0].pages = []  # 无 bill-jc tab
    with patch("vla.subtitle.internal_site_spider.async_playwright") as ap:
        ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        ap.return_value.__aenter__.return_value.chromium = MagicMock()
        ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(
            return_value=fake_browser
        )
        ap.return_value.__aexit__ = AsyncMock(return_value=None)
        cookies, token = await spider._fetch_cookies_and_token()
    assert token == ""


def test_auth_headers_includes_source_yxtspanid_and_token():
    """_auth_headers 输出符合 yunxuetang 后端校验要求的 header 集。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    cookies = [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}]
    h = spider._auth_headers(cookies, "fake-jwt-token")
    # 静态 header(从浏览器 Network 抓包验证)
    assert h["source"] == "501"
    assert h["yxt-orgdomain"] == "b-learning.bill-jc.com"
    assert h["x-yxt-product"] == "xxv2"
    assert h["Origin"] == "https://b-learning.bill-jc.com"
    assert h["Referer"] == "https://b-learning.bill-jc.com/"
    # 动态 header
    assert h["Cookie"] == "tk=tv"
    assert h["token"] == "fake-jwt-token"
    # yxtspanid 每次 12 hex(动态生成)
    assert len(h["yxtspanid"]) == 12
    assert all(c in "0123456789abcdef" for c in h["yxtspanid"])
    # 两次调用 yxtspanid 不同
    h2 = spider._auth_headers(cookies, "fake-jwt-token")
    assert h["yxtspanid"] != h2["yxtspanid"]


def test_auth_headers_omits_token_header_when_token_empty():
    """token 为空字符串时不加 token header(避免服务端误解析)。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    cookies = [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}]
    h = spider._auth_headers(cookies, "")
    assert "token" not in h


@pytest.mark.asyncio
async def test_list_tasks_with_catalog_id_skips_tree(monkeypatch):
    """catalog_id 模式跳过 tree,直接 pagelist(从 bill-jc URL 拿)。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_fetch_cookies_and_token", _fake_cookies)

    pagelist_calls = []

    async def fake_post(url, json=None, headers=None, **kwargs):
        # tree 永远不应该被调
        assert "tree" not in url, "tree should not be called when catalog_id is provided"
        pagelist_calls.append(json["catalogId"])
        body = {
            "datas": [
                {"id": "kng-A", "title": "A"},
                {"id": "kng-B", "title": "B"},
            ]
        }
        return MagicMock(status_code=200, json=lambda: body)

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        tasks = await spider.list_tasks(
            catalog_id="4ff8c024-219c-4e49-91d0-ec869bcf859f", limit=10
        )
    assert pagelist_calls == ["4ff8c024-219c-4e49-91d0-ec869bcf859f"]
    assert [t.id for t in tasks] == ["kng-A", "kng-B"]


@pytest.mark.asyncio
async def test_list_tasks_rejects_both_root_label_and_catalog_id(monkeypatch):
    """root_label 与 catalog_id 互斥(避免歧义)。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_fetch_cookies_and_token", _fake_cookies)
    with pytest.raises(ValueError, match="互斥"):
        await spider.list_tasks(
            root_label="技术分享", catalog_id="some-uuid", limit=10
        )


# --- Phase 10: --parse-only support (2026-09-10) ---


@pytest.mark.asyncio
async def test_fetch_metadata_returns_dict_with_playback_info(monkeypatch):
    """fetch_metadata 返回 dict, 含 m3u8_url / fileId / subtitles_flag / all_resolutions。

    2026-09-10 真账号探勘结论:kngPlay 顶层响应不包含业务元数据
    (title / duration / college_id),只返播放配置。所以这三个字段固定 None,
    metadata 只覆盖"播放就绪"信息。
    """
    spider = InternalSiteSpider(
        cdp_url="http://localhost:9222", college_id="cid", resolution="720p"
    )

    async def fake_borrow():
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}], "fake-jwt"

    monkeypatch.setattr(spider, "_fetch_cookies_and_token", fake_borrow)

    kngplay_payload = {
        "playDetails": [
            {"url": "https://video.bill-jc.com/a_1080p.m3u8", "desc": "1080p"},
            {"url": "https://video.bill-jc.com/a_720p.m3u8", "desc": "720p"},
            {"url": "https://video.bill-jc.com/a_480p.m3u8", "desc": "480p"},
            {"url": "https://video.bill-jc.com/a_360p.m3u8", "desc": "360p"},
        ],
        "fileId": "uuid-real-file",
        "subtitlesFlag": 0,
    }
    responses = [
        MagicMock(status_code=200, json=lambda: {"code": 0}),
        MagicMock(status_code=200, json=lambda: kngplay_payload),
    ]

    async def fake_post(url, json=None, headers=None, **kwargs):
        return responses[0 if "preinit" in url else 1]

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        meta = await spider.fetch_metadata("kng-id-abc")

    assert isinstance(meta, dict)
    assert meta["kng_id"] == "kng-id-abc"
    assert meta["m3u8_url"] == "https://video.bill-jc.com/a_720p.m3u8"  # resolution=720p
    assert meta["resolution"] == "720p"
    assert meta["fileId"] == "uuid-real-file"
    assert meta["subtitles_flag"] == 0  # 无官方字幕,需抽音转写
    assert meta["all_resolutions"] == ["1080p", "720p", "480p", "360p"]
    # 业务字段固定 None(kngPlay 不返)
    assert meta["title"] is None
    assert meta["duration_sec"] is None
    assert meta["college_id"] is None


@pytest.mark.asyncio
async def test_fetch_metadata_subtitles_flag_one_means_official_available(monkeypatch):
    """subtitlesFlag=1 表示服务端有官方字幕(无需抽音)。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")

    async def fake_borrow():
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}], "fake-jwt"

    monkeypatch.setattr(spider, "_fetch_cookies_and_token", fake_borrow)

    kngplay_payload = {
        "playDetails": [{"url": "https://video.bill-jc.com/a_720p.m3u8", "desc": "720p"}],
        "subtitlesFlag": 1,
    }
    responses = [
        MagicMock(status_code=200, json=lambda: {"code": 0}),
        MagicMock(status_code=200, json=lambda: kngplay_payload),
    ]

    async def fake_post(url, json=None, headers=None, **kwargs):
        return responses[0 if "preinit" in url else 1]

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        meta = await spider.fetch_metadata("kng-with-cc")

    assert meta["subtitles_flag"] == 1


@pytest.mark.asyncio
async def test_fetch_metadata_propagates_fetch_m3u8_errors(monkeypatch):
    """fetch_metadata 在 preinit/kngPlay 失败时抛 RuntimeError(同 fetch_m3u8)。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")

    async def fake_borrow():
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}], "fake-jwt"

    monkeypatch.setattr(spider, "_fetch_cookies_and_token", fake_borrow)

    fake_post = AsyncMock(return_value=MagicMock(status_code=401, text="Unauthorized"))
    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        with pytest.raises(RuntimeError, match="preinit 失败"):
            await spider.fetch_metadata("kng-x")


@pytest.mark.asyncio
async def test_fetch_metadata_resolves_requested_resolution(monkeypatch):
    """resolution=480p 时选 desc==480p, 不是第一档。"""
    spider = InternalSiteSpider(
        cdp_url="http://localhost:9222", college_id="cid", resolution="480p"
    )

    async def fake_borrow():
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}], "fake-jwt"

    monkeypatch.setattr(spider, "_fetch_cookies_and_token", fake_borrow)

    kngplay_payload = {
        "playDetails": [
            {"url": "https://video.bill-jc.com/a_720p.m3u8", "desc": "720p"},
            {"url": "https://video.bill-jc.com/a_480p.m3u8", "desc": "480p"},
        ],
        "fileId": "uuid-2",
    }
    responses = [
        MagicMock(status_code=200, json=lambda: {"code": 0}),
        MagicMock(status_code=200, json=lambda: kngplay_payload),
    ]

    async def fake_post(url, json=None, headers=None, **kwargs):
        return responses[0 if "preinit" in url else 1]

    with patch(
        "vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=_fake_client(fake_post)
    ):
        meta = await spider.fetch_metadata("kng-480p")

    assert meta["m3u8_url"] == "https://video.bill-jc.com/a_480p.m3u8"
    assert meta["resolution"] == "480p"
