# Phase 9.6 — InternalSiteSpider 实装 + end-to-end Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实装 `InternalSiteSpider`(4 个 yunxuetang API) + 写 end-to-end spike 脚本 `scripts/spike_bill_jc_full.py`,真账号跑通爬取公司视频 → ffmpeg 流式抽音 → faster-whisper 转写 → 质量门控。

**Architecture:**
```
scripts/spike_bill_jc_full.py
  → VideoLearningAgent._process_one (existing, T10)
    → fetch_asset (existing, T7 — path ② 走 internal_spider m3u8)
       → extract_m3u8_audio (NEW — ffmpeg 流式 m3u8 → wav, 不缓存 mp4)
    → process_asset (existing, T8)
       → transcriber.transcribe (existing, T3)
       → quality_checker.check (auto-construct in T13)
```

**Tech Stack:** Python 3.12, uv, src layout, pydantic v2, httpx (async), ffmpeg subprocess, faster-whisper, Playwright (cookie 借取 only).

**Spec:** `docs/superpowers/specs/2026-09-09-bill-jc-spider-impl-design.md` — 本 plan 论据出自该 spec。

---

## Global Constraints(所有 task 必读,直接复制自 spec)

- Python 3.12, uv, src layout (`src/vla/`), lockfile 已提交
- pydantic v2 BaseModel; `VideoTask.url: HttpUrl`(用 str URL 实例化即可)
- pytest-asyncio, `asyncio_mode = "auto"`
- **字幕永远本地**(只用 faster-whisper / B站 CC / VideoTrans, **禁止云端转写**)
- 云端 LLM 限定两件事:① 字幕质量检查(读 `pass` + `score`)② 6h 批量总结
- **磁盘友好**:256 GB 机器,峰值 < 1 GB;**本轮新增铁律:m3u8 不缓存为 mp4,直接流式抽 wav**(~50MB /3h)
- macOS 权限:屏幕录制(录屏路径), 通知(B 级), 辅助功能(A 级弹窗)首次需用户授权
- 涉及外部 HTTP 调用统一用 **httpx.AsyncClient**(项目内已有 pattern, 见 `scripts/probe_bill_jc_*.py`)
- 不引入 selenium / playwright 打开 b-learning.bill-jc.com; cookie 借取走 CDP `connect_over_cdp` + `context.cookies()`
- 涉及外网 API 失败时 **fail-fast + 给用户清晰 hint**(cookie 过期提示重启 Chrome debug;m3u8 过期提示重 fetch)
- TDD: 写实现前先写失败测试, 跑红 → 写最小实现 → 跑绿 → commit
- import 顺序: stdlib → third-party → local; type import `from __future__ import annotations` + `TYPE_CHECKING`
- logging: `logger = logging.getLogger(__name__)`, 不要 print
- 路径: `pathlib.Path`
- no dead code
- commit 规范: `<scope>: <imperative summary>`, 本仓库现有 conventional 风格

---

## File Structure Summary

| 文件 | 角色 |
|---|---|
| `src/vla/subtitle/internal_site_spider.py` | **完全重写**:stub → 4 API + cookie 借取 + 2 个 public 方法 |
| `src/vla/transcribe/extract.py` | **+1 函数** `extract_m3u8_audio` |
| `src/vla/main_provider.py` | **改 ~10 行** fetch_asset path ② 调 extract_m3u8_audio |
| `tests/test_internal_site_spider.py` | **改 + 加** 4 placeholder → 7 真测试 |
| `tests/test_extract_audio.py` | **+2** m3u8_audio 测试 |
| `tests/test_fetch_asset.py` | **+1** 集成测试 |
| `scripts/spike_bill_jc_full.py` | **新** end-to-end CLI 3 模式 |
| `docs/superpowers/backlog.md` | **改 §1** 标 done |

---

## Plan Order of Execution

T1 → T2 → T3 → T4 → T5 → T6

(T1 提供 cookie 借取 helper, T2 + T3 都依赖; T4 改 main_provider.py 需 T1+T3; T5 装 spike 需全部前置; T6 文档收尾)

---

### Task 1: `InternalSiteSpider._borrow_cookies` Playwright CDP 借 cookie

**Files:**
- Modify: `src/vla/subtitle/internal_site_spider.py`(完全重写,从 stub 改成真模块)
- Modify: `tests/test_internal_site_spider.py`(删 4 placeholder, 加 cookie 借取相关测试)
- 不需新文件

**Interfaces:**
- Consumes:
  - `InternalSiteSpider.__init__(cdp_url, college_id, resolution="720p", cookie_ttl_sec=1800)`(已在 stub 中)
  - `playwright.async_api.async_playwright`(已 `uv add` per `scripts/probe_bill_jc_*.py`)
- Produces:
  - `InternalSiteSpider._borrow_cookies() -> list[dict[str, Any]]` — private helper, 返回 yunxuetang.cn / bill-jc.com 域 cookie dict list
  - `InternalSiteSpider._cookie_cache: dict[str, list[dict]] | None = None` — 实例字段, 缓存 cookie + fetch timestamp
  - `InternalSiteSpider._COOKIE_TTL_SEC` 类常量

- [ ] **Step 1: 写失败测试(`test_borrow_cookies_filters_yunxuetang_domain`)**

`tests/test_internal_site_spider.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from vla.subtitle.internal_site_spider import InternalSiteSpider


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_internal_site_spider.py::test_borrow_cookies_filters_yunxuetang_domain -v`
Expected: FAIL with `AttributeError: 'InternalSiteSpider' object has no attribute '_borrow_cookies'`

- [ ] **Step 3: 重写 `internal_site_spider.py`**

