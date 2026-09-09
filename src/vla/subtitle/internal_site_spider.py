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

        filtered = [c for c in all_cookies if _is_yunxuetang_domain(c.get("domain", ""))]
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
        """爬 tree + pagelist → VideoTask list(最多 limit 条)。

        Args:
            root_label: 限定根目录 label(如 "技术分享");None = 全树。
            limit: 最多返回多少条 VideoTask。

        Raises:
            RuntimeError: cookie 借取失败或 tree HTTP 非 200。
            ValueError: root_label 在目录树里找不到。
        """
        cookies = await self._borrow_cookies()
        headers = {**_DEFAULT_HEADERS, "Cookie": self._cookie_header(cookies)}

        async with httpx.AsyncClient(timeout=30) as client:
            # 第 1 步:tree(拿 catalog 树)
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
            logger.info("catalog tree → %d 个非空叶子(root_label=%r)", len(leaves), root_label)

            # 第 2 步:对每个叶子 catalog 调 pagelist,累积到 limit 条
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
                            url=f"https://b-learning.bill-jc.com/learn/{kng_id}",
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
        cookies = await self._borrow_cookies()
        headers = {**_DEFAULT_HEADERS, "Cookie": self._cookie_header(cookies)}

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
