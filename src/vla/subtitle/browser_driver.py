"""BrowserDriver(SSOT: requirements.md FR-2.4/2.5/2.6/2.7/2.8/2.10 + implementation-plan.md Phase 3.1)。

职责:
- 通过 `connect_over_cdp` 接管用户已打开的 Chrome(无需重复登录)
- `new_background_page()` 创建后台标签页(不抢焦点)
  - 自动清理旧的扩展 popup(只留最新的一个)
- `fetch_subtitle_via_browser()` 跑 4 种 JS 探测(track → initial_state → player → DOM)
- `_fetch_subtitle_text()` 用 `context.request` 跨 origin 下载字幕文本
  (绕过 CORS,使用浏览器已登录的 cookie)

设计:
- `browser_provider` 是注入点,默认走真实 playwright;测试时注入 mock。
- 4 种探测按优先级短路,track 命中就不跑后面的。
- JSON 字幕(B站 AI 风格 `{body: [{from, to, content}]}`)自动提取 content 拼接。
- `cleanup_stale_extension_pages()` 是静态方法,spike 也可直接调用,无需 BrowserDriver 实例。

v3.2.1.3 (F2-6 spike):sync Playwright 全部 page 操作必须在 dispatcher fiber 所在
thread 上跑。从 main asyncio thread 直接调 page.bring_to_front / page.evaluate /
new_background_page 都会撞 "Cannot switch to a different thread"。

**修法**: driver 内部启动一个**专用后台 daemon thread** (`_driver_thread`),
playwright sync_playwright 在该 thread 启动 → dispatcher fiber 在该 thread 创建。
所有 page 操作通过 `_request_queue` 提交给该 thread 串行执行 → 永远同 thread,
永远不撞 greenlet。

**async vs sync**:
- sync 调用方:`driver.run_on_driver_thread(fn, *args)` — 内部 push 到 queue 后
  阻塞 `future.result()`。Sync strategy / page_control 等 legacy 代码用这个。
- async 调用方:`await driver.arun_on_driver_thread(fn, *args)` — 内部用
  `loop.run_in_executor(None, ...)` (default executor) 包装 future.result()。
  这样既不阻塞 asyncio loop,又保证 fn 在 driver thread 上跑(因为 future 是
  driver thread 设置的)。

**关键 invariant**:
- dispatcher fiber 在 `_driver_thread` 上创建
- 所有 page API 都在 `_driver_thread` 上调用 → 永远同 thread → 不撞 greenlet
- 即使 controller / strategy / capture 在不同 thread 调 driver API,driver 都
  把请求转发到 `_driver_thread`,Playwright 那边永远一致。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from concurrent.futures import Future
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
    """Puppeteer 通用驱动 + 4 种 JS 探测 + 跨 origin 下载。

    v3.2.1.3:所有 sync Playwright API 调用都在专用 daemon thread `_driver_thread`
    里跑,Playwright dispatcher fiber 在该 thread 创建。后续所有 page 操作通过
    `_request_queue` 提交给该 thread 串行执行 → 永远同 thread → 不撞 greenlet。

    v3.2.1.4:`_submit` 加重入保护(driver thread 上直接执行,不入队)+ 阻塞等待
    加超时。任何未来的自锁会以 TimeoutError 报错,而不是静默挂死。
    """

    # 单个 page 操作的上限。page.goto 自带 30s timeout,截图 ~1s,留足余量。
    # 主要目的是把"静默挂死"变成"可诊断的异常"。
    _SUBMIT_TIMEOUT_SEC = 120.0

    def __init__(
        self,
        config: VLAConfig,
        browser_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self._browser: Any = None
        # provider() -> playwright Browser;URL 由 driver 内部用 config.puppeteer.cdp_url() 拼
        self._provider: Callable[[], Any] = browser_provider or self._default_provider()
        # v3.2.1.3: 专用后台 daemon thread,所有 Playwright 调用都跑在这 thread
        self._driver_thread: threading.Thread | None = None
        self._request_queue: queue.Queue | None = None
        self._startup_error: Exception | None = None
        self._started = threading.Event()

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

    def _driver_loop(self) -> None:
        """专用后台 daemon thread 主循环。

        v3.2.1.3: 阻塞从 `_request_queue` 取请求,执行 fn,把结果 put 进 future。
        dispatcher fiber 在这 thread 创建(`_provider()` 第一次调时启 Playwright),
        所有 page API 都在这 thread 调 → 永远同 thread → 不撞 greenlet。

        启动时第一件事:调 `_provider()` 建立 Playwright connection。
        失败 → 把异常存到 `_startup_error`,线程退出。
        """
        try:
            self._browser = self._provider()
            # 确保 browser 实际存在才设置启动信号
            assert self._browser is not None, "Browser instance is None after provider"
            self._started.set()
            logger.info("✓ BrowserDriver 后台 thread 已启动,Playwright dispatcher 就绪")
        except Exception as e:
            self._startup_error = e
            self._started.set()
            logger.error("BrowserDriver 后台 thread 启动失败:%s", e)
            return

        assert self._request_queue is not None
        while True:
            item = self._request_queue.get()
            if item is None:  # shutdown sentinel
                self._request_queue.task_done()
                return
            fn, args, kwargs, future = item
            try:
                result = fn(*args, **kwargs)
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)
            finally:
                self._request_queue.task_done()

    def _ensure_thread(self) -> None:
        """确保 driver thread 已启动。connect() 内部 + 任何 submit 之前都会调。"""
        if self._driver_thread is not None:
            return
        self._request_queue = queue.Queue()
        self._driver_thread = threading.Thread(
            target=self._driver_loop,
            name="browser_driver",
            daemon=True,
        )
        self._driver_thread.start()

    def _submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Submit fn 到 driver thread,同步等结果。

        connect 之前调会 lazy 启动 thread,然后 provider 在 thread 内 connect。

        v3.2.1.4 **重入保护**:如果调用方已经在 `_driver_thread` 上,直接执行 fn,
        不入队。

        为什么必须这样:`_driver_loop` 是单线程串行消费者。若 driver thread 上正在
        执行的 fn 又往同一 queue 提交并 `future.result()` 阻塞,唯一能消费该 queue 的
        线程就是它自己 → 永久死锁。

        真实死锁链路(F2-6 spike 卡在 "Chrome Session 已连接" 之后):
            controller.phase_a_start_url
              → arun_on_driver_thread(driver.new_background_page)  # 第 1 层入队
                  → new_background_page 内部 _submit(_create)       # 第 2 层入队
                      → future.result() 等一个永远不会被消费的任务

        直接执行同样满足 greenlet invariant(仍在 `_driver_thread` 上),所以对
        Playwright sync API 是安全的。
        """
        self._ensure_thread()
        if threading.current_thread() is self._driver_thread:
            # 已在 driver thread → 直接跑,避免自锁(仍是同 thread,greenlet 安全)
            return fn(*args, **kwargs)
        assert self._request_queue is not None
        future: Future = Future()
        self._request_queue.put((fn, args, kwargs, future))
        return future.result(timeout=self._SUBMIT_TIMEOUT_SEC)

    def run_on_driver_thread(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """v3.2.1.3:sync API for external code to run sync Playwright ops on the driver thread.

        Use case: page_control.pause_page_video(page) 等 module-level helper,
        它们没有 driver 引用,但 page.evaluate 必须跑在 dispatcher thread。
        调用方传 driver 进来,driver.run_on_driver_thread(pause_page_video, page)。

        **WARNING**: 同步阻塞,future.result() 会阻塞当前线程直到 fn 在 driver thread 完成。
        在 async 上下文里调用会**阻塞 asyncio loop**,导致后续 await 卡死。
        async 调用方请用 `await driver.arun_on_driver_thread(...)`。
        """
        return self._submit(fn, *args, **kwargs)

    async def arun_on_driver_thread(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """v3.2.1.3:async API — submit fn 到 driver thread,await future 不阻塞 loop。

        内部用 `loop.run_in_executor(None, blocking_future_result, future)` —
        future.result() 的阻塞不会撞 asyncio loop(因为是在 default executor
        的 worker thread 上跑)。

        use case: controller.phase_a_start_url() / ScreenCapture.prepare_for_screenshot()
        等 async 函数。

        v3.2.1.4:走 `_submit` 而不是自己重复入队逻辑,这样重入保护 + 超时对
        async 路径同样生效(controller 的嵌套 submit 正是走这条路)。
        `_submit` 的阻塞发生在 executor worker 上,不阻塞 asyncio loop。
        """
        self._ensure_thread()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._submit(fn, *args, **kwargs)
        )

    # ---- 生命周期 ----

    def connect(self) -> Any:
        """连接到 Chrome CDP,保存 browser 实例,返回它。

        幂等:已连接则复用,避免重复 `sync_playwright().start()`。
        重复 start() 在 Playwright sync API 里会因 dispatcher loop 残留报
        "inside the asyncio loop" 错(F2-6 v3.2.1 root cause)。

        v3.2.1.3: connect() 启动专用后台 daemon thread,playwright sync_playwright
        在该 thread 内启动 → dispatcher fiber 在该 thread 创建。后续所有 page
        操作都通过 _submit 转发到该 thread,永远同 thread → 不撞 greenlet。
        """
        if self._browser is not None:
            return self._browser
        self._ensure_thread()
        # 等 thread 启动完成(provider connect 完毕或失败)
        assert self._driver_thread is not None
        # thread.start() 后 join 一小段等 startup 信号
        self._started.wait(timeout=30.0)
        if self._startup_error is not None:
            raise self._startup_error
        # 确保 browser 实际设置后才返回
        if self._browser is None:
            logger.error("BrowserDriver 初始化失败：browser 为 None 启动后")
            logger.error(f"启动事件状态: {self._started.is_set()}, 启动错误: {self._startup_error}")
            raise RuntimeError("BrowserDriver 初始化失败：browser 为 None 启动后")
        logger.info("✓ BrowserDriver.connect() 连接成功完成")
        return self._browser

    def disconnect(self) -> None:
        """断开 Chrome CDP 连接,清理资源。"""
        def _cleanup() -> None:
            if self._browser is not None:
                # 关闭所有 contexts
                for ctx in self._browser.contexts:
                    ctx.close()
                self._browser.close()
                self._browser = None
                logger.info("✓ BrowserDriver 已断开连接")

        if self._browser is not None:
            self._submit(_cleanup)

    async def targets(self) -> list[Any]:
        """v3.2.1.5: 列出所有 CDP 目标 — service_worker / page / iframe / 扩展等。

        用途: TabAudioRecorder.probe_status 用此枚举 `chrome-extension://` service_worker
        来定位扩展(不依赖 chrome.management API,该 API 在普通页面不可用)。

        实现: Playwright sync API 没暴露 `browser.targets()`,改走 Chrome 自己的
        HTTP debug 端点 `GET /json/list` —— 这是 CDP 标准接口,Chrome 自带。
        在 driver thread 跑(避免 asyncio 跨 thread 限速)。
        """
        if self._browser is None:
            return []

        def _fetch_targets() -> list[dict[str, Any]]:
            import json
            import urllib.request
            from urllib.parse import urlparse
            cdp = self._cdp_url()
            # cdp_url = http://127.0.0.1:9222,转成 ws 端的 http 端点
            parsed = urlparse(cdp)
            http_base = f"http://{parsed.hostname}:{parsed.port or 9222}"
            url = f"{http_base}/json/list"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data  # list of {type, url, ...}

        return await self.arun_on_driver_thread(_fetch_targets)

    def new_background_page(self) -> Any:
        """从第一个 context 创建后台标签页(不抢焦点)。

        先调用 `cleanup_stale_extension_pages` 关闭旧的 chrome-extension:// 标签页
        (只保留最新的一个),避免用户多次按 Cmd+Shift+R 时 popup 堆积。

        v3.2.1.3:整个调用包到 executor,确保与 dispatcher fiber 同 thread。
        """
        def _create() -> Any:
            # Chrome CDP connection might not have contexts initially
            if not self._browser.contexts:
                self._browser.new_context()
            ctx = self._browser.contexts[0]
            self.cleanup_stale_extension_pages(ctx, keep_latest=1)
            return ctx.new_page()
        return self._submit(_create)

    def find_page_by_url_substring(self, substring: str) -> Any | None:
        """在所有 context/pages 里找 URL 包含 substring 的 page(用于 F2-6 v3.2.1.8
        Phase A/C 复用用户已开 tab,不新建空白页)。

        Returns: 第一个匹配 page 或 None。
        """
        def _find() -> Any | None:
            if not self._browser or not self._browser.contexts:
                return None
            for ctx in self._browser.contexts:
                for page in ctx.pages:
                    url = getattr(page, "url", "") or ""
                    if substring in url:
                        return page
            return None
        return self._submit(_find)

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

        v3.2.1.3:整个 4-探测流程 + page.close 都在 executor worker 里跑,
        page.evaluate 调到 _sync() 时不会跨 thread → 不撞 greenlet。
        """
        def _run() -> tuple[str | None, dict | None]:
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

        return self._submit(_run)

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