```python
"""InternalSiteSpider — b-learning.bill-jc.com 4-API 蜘蛛实现。

完整 spec: `docs/superpowers/specs/2026-09-09-bill-jc-spider-impl-design.md` §3.1 + §4.1。

API 顺序:
  1. POST /kng/kngCatalog/student/tree       (list_tasks 第 1 步)
  2. POST /kng/knowledge/pagelist            (list_tasks 第 2 步)
  3. POST /kng/study/submit/preinit          (fetch_m3u8 第 1 步)
  4. POST /kng/study/kngPlay                 (fetch_m3u8 第 2 步)
cookie 借取:Playwright connect_over_cdp("http://localhost:9222") → context.cookies()
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import httpx
from playwright.async_api import async_playwright

if TYPE_CHECKING:
    from vla.models import VideoTask


logger = logging.getLogger(__name__)

# yunxuetang 域(大小写不敏感匹配 Playwright cookie["domain"])
_YUNXUETANG_DOMAINS = (".yunxuetang.cn", ".bill-jc.com")

# 3 个 API 端点
_TREE_URL = "https://api-phx-ali.yunxuetang.cn/kng/kngCatalog/student/tree"
_PAGELIST_URL = "https://api-phx-ali.yunxuetang.cn/kng/knowledge/pagelist"
_PREINIT_URL = "https://api-phx-ali.yunxuetang.cn/kng/study/submit/preinit"
_KNGPLAY_URL = "https://api-phx-ali.yunxuetang.cn/kng/study/kngPlay"

# 公共 header(含 Origin + Referer, yunxuetang 校验)
_DEFAULT_HEADERS = {
    "Origin": "https://b-learning.bill-jc.com",
    "Referer": "https://b-learning.bill-jc.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Content-Type": "application/json",
}


class InternalSiteSpider:
    """b-learning.bill-jc.com API-driven spider。"""

    def __init__(
        self,
        cdp_url: str = "http://localhost:9222",
        college_id: str = "",
        resolution: str = "720p",
        cookie_ttl_sec: int = 1800,
    ) -> None:
        self.cdp_url = cdp_url
        self.college_id = college_id
        self.resolution = resolution
        self._cookie_ttl_sec = cookie_ttl_sec
        self._cookie_cache: list[dict[str, Any]] | None = None
        self._cookie_fetched_at: float = 0.0

    async def _borrow_cookies(self) -> list[dict[str, Any]]:
        """借 Chrome CDP session cookie, 仅保留 yunxuetang 域。

        缓存策略: 默认 30 min TTL; 过期时重新借。
        失败提示: Chrome debug 未启 → RuntimeError 含 hint。
        """
        now = time.monotonic()
        if self._cookie_cache is not None and (now - self._cookie_fetched_at) < self._cookie_ttl_sec:
            return self._cookie_cache

        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(self.cdp_url)
                context = browser.contexts[0]
                all_cookies = await context.cookies()
        except Exception as e:
            raise RuntimeError(
                f"无法借 cookie(检查 Chrome debug 是否启动: chrome --remote-debugging-port=9222): {e}"
            ) from e

        filtered = [c for c in all_cookies if any(d in c.get("domain", "") for d in _YUNXUETANG_DOMAINS)]
        self._cookie_cache = filtered
        self._cookie_fetched_at = now
        logger.info("borrowed %d yunxuetang cookies", len(filtered))
        return filtered

    def _cookie_header(self, cookies: list[dict[str, Any]]) -> str:
        """把 cookie dict list 合并成 Cookie header 字符串。"""
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    async def list_tasks(
        self,
        *,
        root_label: str | None = None,
        limit: int = 10,
    ) -> list[VideoTask]:
        # 实装见 Task 3
        raise NotImplementedError("实装见 Task 3")

    async def fetch_m3u8(self, kng_id: str) -> str:
        # 实装见 Task 2
        raise NotImplementedError("实装见 Task 2")
```

- [ ] **Step 4: 删旧 placeholder 测试, 跑新测试确认绿**

`tests/test_internal_site_spider.py` 重写为只留 cookie 测试 + 构造测试:

```python
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
        {"name": "tk3", "value": "v3", "domain": ".google.com"},
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


@pytest.mark.asyncio
async def test_borrow_cookies_caches_within_ttl():
    """30 min TTL 内不重借 cookie。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    fake_cookies = [{"name": "tk1", "value": "v1", "domain": ".yunxuetang.cn"}]
    fake_browser = MagicMock()
    fake_browser.contexts = [MagicMock()]
    fake_browser.contexts[0].cookies = AsyncMock(return_value=fake_cookies)
    with patch("vla.subtitle.internal_site_spider.async_playwright") as ap:
        ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        ap.return_value.__aenter__.return_value.chromium = MagicMock()
        ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
        ap.return_value.__aexit__ = AsyncMock(return_value=None)
        await spider._borrow_cookies()
        # 第二次调用, mock 应仍只被调 1 次
        result2 = await spider._borrow_cookies()
    assert len(result2) == 1


@pytest.mark.asyncio
async def test_borrow_cookies_raises_with_chrome_debug_hint():
    """Chrome debug 未启 → RuntimeError 包含 hint。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9999", college_id="abc")
    with patch("vla.subtitle.internal_site_spider.async_playwright") as ap:
        ap.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("ECONNREFUSED"))
        ap.return_value.__aexit__ = AsyncMock(return_value=None)
        with pytest.raises(RuntimeError, match="Chrome debug"):
            await spider._borrow_cookies()
```

Run: `uv run pytest tests/test_internal_site_spider.py -v`
Expected: 4 passed(2 构造 + 3 借 cookie)

- [ ] **Step 5: Commit**

```bash
git add src/vla/subtitle/internal_site_spider.py tests/test_internal_site_spider.py
git commit -m "feat(spider): InternalSiteSpider 实装 _borrow_cookies + cache TTL"
```

---

### Task 2: `InternalSiteSpider.fetch_m3u8` 走 preinit + kngPlay

**Files:**
- Modify: `src/vla/subtitle/internal_site_spider.py`(`fetch_m3u8` 替换 stub)
- Modify: `tests/test_internal_site_spider.py`(加 3 个 fetch_m3u8 测试)

**Interfaces:**
- Consumes:
  - `InternalSiteSpider._borrow_cookies()`(Task 1)
  - `InternalSiteSpider._cookie_header(cookies)`(Task 1)
- Produces:
  - `async def fetch_m3u8(self, kng_id: str) -> str` — 调 preinit + kngPlay, 返回可达 m3u8 URL

- [ ] **Step 1: 写失败测试(`test_fetch_m3u8_calls_preinit_then_kngplay`)**

