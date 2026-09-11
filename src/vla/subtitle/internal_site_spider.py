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
import uuid
from typing import Any

import httpx
from playwright.async_api import async_playwright

from vla.models import VideoTask


logger = logging.getLogger(__name__)

# yunxuetang 域(大小写不敏感匹配 Playwright cookie["domain"],需 suffix 匹配防 evil-yunxuetang.cn.attacker.com)
_YUNXUETANG_DOMAINS = (".yunxuetang.cn", ".bill-jc.com")



def _is_yunxuetang_domain(domain: str) -> bool:
    """Suffix 匹配:domain 等于 ref 或以 .ref 结尾。

    防 `evil-yunxuetang.cn.attacker.com` 这种 substring 攻击。
    """
    d = domain.lower().lstrip(".")
    for ref in _YUNXUETANG_DOMAINS:
        ref_clean = ref.lstrip(".")
        if d == ref_clean or d.endswith("." + ref_clean):
            return True
    return False

# 3 个 API 端点
_TREE_URL = "https://api-phx-ali.yunxuetang.cn/kng/kngCatalog/student/tree"
_PAGELIST_URL = "https://api-phx-ali.yunxuetang.cn/kng/knowledge/pagelist"
_PREINIT_URL = "https://api-phx-ali.yunxuetang.cn/kng/study/submit/preinit"
_KNGPLAY_URL = "https://api-phx-ali.yunxuetang.cn/kng/study/kngPlay"

