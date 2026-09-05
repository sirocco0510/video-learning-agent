"""BrowserDriver(SSOT: requirements.md FR-2.4/2.5/2.6/2.7/2.8/2.10 + implementation-plan.md Phase 3.1)。

职责:
- 通过 `connect_over_cdp` 接管用户已打开的 Chrome(无需重复登录)
- `new_background_page()` 创建后台标签页(不抢焦点)
  - 自动清理旧的扩展 popup(只留最新的一个)— 用户多次按 Cmd+Shift+R 不会堆积
- `fetch_subtitle_via_browser()` 跑 4 种 JS 探测(track → initial_state → player → DOM)
- `_fetch_subtitle_text()` 用 `context.request` 跨 origin 下载字幕文本
  (绕过 CORS,使用浏览器已登录的 cookie)

设计:
- `browser_provider` 是注入点,默认走真实 playwright;测试时注入 mock。
- 4 种探测按优先级短路,track 命中就不跑后面的。
- JSON 字幕(B站 AI 风格 `{body: [{from, to, content}]}`)自动提取 content 拼接。
- `cleanup_stale_extension_pages()` 是静态方法,spike 也可直接调用,无需 BrowserDriver 实例。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from vla.config import VLAConfig


logger = logging.getLogger(__name__)


# ---- JSON 字幕的 content 拼接 ----


def _extract_json_subtitle(body: Any) -> str | None:
    """B站 AI 字幕 JSON 格式:{body: [{from, to, content}, ...]} → 提取 content 拼接。

    其他结构 → 原样返回 None(让调用方 fallback 到 raw text)。
    """
    if isinstance(body, dict) and isinstance(body.get("body"), list):
        parts = []
        for item in body["body"]:
            if isinstance(item, dict) and "content" in item:
                parts.append(str(item["content"]))
        if parts:
            return "".join(parts)
    return None


class BrowserDriver:
    """Puppeteer 通用驱动 + 4 种 JS 探测 + 跨 origin 下载。"""

    def __init__(
        self,
        config: VLAConfig,
        browser_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self._browser: Any = None
        # provider() -> playwright Browser;URL 由 driver 内部用 config.puppeteer.cdp_url() 拼
        self._provider: Callable[[], Any] = browser_provider or self._default_provider()

    def _default_provider(self) -> Callable[[], Any]:
        def provider() -> Any:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            return pw.chromium.connect_over_cdp(self._cdp_url())

        return provider

    def set_browser_provider(self, provider: Callable[[], Any]) -> None:
        """注入 mock provider(测试用)。"""
        self._provider = provider

    def _cdp_url(self) -> str:
        return self.config.puppeteer.cdp_url()

    # ---- 生命周期 ----

    def connect(self) -> Any:
        """连接到 Chrome CDP,保存 browser 实例,返回它。"""
        self._browser = self._provider()
        return self._browser

    def new_background_page(self) -> Any:
        """从第一个 context 创建后台标签页(不抢焦点)。

        先调用 `cleanup_stale_extension_pages` 关闭旧的 chrome-extension:// 标签页
        (只保留最新的一个),避免用户多次按 Cmd+Shift+R 时 popup 堆积。
        """
        ctx = self._browser.contexts[0]
        self.cleanup_stale_extension_pages(ctx, keep_latest=1)
        return ctx.new_page()

    @staticmethod
    def cleanup_stale_extension_pages(ctx: Any, keep_latest: int = 1) -> int:
        """关闭除最新 `keep_latest` 个之外的所有 `chrome-extension://` 标签页。

        用途: Screen Recorder 扩展每次按 Cmd+Shift+R 都开一个 popup,
        多次误触会让 Chrome 标签栏堆满 `chrome-extension://.../popup.html`。
        只保留最新的那一个,其余关闭,避免 tab 堆积。

        Args:
            ctx: playwright BrowserContext(或 mock,需要有 `.pages` 属性)
            keep_latest: 保留最新的多少个扩展页(默认 1)

        Returns:
            实际关闭的页面数。close() 抛错不阻塞,只 log debug。
        """
        pages_attr = getattr(ctx, "pages", None)
        if pages_attr is None:
            return 0
        try:
            pages = list(pages_attr)
        except TypeError:
            return 0

        ext_pages = [p for p in pages if (getattr(p, "url", "") or "").startswith("chrome-extension://")]
        if len(ext_pages) <= keep_latest:
            return 0

        to_close = ext_pages[:-keep_latest] if keep_latest > 0 else ext_pages
        closed = 0
        for p in to_close:
            try:
                p.close()
                closed += 1
            except Exception as e:
                logger.debug("关闭扩展页失败 %s: %s", getattr(p, "url", "?"), e)
        if closed:
            logger.info("🧹 关闭 %d 个旧扩展 popup(保留最新 %d 个)", closed, keep_latest)
        return closed

    # ---- 4 种 JS 探测 ----

    def fetch_subtitle_via_browser(
        self, page: Any, url: str
    ) -> tuple[str | None, dict | None]:
        """按优先级跑 4 种探测,首个命中即返回;全 miss 返回 (None, None)。

        完成后关闭 page(无论命中与否)。
        """
        try:
            # 1. <track> 标签
            text, meta = self._probe_track(page, url)
            if text:
                return text, meta

            # 2. window.__INITIAL_STATE__
            text, meta = self._probe_initial_state(page, url)
            if text:
                return text, meta

            # 3. window.player.getSubtitle()
            text, meta = self._probe_player(page, url)
            if text:
                return text, meta

            # 4. DOM 扫描常见字幕容器
            text, meta = self._probe_dom(page, url)
            if text:
                return text, meta

            return None, None
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _probe_track(
        self, page: Any, url: str
    ) -> tuple[str | None, dict | None]:
        """探测 <track> 标签的 src / srclang。"""
        result = page.evaluate(
            "() => { const t = document.querySelector('track'); return t ? {src: t.src, lang: t.srclang} : null; }"
        )
        if not result or not isinstance(result, dict) or not result.get("src"):
            return None, None
        text = self._fetch_subtitle_text(page, result["src"])
        if not text:
            return None, None
        return text, {"method": "track", "lang": result.get("lang", "")}

    def _probe_initial_state(
        self, page: Any, url: str
    ) -> tuple[str | None, dict | None]:
        """探测 window.__INITIAL_STATE__ 中的字幕 URL。

        简单方案: 在序列化字符串里搜 http(s)://...subtitle|...patterns/v1/...
        """
        sub_url = page.evaluate(
            """() => {
                const s = window.__INITIAL_STATE__;
                if (!s) return null;
                try {
                    const blob = JSON.stringify(s);
                    const m = blob.match(/https?:\\/\\/[\\w.-]+\\/[^\"\\\\]*?(?:subtitle|subtitles)[^\"\\\\]*/i);
                    return m ? m[0] : null;
                } catch(e) { return null; }
            }"""
        )
        if not sub_url or not isinstance(sub_url, str):
            return None, None
        text = self._fetch_subtitle_text(page, sub_url)
        if not text:
            return None, None
        return text, {"method": "initial_state"}

    def _probe_player(
        self, page: Any, url: str
    ) -> tuple[str | None, dict | None]:
        """探测 window.player.getSubtitle()(返回文本)。"""
        result = page.evaluate(
            """() => {
                try {
                    const p = window.player;
                    if (!p || typeof p.getSubtitle !== 'function') return null;
                    const s = p.getSubtitle();
                    return s ? String(s) : null;
                } catch(e) { return null; }
            }"""
        )
        if not result or not isinstance(result, str):
            return None, None
        return result, {"method": "player_object"}

    def _probe_dom(
        self, page: Any, url: str
    ) -> tuple[str | None, dict | None]:
        """扫描 DOM 常见字幕容器。"""
        result = page.evaluate(
            """() => {
                const selectors = [
                    '.bilibili-player-video-subtitle',
                    '.subtitle', '.caption',
                    '[class*="subtitle"]', '[class*="caption"]'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText && el.innerText.trim()) {
                        return el.innerText.trim();
                    }
                }
                return null;
            }"""
        )
        if not result or not isinstance(result, str):
            return None, None
        return result, {"method": "dom_selector"}

    # ---- 跨 origin 下载 ----

    def _fetch_subtitle_text(self, page: Any, url: str) -> str | None:
        """跨 origin 拿字幕文本(content.request.get 用浏览器 cookie,绕过 CORS)。

        - protocol-relative URL `//cdn/...` 自动补 `https:`
        - JSON 字幕自动提取 content(B站 AI 字幕格式)
        - 404 / 异常 / 空 → 返回 None
        """
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url
        try:
            resp = page.context.request.get(url)
        except Exception:
            return None
        if resp.status != 200:
            return None
        try:
            body = resp.text()
        except Exception:
            return None
        if not body:
            return None
        # 尝试解析 JSON(B站 AI 字幕风格)
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" in ct:
            try:
                data = resp.json()
                extracted = _extract_json_subtitle(data)
                if extracted is not None:
                    return extracted
            except Exception:
                pass
        return body