```python
@pytest.mark.asyncio
async def test_fetch_m3u8_calls_preinit_then_kngplay(monkeypatch):
    """fetch_m3u8 顺序调 preinit + kngPlay, 从 playDetails 选 resolution 匹配的 url。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid-1", resolution="720p")
    # stub _borrow_cookies 返 1 个 cookie
    async def fake_borrow():
        return [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}]
    monkeypatch.setattr(spider, "_borrow_cookies", fake_borrow)

    # 模拟 httpx 两次 POST
    preinit_payload = {"code": 0, "msg": "success"}
    kngplay_payload = {
        "playDetails": [
            {"url": "https://video.bill-jc.com/..._720p.m3u8", "desc": "720p", "vertical": False},
            {"url": "https://video.bill-jc.com/..._480p.m3u8", "desc": "480p", "vertical": False},
            {"url": "https://video.bill-jc.com/..._360p.m3u8", "desc": "360p", "vertical": False},
        ],
        "fileId": "uuid-1",
    }

    responses = [
        AsyncMock(status_code=200, json=lambda: preinit_payload),
        AsyncMock(status_code=200, json=lambda: kngplay_payload),
    ]
    call_log = []

    async def fake_post(url, json=None, headers=None, **kwargs):
        call_log.append((url, json))
        return responses[len(call_log) - 1]

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = fake_post

    with patch("vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=fake_client):
        url = await spider.fetch_m3u8("kng-id-abc")

    assert url == "https://video.bill-jc.com/..._720p.m3u8"
    assert len(call_log) == 2
    assert "preinit" in call_log[0][0]
    assert "kngPlay" in call_log[1][0]
    # preinit body 必含 kng_id
    assert call_log[0][1]["kngId"] == "kng-id-abc"
    assert call_log[1][1]["kngId"] == "kng-id-abc"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_internal_site_spider.py::test_fetch_m3u8_calls_preinit_then_kngplay -v`
Expected: FAIL with `NotImplementedError` (fetch_m3u8 仍 stub)

- [ ] **Step 3: 实现 `fetch_m3u8`**

替换 `src/vla/subtitle/internal_site_spider.py` 中 `fetch_m3u8` 的 NotImplementedError stub:

```python
async def fetch_m3u8(self, kng_id: str) -> str:
    """走 preinit + kngPlay, 返回 resolution 匹配的 m3u8 URL。

    Raises:
        RuntimeError: preinit/kngPlay HTTP 非 200, 或 cookie 借取失败,
                       或 playDetails 为空。
    """
    cookies = await self._borrow_cookies()
    headers = {**_DEFAULT_HEADERS, "Cookie": self._cookie_header(cookies)}

    base_payload = {
        "kngId": kng_id,
        "courseId": "",
        "studyParam": {"originOrgId": "", "previewType": 0},
        "targetCode": "kng",
        "targetId": "",
        "targetParam": {"taskId": "", "projectId": "", "flipId": "", "batchId": ""},
        "customFunctionCode": "",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # 第 1 步: preinit(预热 study session)
        preinit_resp = await client.post(_PREINIT_URL, json=base_payload, headers=headers)
        if preinit_resp.status_code != 200:
            raise RuntimeError(f"preinit 失败 status={preinit_resp.status_code}: {preinit_resp.text[:300]}")

        # 第 2 步: kngPlay(拿 m3u8 URL)
        kngplay_payload = {**base_payload, "fullname": "", "lang": ""}
        kngplay_resp = await client.post(_KNGPLAY_URL, json=kngplay_payload, headers=headers)
        if kngplay_resp.status_code != 200:
            raise RuntimeError(f"kngPlay 失败 status={kngplay_resp.status_code}: {kngplay_resp.text[:300]}")

        data = kngplay_resp.json()
        play_details = data.get("playDetails", [])
        if not play_details:
            raise RuntimeError(f"kngPlay 返回空 playDetails: {data}")

        # 选 desc == self.resolution; 找不到 fallback 到第一个
        match = next((p for p in play_details if p.get("desc") == self.resolution), None)
        url = match["url"] if match else play_details[0]["url"]
        logger.info("fetch_m3u8 kng=%s resolution=%s → %s", kng_id, self.resolution, url)
        return url
```

- [ ] **Step 4: 加 2 个补充测试 + 跑全测确认绿**

```python
@pytest.mark.asyncio
async def test_fetch_m3u8_falls_back_to_first_if_resolution_missing(monkeypatch):
    """resolution 不存在时 fallback 到 playDetails[0]。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid", resolution="720p")
    monkeypatch.setattr(spider, "_borrow_cookies", lambda: [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}])

    kngplay_payload = {
        "playDetails": [
            {"url": "https://video.bill-jc.com/..._480p.m3u8", "desc": "480p"},
        ]  # 没 720p, 应 fallback
    }
    responses = [
        AsyncMock(status_code=200, json=lambda: {"code": 0}),
        AsyncMock(status_code=200, json=lambda: kngplay_payload),
    ]
    call_log = []
    async def fake_post(url, json=None, headers=None, **kwargs):
        call_log.append(url)
        return responses[len(call_log) - 1]
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = fake_post
    with patch("vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=fake_client):
        url = await spider.fetch_m3u8("kng-x")
    assert url == "https://video.bill-jc.com/..._480p.m3u8"


@pytest.mark.asyncio
async def test_fetch_m3u8_raises_on_401(monkeypatch):
    """preinit 401 → RuntimeError 提示 cookie 过期。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", lambda: [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}])

    fake_resp = AsyncMock(status_code=401, text="Unauthorized")
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(return_value=fake_resp)
    with patch("vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(RuntimeError, match="preinit 失败"):
            await spider.fetch_m3u8("kng-x")
```

Run: `uv run pytest tests/test_internal_site_spider.py -v`
Expected: 7 passed(2 构造 + 3 borrow_cookies + 3 fetch_m3u8)

- [ ] **Step 5: Commit**

```bash
git add src/vla/subtitle/internal_site_spider.py tests/test_internal_site_spider.py
git commit -m "feat(spider): fetch_m3u8 走 preinit + kngPlay + resolution 选档"
```

---

### Task 3: `InternalSiteSpider.list_tasks` 走 tree + pagelist

**Files:**
- Modify: `src/vla/subtitle/internal_site_spider.py`(`list_tasks` 替换 stub)
- Modify: `tests/test_internal_site_spider.py`(加 4 个 list_tasks 测试)

