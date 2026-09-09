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
        # 实装见 Task 3
        raise NotImplementedError("实装见 Task 3")

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