# 公共 header:含 Origin + Referer + yunxuetang SPA 自定义字段
# `source: 501` = PC web 客户端(501 是 yunxuetang 内部枚举值;移动 app 是其他)
# `yxt-orgdomain` / `x-yxt-product` = 浏览器 SDK 写死的固定值
_DEFAULT_HEADERS = {
    "Origin": "https://b-learning.bill-jc.com",
    "Referer": "https://b-learning.bill-jc.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "source": "501",
    "yxt-orgdomain": "b-learning.bill-jc.com",
    "x-yxt-product": "xxv2",
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
        self._token_cache: str = ""
        self._cookie_fetched_at: float = 0.0

    async def _borrow_cookies(self) -> list[dict[str, Any]]:
        """借 Chrome CDP session cookie, 仅保留 yunxuetang 域。

        缓存策略: 默认 30 min TTL; 过期时重新借。
        失败提示: Chrome debug 未启 → RuntimeError 含 hint。
        """
        now = time.monotonic()
        if self._cookie_cache is not None and (now - self._cookie_fetched_at) < self._cookie_ttl_sec:
            return self._cookie_cache

        cookies, _ = await self._fetch_cookies_and_token()
        self._cookie_cache = cookies
        self._cookie_fetched_at = now
        logger.info("borrowed %d yunxuetang cookies", len(cookies))
        return cookies

    async def _borrow_auth(self) -> tuple[list[dict[str, Any]], str]:
        """借 Chrome CDP cookies + localStorage token。

        yunxuetang API 身份校验用 `Authorization: Bearer <JWT>`,token 存在
        b-learning.bill-jc.com 域的 localStorage (key=`token`)而非 cookie。
        cookies 还是要带的(可能含其他 metadata / session)。

        缓存: 默认 30 min TTL, cookies 和 token 同步缓存。
        Returns: (yunxuetang_cookies, bearer_token)。token 可能为空字符串
        (如 Chrome debug 启了但用户没登录 b-learning.bill-jc.com)。
        """
        now = time.monotonic()
        if self._cookie_cache is not None and (now - self._cookie_fetched_at) < self._cookie_ttl_sec:
            return self._cookie_cache, self._token_cache

        cookies, token = await self._fetch_cookies_and_token()
        self._cookie_cache = cookies
        self._token_cache = token
        self._cookie_fetched_at = now
        logger.info("borrowed %d yunxuetang cookies + token (token_len=%d)", len(cookies), len(token))
        return cookies, token

    async def _fetch_cookies_and_token(self) -> tuple[list[dict[str, Any]], str]:
        """实际从 Chrome 借 cookies + 找 b-learning.bill-jc.com 页面读 localStorage token。"""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(self.cdp_url)
                context = browser.contexts[0]
                all_cookies = await context.cookies()

                # 借 localStorage token:遍历 page 找 bill-jc 域(否则 localStorage 隔离)
                token = ""
                for pg in context.pages:
                    if "bill-jc.com" in pg.url:
                        try:
                            token = await pg.evaluate(
                                "() => localStorage.getItem('token') || ''"
                            )
                        except Exception:
                            continue
                        if token:
                            break
        except Exception as e:
            raise RuntimeError(
                f"无法借 auth (检查 Chrome debug 是否启动: chrome --remote-debugging-port=9222): {e}"
            ) from e

        filtered = [c for c in all_cookies if _is_yunxuetang_domain(c.get("domain", ""))]
        return filtered, token or ""

    def _cookie_header(self, cookies: list[dict[str, Any]]) -> str:
        """把 cookie dict list 合并成 Cookie header 字符串。"""
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    def _auth_headers(self, cookies: list[dict[str, Any]], token: str) -> dict[str, str]:
        """构造 yunxuetang API 完整 headers:Origin/Referer + Cookie + token + yxtspanid。

        关键 header(从浏览器 Network 抓包验证):
        - `token: <JWT>` — yunxuetang API 身份校验,token 来自
          b-learning.bill-jc.com 页面 localStorage(`localStorage.getItem('token')`),
          不是 Cookie。
        - `yxtspanid: <12hex>` — yunxuetang 前端 SDK 动态生成(每次请求随机),
          外部调用方也随机生成一个即可。
        - Cookie header 也保留:服务端可能用 Cookie 做辅助校验。
        """
        h = {**_DEFAULT_HEADERS, "Cookie": self._cookie_header(cookies)}
        if token:
            h["token"] = token
        # 每次请求生成新的 yxtspanid(对应浏览器 SPA 行为)
        h["yxtspanid"] = uuid.uuid4().hex[:12]
        return h

    async def list_tasks(
        self,
        *,
        root_label: str | None = None,
        catalog_id: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[VideoTask]:
        """爬 tree + pagelist → VideoTask list(最多 limit 条)。

        Args:
            root_label: 限定根目录 label(如 "技术分享");None = 全树。
                与 catalog_id 互斥。
            catalog_id: 直接指定 catalogId(从 bill-jc 页面 URL 拿),
                跳过 tree API 直接 pagelist。当 catalog 树太大或已知目标时用。
            limit: 最多返回多少条 VideoTask。**同时是 pagelist 的页大小** ——
                翻页调用方必须用它作 offset 步长,否则每页会漏
                (page_size - limit) 条(见下)。
            offset: pagelist 起始偏移(默认 0)。仅 catalog_id 直通模式可用 ——
                tree 模式下有多个叶子,"offset 从哪儿算"没有明确语义,
                静默套到每个叶子上只会产出错乱结果(2026-09-10 FR-11)。

        Raises:
            ValueError: root_label 与 catalog_id 同时设置;root_label 不存在;
                offset > 0 但没给 catalog_id。
            RuntimeError: cookie 借取失败 / tree 或 pagelist HTTP 非 200。
        """
        if root_label is not None and catalog_id is not None:
            raise ValueError("root_label 与 catalog_id 互斥,只能设一个")
        if offset > 0 and catalog_id is None:
            raise ValueError(
                f"offset={offset} 只在 catalog_id 直通模式下有意义;"
                "tree/root_label 模式请用 limit 或改用 catalog_id"
            )

        cookies, token = await self._borrow_auth()
        headers = self._auth_headers(cookies, token)

        async with httpx.AsyncClient(timeout=30) as client:
            if catalog_id is not None:
                # 直通模式:跳过 tree,直接对给定 catalogId pagelist
                logger.info("list_tasks: 直接模式 catalog_id=%s", catalog_id)
                leaves = [{"id": catalog_id, "label": f"<catalog_id={catalog_id}>"}]
            else:
                # 树模式:tree → leaves → pagelist per leaf
                tree_resp = await client.post(
                    _TREE_URL,
                    json={"pmType": "0", "collegeId": self.college_id},
                    headers=headers,
                )
                if tree_resp.status_code != 200:
                    raise RuntimeError(
                        f"tree 失败 status={tree_resp.status_code}(cookie 可能过期): "
                        f"{tree_resp.text[:300]}"
                    )
                leaves = _flatten_leaves(tree_resp.json() or [], root_label=root_label)
                logger.info(
                    "catalog tree → %d 个非空叶子(root_label=%r)", len(leaves), root_label
                )

            # pagelist per leaf,累积到 limit 条
            tasks: list[VideoTask] = []
            for leaf in leaves:
                if len(tasks) >= limit:
                    break
                page_resp = await client.post(
                    _PAGELIST_URL,
                    # 页大小 == limit:下面按 limit 截断 tasks,若页大小写死 16,
                    # limit=10 时每页会静默丢弃 6 条。
                    params={"limit": limit, "offset": offset, "orderType": "desc", "orderBy": "createTime"},
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
                    logger.warning(
                        "pagelist catalog=%s 失败 status=%d,跳过该目录",
                        leaf["id"],
                        page_resp.status_code,
                    )
                    continue
                for v in page_resp.json().get("datas") or []:
                    if len(tasks) >= limit:
                        break
                    kng_id = v["id"]
                    tasks.append(
                        VideoTask(
                            id=kng_id,
                            title=v.get("title") or f"kng-{kng_id}",
                            url=f"https://b-learning.bill-jc.com/kng/#/video/play?kngId={kng_id}&projectId=&btid=&gwnlUrl=",
                            expected_duration=3600,
                        )
                    )

        return tasks

    async def fetch_m3u8(self, kng_id: str) -> str:
        """走 preinit + kngPlay, 返回 resolution 匹配的 m3u8 URL。

        Raises:
            RuntimeError: cookie 借取失败, preinit/kngPlay HTTP 非 200,
                          或 playDetails 为空。
        """
        cookies, token = await self._borrow_auth()
        headers = self._auth_headers(cookies, token)

        base_payload: dict[str, Any] = {
            "kngId": kng_id,
            "courseId": "",
            "studyParam": {"originOrgId": "", "previewType": 0},
            "targetCode": "kng",
            "targetId": "",
            "targetParam": {"taskId": "", "projectId": "", "flipId": "", "batchId": ""},
            "customFunctionCode": "",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            # 第 1 步:preinit(预热 study session)
            preinit_resp = await client.post(_PREINIT_URL, json=base_payload, headers=headers)
            if preinit_resp.status_code != 200:
                raise RuntimeError(
                    f"preinit 失败 status={preinit_resp.status_code}(cookie 可能过期): "
                    f"{preinit_resp.text[:300]}"
                )

            # 第 2 步:kngPlay(拿 m3u8 URL)
            kngplay_payload = {**base_payload, "fullname": "", "lang": ""}
            kngplay_resp = await client.post(_KNGPLAY_URL, json=kngplay_payload, headers=headers)
            if kngplay_resp.status_code != 200:
                raise RuntimeError(
                    f"kngPlay 失败 status={kngplay_resp.status_code}: {kngplay_resp.text[:300]}"
                )

            data = kngplay_resp.json()

        play_details = data.get("playDetails") or []
        if not play_details:
            raise RuntimeError(f"kngPlay 返回空 playDetails: {data}")

        # 选 desc == self.resolution;找不到 fallback 到第一档
        match = next((p for p in play_details if p.get("desc") == self.resolution), None)
        url = match["url"] if match else play_details[0]["url"]
        logger.info("fetch_m3u8 kng=%s resolution=%s → %s", kng_id, self.resolution, url)
        return url

    async def fetch_metadata(self, kng_id: str) -> dict[str, Any]:
        """走 preinit + kngPlay, 返回 dict(含 m3u8_url + fileId + resolution 等)。

        2026-09-10 真账号探勘结论:yunxuetang kngPlay API 顶层响应**不包含**
        视频业务元数据(title / duration / college_id),只返播放配置:
          - playDetails    — 多档 m3u8 URL(720p / 1080p / 480p / 360p)
          - fileId         — 服务端文件 ID
          - watermarkConfig — 水印
          - subtitlesFlag  — 是否有官方字幕(0 = 无,需走抽音)
          - timeCompleteStandard — 学习时长阈值

        因此 metadata 只覆盖"播放就绪"信息,**业务元数据需从其他途径拿**:
          - title: 从 list_tasks(pagelist)拿,但需要 college_id + catalog_id
          - duration_sec: 只能 ffprobe 抽音后拿(端到端路径覆盖)
          - college_id: 只能由用户告知

        返回字段语义:
          - kng_id      str   入参透传
          - m3u8_url    str   resolution 匹配的播放地址
          - resolution  str   选中的分辨率档(默认 720p)
          - fileId      str   服务端文件 ID(诊断用)
          - subtitles_flag int 0 = 服务端无字幕(需抽音转写);1 = 有官方字幕
          - all_resolutions list[str] 服务端提供的全部档位
          - title       None  kngPlay 不返业务字段,固定 None(用户从 URL 上下文补)
          - duration_sec None 同上
          - college_id  None 同上

        Raises:
            RuntimeError: cookie 借取失败, preinit/kngPlay HTTP 非 200,
                          或 playDetails 为空(同 fetch_m3u8)。
        """
        cookies, token = await self._borrow_auth()
        headers = self._auth_headers(cookies, token)

        base_payload: dict[str, Any] = {
            "kngId": kng_id,
            "courseId": "",
            "studyParam": {"originOrgId": "", "previewType": 0},
            "targetCode": "kng",
            "targetId": "",
            "targetParam": {"taskId": "", "projectId": "", "flipId": "", "batchId": ""},
            "customFunctionCode": "",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            # 第 1 步:preinit(预热 study session)
            preinit_resp = await client.post(_PREINIT_URL, json=base_payload, headers=headers)
            if preinit_resp.status_code != 200:
                raise RuntimeError(
                    f"preinit 失败 status={preinit_resp.status_code}(cookie 可能过期): "
                    f"{preinit_resp.text[:300]}"
                )

            # 第 2 步:kngPlay(拿 m3u8 URL + 播放配置)
            kngplay_payload = {**base_payload, "fullname": "", "lang": ""}
            kngplay_resp = await client.post(_KNGPLAY_URL, json=kngplay_payload, headers=headers)
            if kngplay_resp.status_code != 200:
                raise RuntimeError(
                    f"kngPlay 失败 status={kngplay_resp.status_code}: {kngplay_resp.text[:300]}"
                )

            data = kngplay_resp.json()

        play_details = data.get("playDetails") or []
        if not play_details:
            raise RuntimeError(f"kngPlay 返回空 playDetails: {data}")

        # 选 desc == self.resolution;找不到 fallback 到第一档
        match = next((p for p in play_details if p.get("desc") == self.resolution), None)
        m3u8_url = match["url"] if match else play_details[0]["url"]
        all_resolutions = [p.get("desc") for p in play_details if p.get("desc")]

        logger.info(
            "fetch_metadata kng=%s resolution=%s m3u8=%s fileId=%s subtitles=%s all=%s",
            kng_id, self.resolution, m3u8_url[:80],
            data.get("fileId"), data.get("subtitlesFlag"), all_resolutions,
        )
        return {
            "kng_id": kng_id,
            "m3u8_url": m3u8_url,
            "resolution": self.resolution,
            "fileId": data.get("fileId"),
            "subtitles_flag": data.get("subtitlesFlag"),
            "all_resolutions": all_resolutions,
            "title": None,
            "duration_sec": None,
            "college_id": None,
        }


def _flatten_leaves(
    tree: list[dict[str, Any]],
    *,
    root_label: str | None,
) -> list[dict[str, Any]]:
    """递归找 catalog 树里 kngCount > 0 的叶子节点(无 children 的节点)。

    root_label 非空 → 只返回该子树内的叶子;多个子树同名时取 DFS 首个匹配
    (确定性,spec §4.1);无匹配 → ValueError,让用户重选而非悄悄返空。
    """

    def _find_subtree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """DFS 找首个 label == root_label 的节点,返回 [该节点] 作为遍历范围。"""
        for node in nodes:
            if node.get("label") == root_label:
                return [node]
            found = _find_subtree(node.get("children") or [])
            if found is not None:
                return found
        return None

    if root_label is None:
        scope = tree
    else:
        matched = _find_subtree(tree)
        if matched is None:
            raise ValueError(f"root_label not found in catalog tree: {root_label!r}")
        scope = matched

    leaves: list[dict[str, Any]] = []

    def _walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            children = node.get("children") or []
            if children:
                _walk(children)
            elif node.get("kngCount", 0) > 0:
                leaves.append(node)

    _walk(scope)
    return leaves