**Interfaces:**
- Consumes: `InternalSiteSpider._borrow_cookies()`(Task 1)
- Produces:
  - `async def list_tasks(self, *, root_label=None, limit=10) -> list[VideoTask]`
  - private helper `_flatten_leaves(tree, root_label) -> list[dict]`

- [ ] **Step 1: 写失败测试(`test_list_tasks_walks_tree_then_paginates`)**

```python
@pytest.mark.asyncio
async def test_list_tasks_walks_tree_then_paginates(monkeypatch):
    """list_tasks 调 tree, 找 leaf, 对每个 leaf 调 pagelist, 转 VideoTask。"""
    from vla.models import VideoTask
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", lambda: [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}])

    tree_payload = [
        {"id": "leaf-1", "parentId": "root", "label": "技术分享", "kngCount": 2, "children": []},
        {"id": "node-1", "parentId": "root", "label": "中间", "kngCount": 0, "children": [
            {"id": "leaf-2", "parentId": "node-1", "label": "叶子2", "kngCount": 1, "children": []}
        ]},
    ]
    pagelist_leaf1 = {"datas": [
        {"id": "kng-001", "title": "视频1", "coverUrl": "..."},
        {"id": "kng-002", "title": "视频2", "coverUrl": "..."},
    ], "totalCount": 2}
    pagelist_leaf2 = {"datas": [
        {"id": "kng-003", "title": "视频3", "coverUrl": "..."},
    ], "totalCount": 1}

    pagelist_calls = []
    async def fake_post(url, json=None, headers=None, **kwargs):
        if "tree" in url:
            return AsyncMock(status_code=200, json=lambda: tree_payload)
        if "pagelist" in url:
            pagelist_calls.append(json["catalogId"])
            if json["catalogId"] == "leaf-1":
                return AsyncMock(status_code=200, json=lambda: pagelist_leaf1)
            else:
                return AsyncMock(status_code=200, json=lambda: pagelist_leaf2)
        raise ValueError(f"unexpected URL: {url}")

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = fake_post
    with patch("vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=fake_client):
        tasks = await spider.list_tasks(limit=10)

    # leaf-2 应被 visit(它 kngCount=1 > 0); kngCount=0 节点(node-1)跳过
    assert "leaf-1" in pagelist_calls
    assert "leaf-2" in pagelist_calls
    assert len(tasks) == 3
    assert all(isinstance(t, VideoTask) for t in tasks)
    assert tasks[0].id == "kng-001"
    assert tasks[0].title == "视频1"
    assert str(tasks[0].url) == "https://b-learning.bill-jc.com/learn/kng-001"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_internal_site_spider.py::test_list_tasks_walks_tree_then_paginates -v`
Expected: FAIL with `NotImplementedError` (list_tasks 仍 stub)

- [ ] **Step 3: 实现 `list_tasks` + `_flatten_leaves`**

替换 `src/vla/subtitle/internal_site_spider.py` 中 `list_tasks` stub:

```python
async def list_tasks(
    self,
    *,
    root_label: str | None = None,
    limit: int = 10,
) -> list[VideoTask]:
    """爬 tree + pagelist → VideoTask list(最多 limit 条)。

    root_label: 限定根目录 label(如 "技术分享"); None = 全树。
    多子树同名 → DFS 首个匹配(确定性); 无匹配 → ValueError。
    """
    from vla.models import VideoTask

    cookies = await self._borrow_cookies()
    headers = {**_DEFAULT_HEADERS, "Cookie": self._cookie_header(cookies)}

    async with httpx.AsyncClient(timeout=30) as client:
        # 第 1 步: tree
        tree_resp = await client.post(
            _TREE_URL,
            json={"pmType": "0", "collegeId": self.college_id},
            headers=headers,
        )
        if tree_resp.status_code != 200:
            raise RuntimeError(f"tree 失败 status={tree_resp.status_code}: {tree_resp.text[:300]}")
        tree = tree_resp.json()
        leaves = _flatten_leaves(tree, root_label=root_label)

        # 第 2 步: 对每个 leaf 调 pagelist
        tasks: list[VideoTask] = []
        for leaf in leaves:
            if len(tasks) >= limit:
                break
            page_resp = await client.post(
                _PAGELIST_URL,
                params={"limit": 16, "offset": 0, "orderType": "desc", "orderBy": "createTime"},
                json={
                    "collegeId": self.college_id,
                    "catalogId": leaf["id"],
                    "title": "",
                    "type": "",
                    "allTag": 1,
                    "tagIds": [],
                },
                headers=headers,
            )
            if page_resp.status_code != 200:
                logger.warning("pagelist catalog=%s 失败 status=%d, 跳过", leaf["id"], page_resp.status_code)
                continue
            datas = page_resp.json().get("datas", [])
            for v in datas:
                if len(tasks) >= limit:
                    break
                tasks.append(VideoTask(
                    id=v["id"],
                    title=v.get("title", f"kng-{v['id']}"),
                    url=f"https://b-learning.bill-jc.com/learn/{v['id']}",
                    expected_duration=3600,
                ))
        return tasks


def _flatten_leaves(
    tree: list[dict],
    *,
    root_label: str | None,
) -> list[dict]:
    """递归找 tree 中 kngCount > 0 的叶子节点。

    root_label 非空 → 只返回 label 匹配子树内的叶子;
    DFS 首个匹配子树; 无匹配 → raise ValueError。
    """
    leaves: list[dict] = []

    def _walk(nodes: list[dict], in_scope: bool) -> None:
        for node in nodes:
            label = node.get("label", "")
            children = node.get("children", [])
            current_in_scope = in_scope or (root_label is not None and label == root_label)
            if not children:
                # 真叶子
                if current_in_scope or root_label is None:
                    if node.get("kngCount", 0) > 0:
                        leaves.append(node)
            else:
                # 中间节点, 递归
                _walk(children, current_in_scope)

    if root_label is not None:
        # 检查 root_label 是否存在
        def _exists(nodes: list[dict]) -> bool:
            for n in nodes:
                if n.get("label") == root_label:
                    return True
                if _exists(n.get("children", [])):
                    return True
            return False
        if not _exists(tree):
            raise ValueError(f"root_label not found in catalog tree: {root_label!r}")
    _walk(tree, in_scope=(root_label is None))
    return leaves
```

