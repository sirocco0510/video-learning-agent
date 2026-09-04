"""TabAudioRecorder 单元测试 (SSOT: spec §3.1, FR-2.21/2.24/2.24a/2.25)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from vla.subtitle.tab_audio_recorder import (
    DownloadTimeoutError,
    ExtensionNotFoundError,
    RecorderTriggerError,
    TabAudioRecorder,
)


# ---- Fixtures ----


class FakeExtension:
    """Mock chrome.management extension entry."""

    def __init__(self, name: str, ext_id: str, enabled: bool, description: str = "") -> None:
        self.name = name
        self.id = ext_id
        self.enabled = enabled
        self.description = description


class FakeBrowser:
    """Mock playwright Browser — only exposes what probe_status needs.

    probe_status uses `await browser.evaluate(...)` to call
    `chrome.management.getAll()` which returns a Promise. Tests
    inject pre-resolved values via `_get_all_result` (a coroutine
    that resolves to the list, or raises an exception).
    """

    def __init__(
        self,
        get_all_result: list[FakeExtension] | Exception,
        evaluate_delay: float = 0.0,
    ) -> None:
        self._get_all_result = get_all_result
        self._evaluate_delay = evaluate_delay

    async def evaluate(self, js: str) -> Any:
        if self._evaluate_delay:
            await asyncio.sleep(self._evaluate_delay)
        if isinstance(self._get_all_result, Exception):
            raise self._get_all_result
        return [
            {"name": e.name, "id": e.id, "enabled": e.enabled, "description": e.description}
            for e in self._get_all_result
        ]


@pytest.fixture
def recorder(tmp_path: Path) -> TabAudioRecorder:
    return TabAudioRecorder(
        match_keyword="tab audio",
        save_dir=tmp_path / "audio_raw",
        match_timeout_sec=5.0,
    )


# ---- probe_status tests (4) ----


class TestProbeStatus:
    async def test_probe_status_enabled(self, recorder: TabAudioRecorder) -> None:
        browser = FakeBrowser(
            get_all_result=[FakeExtension("Tab Audio Recorder", "ext123", True)]
        )
        result = await recorder.probe_status(browser)
        assert result == "enabled"

    async def test_probe_status_disabled(self, recorder: TabAudioRecorder) -> None:
        browser = FakeBrowser(
            get_all_result=[FakeExtension("Tab Audio Recorder", "ext123", False)]
        )
        result = await recorder.probe_status(browser)
        assert result == "disabled"

    async def test_probe_status_not_installed(self, recorder: TabAudioRecorder) -> None:
        browser = FakeBrowser(get_all_result=[])
        result = await recorder.probe_status(browser)
        assert result == "not_installed"

    async def test_probe_status_timeout_returns_not_installed(
        self, recorder: TabAudioRecorder
    ) -> None:
        # getAll raises (timeout / permission denied) → defensive fallback
        browser = FakeBrowser(get_all_result=RuntimeError("timeout"))
        result = await recorder.probe_status(browser)
        assert result == "not_installed"


# ---- _resolve_ext_id test (1) ----


class TestResolveExtId:
    async def test_resolve_ext_id_matches_keyword_case_insensitive(
        self, recorder: TabAudioRecorder
    ) -> None:
        # Match against name (case-insensitive)
        browser = FakeBrowser(
            get_all_result=[
                FakeExtension("Some Other Extension", "other456", True),
                FakeExtension("TAB AUDIO CAPTURE PRO", "tab999", True),
            ]
        )
        ext_id = await recorder._resolve_ext_id(browser)
        assert ext_id == "tab999"

    async def test_resolve_ext_id_not_found_raises(self, recorder: TabAudioRecorder) -> None:
        browser = FakeBrowser(
            get_all_result=[FakeExtension("Random Extension", "rand111", True)]
        )
        with pytest.raises(ExtensionNotFoundError):
            await recorder._resolve_ext_id(browser)


# ---- start_recording test (1) ----


class FakeBackgroundPage:
    """Mock 扩展 background page,url 状态可手动推进以测试 polling。"""

    def __init__(self, url_sequence: list[str], evaluate_js: str = "undefined") -> None:
        self._url_sequence = url_sequence
        self._index = 0
        self._evaluate_js = evaluate_js
        self.evaluate_calls: list[str] = []

    @property
    def url(self) -> str:
        if self._index < len(self._url_sequence):
            return self._url_sequence[self._index]
        return self._url_sequence[-1]

    def advance(self) -> None:
        if self._index < len(self._url_sequence) - 1:
            self._index += 1

    async def evaluate(self, js: str) -> Any:
        self.evaluate_calls.append(js)
        return self._evaluate_js


class FakeDriver:
    """Mock playwright Driver — 暴露 start_recording / click_download 需要的 hooks。

    模拟 _resolve_ext_id 路径:driver.evaluate() 解析 _PROBE_GET_ALL_JS,
    返回 chrome.management.getAll() 等价值。
    """

    def __init__(
        self,
        bg_page: FakeBackgroundPage | None = None,
        targets: list[Any] | None = None,
        pages_for_goto: dict[str, FakeBackgroundPage] | None = None,
        get_all_result: list[FakeExtension] | Exception | None = None,
        bg_url_override: str | None = None,
    ) -> None:
        self.bg_page = bg_page
        self.targets_list = targets or []
        self.pages_for_goto = pages_for_goto or {}
        self.new_page_calls: list[str] = []
        self.goto_calls: list[str] = []
        self._get_all_result = get_all_result if get_all_result is not None else []
        self._bg_url_override = bg_url_override
        self.evaluate_calls: list[str] = []

    async def targets(self) -> list[Any]:
        return self.targets_list

    async def evaluate(self, js: str) -> Any:
        # 用于 _resolve_ext_id:解析 chrome.management.getAll() JS,返回扩展列表
        self.evaluate_calls.append(js)
        if isinstance(self._get_all_result, Exception):
            raise self._get_all_result
        return [
            {"name": e.name, "id": e.id, "enabled": e.enabled, "description": e.description}
            for e in self._get_all_result
        ]

    def new_page(self) -> Any:
        # 返回一个 capture 用的 mock,实际跳转由 goto 单独处理
        mock = type("M", (), {})()
        return mock


class TestStartRecording:
    async def test_start_recording_polls_url_and_extracts_audio_id(
        self, recorder: TabAudioRecorder
    ) -> None:
        # start_recording 的核心: _resolve_ext_id → 找到 bg page → 启动录制
        # → 轮询 bg_page.url 直到 editor.html?id=<audio_id>。
        # 用 FakeBackgroundPage 模拟 url 变化:
        #   第 1 次: chrome-extension://ext123/_generated_background_page.html  (录制中)
        #   第 2 次: chrome-extension://ext123/editor.html?id=4242  (录制完成)
        bg = FakeBackgroundPage(
            url_sequence=[
                "chrome-extension://ext123/_generated_background_page.html",
                "chrome-extension://ext123/editor.html?id=4242",
            ],
            evaluate_js="undefined",
        )
        # start_recording 第一步 _resolve_ext_id(driver) → driver.evaluate(...)
        # 然后在 bg page 上 evaluate("startTabRecording()") → 我们让 bg.advance()
        # 在 evaluate 后推进一次。

        original_evaluate = bg.evaluate

        async def evaluate_then_advance(js: str) -> Any:
            result = await original_evaluate(js)
            bg.advance()
            return result

        bg.evaluate = evaluate_then_advance  # type: ignore[assignment]

        # FakeDriver 同时支持 evaluate (给 _resolve_ext_id) 和 targets() (给 bg page 查找)
        driver = FakeDriver(
            targets=[bg],
            get_all_result=[FakeExtension("Tab Audio Recorder", "ext123", True)],
        )

        audio_id = await recorder.start_recording(driver, "https://example.com/video", 60)
        assert audio_id == "4242"
        assert bg.evaluate_calls, "start_recording 应该至少调用一次 evaluate 启动录制"

    async def test_start_recording_no_bg_page_raises(
        self, recorder: TabAudioRecorder
    ) -> None:
        # driver.targets() 返回 [] → _resolve_ext_id 之后找不到 bg page → RecorderTriggerError
        driver = FakeDriver(
            targets=[],
            get_all_result=[FakeExtension("Tab Audio Recorder", "ext123", True)],
        )

        with pytest.raises(RecorderTriggerError):
            await recorder.start_recording(
                driver, "https://example.com/video", 60
            )
        # 关键: _resolve_ext_id 必须先调用(evaluate 非空),
        # 而不是直接走 bg page 查找(spec §3.1 line 113)。
        assert driver.evaluate_calls, (
            "start_recording 必须在找 bg page 之前先调用 _resolve_ext_id(driver)"
        )

    async def test_start_recording_poll_timeout_raises(
        self, recorder: TabAudioRecorder
    ) -> None:
        # URL 永远停在 _generated_background_page.html,从不跳到 editor.html?id=
        # 用 duration_sec=1, post_buffer_sec=0 让测试尽快失败
        bg = FakeBackgroundPage(
            url_sequence=[
                "chrome-extension://ext123/_generated_background_page.html",
            ],
            evaluate_js="undefined",
        )
        driver = FakeDriver(
            targets=[bg],
            get_all_result=[FakeExtension("Tab Audio Recorder", "ext123", True)],
        )

        with pytest.raises(RecorderTriggerError):
            await recorder.start_recording(
                driver, "https://example.com/video", duration_sec=1, post_buffer_sec=0
            )
        # 必须先调 _resolve_ext_id,再走 polling 超时路径
        assert driver.evaluate_calls, (
            "start_recording 必须在 polling 之前先调用 _resolve_ext_id(driver)"
        )


# ---- click_download tests (2) ----


class FakeDownload:
    """Mock playwright Download 对象。"""

    def __init__(self, suggested_filename: str, save_path: Path) -> None:
        self.suggested_filename = suggested_filename
        self._save_path = save_path
        self.save_as_calls: list[Path] = []

    async def save_as(self, path: Path) -> None:
        self.save_as_calls.append(path)
        # 模拟文件落盘
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-webm-bytes")


class FakeDownloadPage:
    """Mock 一个可以触发 download 事件的 page。"""

    def __init__(
        self,
        download: FakeDownload | None,
        click_should_trigger: bool = True,
        click_delay: float = 0.0,
    ) -> None:
        self._download = download
        self._click_should_trigger = click_should_trigger
        self._click_delay = click_delay
        self.url = "chrome-extension://ext123/editor.html?id=99"
        self.goto_calls: list[str] = []
        self.click_calls: list[str] = []
        self._download_emitted = False

    async def goto(self, url: str) -> None:
        self.goto_calls.append(url)

    async def click(self, selector: str, **kwargs: Any) -> None:
        self.click_calls.append(selector)
        if self._click_delay:
            await asyncio.sleep(self._click_delay)
        if self._click_should_trigger and self._download and not self._download_emitted:
            self._download_emitted = True
            # 模拟 download 事件被 ctx._download_handler 捕获
            # 由 FakeContext.emit_download 触发,这里只标状态

    @property
    def download(self) -> FakeDownload | None:
        return self._download if self._download_emitted else None


class FakeContext:
    """Mock BrowserContext,管理 download handler 注册 + 新页面创建。"""

    def __init__(self, page: FakeDownloadPage) -> None:
        self.page = page
        self._download_handler: Any = None
        self.handlers: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        if event == "download":
            self._download_handler = handler
            self.handlers.append(handler)

    def new_page(self) -> FakeDownloadPage:
        return self.page

    def emit_download(self, dl: FakeDownload) -> None:
        """手动触发 download 事件(模拟扩展点完按钮后产生的事件)。

        handler 实际是 sync(只是 set_result),所以直接同步调用。
        brief 原稿用了 asyncio.create_task(handler(dl)),但 sync handler 返回
        None,create_task(None) 会 TypeError;这里改成同步调用。
        """
        if self._download_handler is not None:
            self._download_handler(dl)


class FakeDriverWithContext:
    """带 context 的 FakeDriver,用于 click_download 测试。"""

    def __init__(self, context: FakeContext, page: FakeDownloadPage) -> None:
        self.context = context
        self.page = page


class TestClickDownload:
    async def test_click_download_registers_listener_before_click(
        self, tmp_path: Path, recorder: TabAudioRecorder
    ) -> None:
        # 验证: context.on("download") 必须在 click() 之前注册
        # 用 ordered_calls 列表记录两件事的先后顺序
        ordered_calls: list[str] = []

        class TrackingContext(FakeContext):
            def on(self, event: str, handler: Any) -> None:
                ordered_calls.append(f"on:{event}")
                super().on(event, handler)

        class TrackingPage(FakeDownloadPage):
            async def click(self, selector: str, **kwargs: Any) -> None:
                ordered_calls.append(f"click:{selector}")
                await super().click(selector, **kwargs)

        dl = FakeDownload("audio.webm", tmp_path / "audio.webm")
        page = TrackingPage(download=dl, click_should_trigger=False)
        # click 不立即 emit,让测试自己 emit,确保 on 在 click 之前注册
        ctx = TrackingContext(page=page)
        driver = FakeDriverWithContext(ctx, page)

        # 后台 emit download(模拟扩展点完按钮后产生的事件)
        async def emit_later() -> None:
            await asyncio.sleep(0.05)
            ctx.emit_download(dl)

        emit_task = asyncio.create_task(emit_later())

        result_path = await recorder.click_download(
            driver,  # type: ignore[arg-type]
            audio_id="99",
            ext_id="ext123",
            save_dir=tmp_path,
            timeout_sec=5,
        )

        await emit_task

        assert result_path.exists()
        assert result_path.name == "99.webm"  # audio_id 作为文件名(FR-2.26)
        # 关键断言: on("download") 必须在 click() 之前
        assert ordered_calls[0].startswith("on:"), (
            f"download listener must be registered before click, got: {ordered_calls}"
        )
        assert any(c.startswith("click:") for c in ordered_calls)

    async def test_click_download_timeout_raises(
        self, tmp_path: Path, recorder: TabAudioRecorder
    ) -> None:
        # click 永远不触发 download 事件 → timeout 抛 DownloadTimeoutError
        page = FakeDownloadPage(download=None, click_should_trigger=False)
        ctx = FakeContext(page=page)
        driver = FakeDriverWithContext(ctx, page)

        with pytest.raises(DownloadTimeoutError):
            await recorder.click_download(
                driver,  # type: ignore[arg-type]
                audio_id="99",
                ext_id="ext123",
                save_dir=tmp_path,
                timeout_sec=1,  # 缩短 timeout 让测试快
            )
