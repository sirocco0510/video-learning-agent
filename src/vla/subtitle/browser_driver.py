"""BrowserDriver(SSOT: requirements.md FR-2.4/2.5/2.6/2.7/2.8/2.10 + implementation-plan.md Phase 3.1)。

职责:
- 通过 `connect_over_cdp` 接管用户已打开的 Chrome(无需重复登录)
- `new_background_page()` 创建后台标签页(不抢焦点)
- `fetch_subtitle_via_browser()` 跑 4 种 JS 探测(track → initial_state → player → DOM)
- `_fetch_subtitle_text()` 用 `context.request` 跨 origin 下载字幕文本
  (绕过 CORS,使用浏览器已登录的 cookie)

设计:
- `browser_provider` 是注入点,默认走真实 playwright;测试时注入 mock。
- 4 种探测按优先级短路,track 命中就不跑后面的。
- JSON 字幕(B站 AI 风格 `{body: [{from, to, content}]}`)自动提取 content 拼接。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from vla.config import VLAConfig


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
        """从第一个 context 创建后台标签页(不抢焦点)。"""
        ctx = self._browser.contexts[0]
        return ctx.new_page()

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