- [ ] **Step 4: 加 4 个补充测试 + 跑全测确认绿**

```python
@pytest.mark.asyncio
async def test_list_tasks_filters_by_root_label(monkeypatch):
    """root_label 只走该子树内的叶子。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", lambda: [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}])
    tree_payload = [
        {"id": "leaf-1", "parentId": "root", "label": "技术分享", "kngCount": 1, "children": []},
        {"id": "leaf-2", "parentId": "root", "label": "财务培训", "kngCount": 1, "children": []},
    ]
    pagelist_calls = []

    async def fake_post(url, json=None, headers=None, **kwargs):
        if "tree" in url:
            return AsyncMock(status_code=200, json=lambda: tree_payload)
        if "pagelist" in url:
            pagelist_calls.append(json["catalogId"])
            return AsyncMock(status_code=200, json=lambda: {"datas": [
                {"id": f"kng-{json['catalogId']}", "title": "t"}
            ]})
        raise ValueError(url)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = fake_post
    with patch("vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=fake_client):
        tasks = await spider.list_tasks(root_label="技术分享", limit=10)
    assert pagelist_calls == ["leaf-1"]  # 只访问 leaf-1
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_list_tasks_root_label_not_found_raises(monkeypatch):
    """root_label 找不到 → ValueError。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", lambda: [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}])
    tree_payload = [{"id": "leaf-1", "label": "其他分类", "kngCount": 1, "children": []}]

    async def fake_post(url, json=None, headers=None, **kwargs):
        return AsyncMock(status_code=200, json=lambda: tree_payload)
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = fake_post
    with patch("vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(ValueError, match="root_label not found"):
            await spider.list_tasks(root_label="不存在的目录")


@pytest.mark.asyncio
async def test_list_tasks_respects_limit(monkeypatch):
    """limit=3 时只返 3 条, 即使 leaf 内有更多。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", lambda: [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}])
    tree_payload = [{"id": "leaf-1", "label": "L", "kngCount": 1, "children": []}]

    async def fake_post(url, json=None, headers=None, **kwargs):
        if "tree" in url:
            return AsyncMock(status_code=200, json=lambda: tree_payload)
        return AsyncMock(status_code=200, json=lambda: {"datas": [
            {"id": f"kng-{i}", "title": f"t{i}"} for i in range(5)
        ]})

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = fake_post
    with patch("vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=fake_client):
        tasks = await spider.list_tasks(limit=3)
    assert len(tasks) == 3


@pytest.mark.asyncio
async def test_list_tasks_skips_empty_leaves(monkeypatch):
    """kngCount=0 节点不调 pagelist。"""
    spider = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="cid")
    monkeypatch.setattr(spider, "_borrow_cookies", lambda: [{"name": "tk", "value": "tv", "domain": ".yunxuetang.cn"}])
    tree_payload = [
        {"id": "empty-leaf", "label": "空", "kngCount": 0, "children": []},
        {"id": "full-leaf", "label": "满", "kngCount": 1, "children": []},
    ]
    pagelist_calls = []

    async def fake_post(url, json=None, headers=None, **kwargs):
        if "tree" in url:
            return AsyncMock(status_code=200, json=lambda: tree_payload)
        pagelist_calls.append(json["catalogId"])
        return AsyncMock(status_code=200, json=lambda: {"datas": [
            {"id": f"kng-{json['catalogId']}", "title": "t"}
        ]})
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = fake_post
    with patch("vla.subtitle.internal_site_spider.httpx.AsyncClient", return_value=fake_client):
        await spider.list_tasks(limit=10)
    assert "empty-leaf" not in pagelist_calls
    assert "full-leaf" in pagelist_calls
```

Run: `uv run pytest tests/test_internal_site_spider.py -v`
Expected: 11 passed(2 构造 + 3 borrow_cookies + 3 fetch_m3u8 + 4 list_tasks, 减去 1 placeholder 重写)

- [ ] **Step 5: Commit**

```bash
git add src/vla/subtitle/internal_site_spider.py tests/test_internal_site_spider.py
git commit -m "feat(spider): list_tasks 走 tree + pagelist + _flatten_leaves (DFS + root_label 匹配)"
```

---

### Task 4: `extract_m3u8_audio` ffmpeg 流式抽音 + fetch_asset path ② 改造

**Files:**
- Modify: `src/vla/transcribe/extract.py`(加 `extract_m3u8_audio` 函数)
- Modify: `src/vla/main_provider.py`(fetch_asset path ② 改调)
- Modify: `tests/test_extract_audio.py`(加 2 个 m3u8 测试)
- Modify: `tests/test_fetch_asset.py`(加 1 个集成测试)

**Interfaces:**
- Consumes:
  - `subprocess.run`(现有 pattern in `extract_audio`)
  - `RealTextProvider.fetch_asset` 内部, 改动 path ② ~line 115
- Produces:
  - `def extract_m3u8_audio(m3u8_url: str, output_path: Path) -> None`(独立函数, -vn 跳过视频轨)
  - `fetch_asset path ②`: 改 `extract_audio(Path(video_url), wav_path)` → `extract_m3u8_audio(video_url, wav_path)`

- [ ] **Step 1: 写失败测试 `test_extract_m3u8_audio_success`**

`tests/test_extract_audio.py` 末尾加:

```python
from unittest.mock import patch, MagicMock
from vla.transcribe.extract import extract_m3u8_audio


def test_extract_m3u8_audio_success(tmp_path, monkeypatch):
    """extract_m3u8_audio 调 ffmpeg with -vn -ac 1 -ar 16000 -f wav。"""
    out = tmp_path / "audio.wav"
    fake_proc = MagicMock(returncode=0, stderr="")
    captured_cmd = []
    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        out.write_bytes(b"RIFF")
        return fake_proc
    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_m3u8_audio("https://video.bill-jc.com/foo.m3u8", out)
    # 验证 -vn 在 args 里, audio flags 对, wav 落盘
    assert "-vn" in captured_cmd
    assert "-ac" in captured_cmd and "1" in captured_cmd
    assert "-ar" in captured_cmd and "16000" in captured_cmd
    assert "-f" in captured_cmd and "wav" in captured_cmd
    assert "https://video.bill-jc.com/foo.m3u8" in captured_cmd
    assert str(out) in captured_cmd


def test_extract_m3u8_audio_fails_on_ffmpeg_nonzero(tmp_path, monkeypatch):
    """ffmpeg 返回非 0 → RuntimeError, 半截 wav 清掉。"""
    out = tmp_path / "audio.wav"
    out.write_bytes(b"RIFF")
    fake_proc = MagicMock(returncode=1, stderr="Connection refused")
    def fake_run(cmd, **kwargs):
        return fake_proc
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="extract_m3u8_audio failed"):
        extract_m3u8_audio("https://x.com/bad.m3u8", out)
    assert not out.exists()  # 半截 wav 应被清掉
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_extract_audio.py -v -k "m3u8_audio"`
Expected: FAIL with `ImportError: cannot import name 'extract_m3u8_audio'`

- [ ] **Step 3: 在 `extract.py` 加函数**

`src/vla/transcribe/extract.py` 末尾追加(保留 `extract_audio` 不动):

```python
def extract_m3u8_audio(m3u8_url: str, output_path: Path) -> None:
    """ffmpeg 流式抽 m3u8 音轨 → wav, 不缓存视频。

    与 extract_audio 的差异: -vn 跳过视频轨, m3u8 直接走 HLS 流式输入。
    适合 InternalSiteSpider 路径 ②: 3h 视频 ~150MB wav vs 1.5GB mp4。

    Args:
        m3u8_url: HLS manifest URL(可达, 含签名 token)
        output_path: 目标 wav 路径(需 .wav 后缀)

    Raises:
        RuntimeError: ffmpeg 返回非 0(网络/格式错)
        FileNotFoundError: ffmpeg 二进制缺失

    失败语义: 半截 wav 在 except 里被删, 避免磁盘残留。
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-vn",  # 跳过视频轨(磁盘友好: 不缓存 mp4)
        "-i", m3u8_url,
        "-ac", "1", "-ar", "16000", "-f", "wav", str(output_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"extract_m3u8_audio failed: {m3u8_url} → {output_path}: {proc.stderr[:500]}"
            )
    except Exception:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                logger.warning("清理半截 wav 失败 %s, 继续", output_path)
        raise
```

- [ ] **Step 4: 跑 extract_audio 测试确认绿**

Run: `uv run pytest tests/test_extract_audio.py -v`
Expected: 7 passed(5 旧 + 2 新)

- [ ] **Step 5: 改 `main_provider.py` fetch_asset path ② + 写集成测试**

读 `src/vla/main_provider.py` 找 path ② 的 `extract_audio(Path(video_url), wav_path)`(原 line 117 附近, T14 后可能漂移),改:

```python
from vla.transcribe.extract import extract_audio, extract_m3u8_audio
# ...
        if result is not None and (result.source or "").startswith("internal"):
            video_url = (result.metadata or {}).get("video_url")
            if not video_url:
                logger.warning("internal spider 返回无 video_url, 跳过: %s", result)
                return None
            wav_path = self._save_dir / "audio_raw" / f"{task.id}.wav"
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                # OLD: extract_audio(Path(video_url), wav_path)
                extract_m3u8_audio(video_url, wav_path)
            except Exception as e:
                logger.warning("internal spider m3u8 抽音失败 %s: %s", video_url, e)
                return None
            return Asset(text=None, source="whisper_internal_download", audio_path=wav_path, deletable=True)
```

`tests/test_fetch_asset.py` 加:

```python
from unittest.mock import patch, AsyncMock, MagicMock
from vla.models import VideoTask
from vla.subtitle.strategy import SubtitleResult


@pytest.mark.asyncio
async def test_fetch_asset_internal_spider_uses_extract_m3u8_audio(tmp_path):
    """fetch_asset path ② 调 extract_m3u8_audio(不是 extract_audio)。"""
    from vla.main_provider import RealTextProvider
    p = RealTextProvider.__new__(RealTextProvider)
    p.cfg = MagicMock()
    p.strategy = MagicMock()
    p.transcriber = MagicMock()
    p.source_factory = MagicMock()
    p.notifier = MagicMock()
    p.plugin_status = MagicMock()
    p._save_dir = tmp_path
    p.log = MagicMock()
    p.checker = MagicMock()
    p.refiner = None
    p._today_dir = tmp_path  # T14 requirement

    # strategy 返 SubtitleResult(source="internal_spider", metadata={"video_url": "https://x.m3u8"})
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(
        text=None, source="internal_spider", metadata={"video_url": "https://x.m3u8"},
    ))
    # source_factory 不调(已走 path ②)
    p.source_factory.get = MagicMock(return_value=None)

    fake_wav = tmp_path / "audio_raw" / "test.wav"
    with patch("vla.main_provider.extract_m3u8_audio") as fake_extract, \
         patch("vla.main_provider.extract_audio") as legacy_extract:
        fake_extract.side_effect = lambda url, p: fake_wav.write_bytes(b"RIFF")
        task = VideoTask(id="test", title="t", url="https://b-learning.bill-jc.com/x", expected_duration=3600)
        asset = await p.fetch_asset(task)

    fake_extract.assert_called_once()
    assert "https://x.m3u8" in fake_extract.call_args[0][0]
    legacy_extract.assert_not_called()  # 验证走的是 m3u8 路径, 不是 legacy
    assert asset is not None
    assert asset.source == "whisper_internal_download"
    assert asset.audio_path == fake_wav
    assert asset.deletable is True
```

- [ ] **Step 6: 跑全测确认绿 + 不 regression**

Run: `uv run pytest tests/test_extract_audio.py tests/test_fetch_asset.py -v`
Expected: 全部 passed,包括新加的 3 个测试

然后跑全 suite:
Run: `uv run pytest tests/ --ignore=tests/test_e2e.py -q`
Expected: 622 + 3 = 625 passed

- [ ] **Step 7: Commit**

```bash
git add src/vla/transcribe/extract.py src/vla/main_provider.py tests/test_extract_audio.py tests/test_fetch_asset.py
git commit -m "feat(provider): extract_m3u8_audio 流式抽音 + fetch_asset path ② 改造"
```

---

### Task 5: end-to-end spike 脚本 `spike_bill_jc_full.py`

**Files:**
- Create: `scripts/spike_bill_jc_full.py`(~250 行)
- 不需新测试(spike 是手工跑,无自动测试)

**Interfaces:**
- Consumes:
  - `InternalSiteSpider`(Task 1+2+3)
  - `build_text_provider`(已有)
  - `VideoLearningAgent._process_one`(已有)
  - `scripts/spike_f26_pipeline.py`(作为参考, Typer CLI pattern)
- Produces:
  - CLI 命令 `run` 接 3 模式: list-only / 端到端单视频 / 参数化

- [ ] **Step 1: 复制 `spike_f26_pipeline.py` 作脚手架**

```bash
uv run python scripts/spike_f26_pipeline.py --help
```
确认 typer + rich 输出正常。

- [ ] **Step 2: 写 spike 脚本**

```python
"""b-learning.bill-jc.com end-to-end spike — Phase 9.6 验收脚本。

3 种模式:
  1. --list-only: 爬目录树 + 视频列表, 打印 kng_id/title
  2. --kng-id <id>: 端到端跑单视频(爬 → 抽音 → 转写 → 质量门控)
  3. --kng-id + --resolution 360p: 走低分辨率档

前置: 用户手动启 Chrome debug:
  chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug

用法:
  uv run python scripts/spike_bill_jc_full.py \\
      --college-id <cid> --list-only --root-label "技术分享" --limit 5
  uv run python scripts/spike_bill_jc_full.py \\
      --college-id <cid> --kng-id <kng_id>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

from vla.config import CONFIG_FILE, load_config
from vla.main_provider import build_text_provider
from vla.models import VideoTask
from vla.subtitle.internal_site_spider import InternalSiteSpider

app = typer.Typer()


@app.command()
def run(
    kng_id: str | None = typer.Option(None, "--kng-id", help="单视频模式: 直接跑这个 kng_id"),
    list_only: bool = typer.Option(False, "--list-only", help="只跑 list_tasks 打印目录 + 视频"),
    root_label: str | None = typer.Option(None, "--root-label", help="list 模式: 限定根目录"),
    limit: int = typer.Option(10, "--limit", help="list 模式: 最多返回几条"),
    college_id: str = typer.Option(..., "--college-id"),
    cdp_url: str = typer.Option("http://localhost:9222", "--cdp-url"),
    resolution: str = typer.Option("720p", "--resolution"),
    config_path: Path = typer.Option(CONFIG_FILE, "--config"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    cfg = load_config(config_path)
    spider = InternalSiteSpider(cdp_url=cdp_url, college_id=college_id, resolution=resolution)

    if list_only:
        _run_list_mode(spider, root_label, limit, verbose)
        return

    if kng_id is None:
        typer.echo("ERROR: --kng-id 或 --list-only 至少一个", err=True)
        raise typer.Exit(1)

    _run_endtoend(cfg, spider, kng_id, verbose)


def _run_list_mode(spider: InternalSiteSpider, root_label: str | None, limit: int, verbose: bool) -> None:
    """模式 1: 爬目录树 + 视频列表, 打印 kng_id/title/url。"""
    typer.echo(f"[LIST] root_label={root_label!r} limit={limit}")
    try:
        tasks = asyncio.run(spider.list_tasks(root_label=root_label, limit=limit))
    except ValueError as e:
        typer.echo(f"[FAIL] {e}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"[FAIL] list_tasks 异常: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"[LIST] 找到 {len(tasks)} 条:")
    for t in tasks:
        typer.echo(f"  {t.id} | {t.title} | {t.url}")


def _run_endtoend(cfg, spider: InternalSiteSpider, kng_id: str, verbose: bool) -> None:
    """模式 2 + 3: 端到端跑单视频。"""
    fetch_asset, process_asset = build_text_provider(cfg)
    task = VideoTask(
        id=kng_id, title=f"bill-jc-{kng_id}",
        url=f"https://b-learning.bill-jc.com/learn/{kng_id}",
        expected_duration=3600,
    )

    typer.echo(f"[FETCH] kng_id={kng_id} resolution={spider.resolution}")
    asset = asyncio.run(fetch_asset(task))
    if asset is None:
        typer.echo("[FAIL] fetch_asset 返回 None — 检查 Chrome debug + cookie", err=True)
        raise typer.Exit(1)

    wav_size_mb = asset.audio_path.stat().st_size / 1e6 if asset.audio_path else 0
    typer.echo(f"[OK] Asset source={asset.source} wav={asset.audio_path} size={wav_size_mb:.1f}MB")

    typer.echo("[PROCESS] 转写 + 质量门控 ...")
    result = asyncio.run(process_asset(asset, task))
    if result is None:
        typer.echo("[FAIL] process_asset 返回 None", err=True)
        raise typer.Exit(1)

    typer.echo(f"[OK] Quality score={result.qr.score} passed={result.qr.passed}")
    typer.echo(f"[OK] Duration: {result.duration_sec}s")
    typer.echo(f"[DONE] 字幕落盘: {cfg.storage.transcribed_dir}/{kng_id}.cleaned.txt")


if __name__ == "__main__":
    app()
```

- [ ] **Step 3: 跑 `--help` 确认 CLI 不破**

Run: `uv run python scripts/spike_bill_jc_full.py --help`
Expected: 显示 3 个 options + 3 模式用法 hint

- [ ] **Step 4: 跑 `--list-only` 模式(用户手动启 Chrome debug 后)**

```bash
chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &
# 用户登录 b-learning.bill-jc.com
uv run python scripts/spike_bill_jc_full.py --college-id <cid> --list-only --root-label "技术分享" --limit 5
```
Expected: 打印 ≥ 1 条 kng_id | title | url

如果 0 条 / 失败 → 检查 Chrome debug 是否启 + cookie 是否拿到(看日志 "borrowed N yunxuetang cookies")

- [ ] **Step 5: 跑端到端模式(从 list 选 1 个 kng_id)**

```bash
uv run python scripts/spike_bill_jc_full.py --college-id <cid> --kng-id <kng_id>
```
Expected:
- `[OK] Asset source=whisper_internal_download wav=.../audio_raw/<id>.wav size=N.NMB`
- `[OK] Quality score=NN passed=True|False`
- `[DONE] 字幕落盘: ...`

- [ ] **Step 6: 磁盘检查**

```bash
ls -lh /tmp/audio_raw/<kng_id>.wav  # 应在, ~50MB
find /tmp -name "*.mp4" -newer spike  # 应空
df -h /tmp  # spike 完成前后对比, ~50MB 增量
```

- [ ] **Step 7: Commit**

```bash
git add scripts/spike_bill_jc_full.py
git commit -m "feat(spike): bill_jc_full end-to-end CLI 3-mode (list-only / 单视频 / 参数化)"
```

---

### Task 6: 文档收尾 + backlog §1 done + Phase 9.6 implementation-plan

**Files:**
- Modify: `docs/superpowers/backlog.md`(§1 标 done)
- Modify: `implementation-plan.md`(+ Phase 9.6 节)
- 不需新测试

**Interfaces:** 无

- [ ] **Step 1: 改 backlog.md**

`docs/superpowers/backlog.md` 找到 §1 "InternalSiteSpider 实装":

```markdown
## §1 InternalSiteSpider 实装

**状态**: ✅ **DONE** (Phase 9.6, 2026-09-09)

**PR commits**:
- InternalSiteSpider 完全实装(fetch_m3u8 + list_tasks + _borrow_cookies + _flatten_leaves)
- extract_m3u8_audio ffmpeg 流式抽音(不缓存 mp4)
- fetch_asset path ② 改造
- end-to-end spike 脚本 `scripts/spike_bill_jc_full.py` 3 模式

**实测验收**: [用户的真实 college_id + kng_id 跑通的 wav 大小 + 质量分数]

**剩余子项**:
- cookie 自动检测过期 / 自动重连(本轮 fail-fast + 提示用户启 Chrome debug)
- m3u8 token 缓存(本轮每次重新调, 不缓存, 符合"短时效"现实)
```

- [ ] **Step 2: 改 implementation-plan.md**

在 Phase 9.5 节之后追加 Phase 9.6:

```markdown
### Phase 9.6 — InternalSiteSpider 实装 + bill-jc end-to-end spike (2026-09-09)

**目标**: 公司视频源全链路跑通(爬 → 抽音 → 转写 → 质量门控)。

**SSOT**:
- 设计: `docs/superpowers/specs/2026-09-09-bill-jc-spider-impl-design.md`
- 计划: `docs/superpowers/plans/2026-09-09-bill-jc-spider-impl.md`

**6 个 task** (T1-T6, 见 plan):
1. T1: `_borrow_cookies` Playwright CDP
2. T2: `fetch_m3u8` preinit + kngPlay
3. T3: `list_tasks` tree + pagelist
4. T4: `extract_m3u8_audio` + fetch_asset path ②
5. T5: end-to-end spike `spike_bill_jc_full.py`
6. T6: 文档收尾(本 task)

**验收**:
- ✅ 单元测试 625 passed
- ✅ spike `--list-only` 打印 ≥1 条 video
- ✅ spike `--kng-id` 端到端跑通 wav + 字幕落盘
- ✅ 磁盘检查: tmp 不残留 mp4
```

- [ ] **Step 3: 跑全测 + doctor**

```bash
uv run pytest tests/ --ignore=tests/test_e2e.py -q
uv run vla doctor
```
Expected: 全 OK

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/backlog.md implementation-plan.md
git commit -m "docs(phase-9.6): backlog §1 done + implementation-plan Phase 9.6 节"
```

---

## Self-Review

**1. Spec coverage:**

| Spec 节 | Task |
|---|---|
| §3.1 InternalSiteSpider 接口 | T1 (`__init__` + `_borrow_cookies`) + T2 (`fetch_m3u8`) + T3 (`list_tasks`) ✓ |
| §3.2 `extract_m3u8_audio` 接口 | T4 Step 3 ✓ |
| §3.3 `fetch_asset` path ② 改动 | T4 Step 5 ✓ |
| §4.1 `internal_site_spider.py` 重写 | T1+T2+T3 ✓ |
| §4.2 `extract.py` 新函数 | T4 Step 3 ✓ |
| §4.3 `main_provider.py` 改 | T4 Step 5 ✓ |
| §4.4 `scripts/spike_bill_jc_full.py` 3 模式 | T5 ✓ |
| §4.5 12 个测试 | T1 (3) + T2 (3) + T3 (4) + T4 (3) = 13 个新增 ✓ |
| §5.2 验收(3 阶段) | T4 Step 6 + T5 Step 4-6 + T6 Step 3 ✓ |
| §6 文件清单 | 全覆盖 ✓ |
| §8 backlog §1 | T6 Step 1 ✓ |

**2. Placeholder scan:** 无 "TBD" / "TODO" / "类似 Task N"。所有 step 含代码块 + commit 命令。

**3. Type consistency:**
- `VideoTask(id: str, title: str, url: HttpUrl, expected_duration: int)` — T3 Step 3 + T5 Step 2 一致 ✓
- `extract_m3u8_audio(m3u8_url: str, output_path: Path)` — T4 全部一致 ✓
- `list_tasks(*, root_label=None, limit=10) -> list[VideoTask]` — T3 + T5 一致 ✓
- `fetch_m3u8(kng_id: str) -> str` — T2 + T5 一致 ✓

**4. Plan order**: T1 → T2 → T3 → T4 → T5 → T6(T1 cookie helper 先行, T4 需 T1+T3, T5 装 spike, T6 收尾)。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-09-09-bill-jc-spider-impl.md`. Two execution options:**

1. **Subagent-Driven (recommended)** - Fresh subagent per task (T1-T6), per-task reviewer, opus final whole-branch review。Phase 9.5 同样模式跑通 12 个 task + 2 个 unplanned fix; spec 风险更高(外网 API + cookie 借取), per-task review 必要。

2. **Inline Execution** - 同一会话内顺序跑 6 个 task, 期间 checkpoint 检查。

**Which approach?**

(建议: **Subagent-Driven** — 涉及 4 个外网 HTTP 调用 + cookie 借取,per-task reviewer 可在每个 API 调用接 mock 后 gate,减少跨上下文漂移)