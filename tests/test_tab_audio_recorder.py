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
    """Mock playwright Driver — 暴露 start_recording / click_download 需要的 hooks。"""

    def __init__(
        self,
        bg_page: FakeBackgroundPage | None = None,
        targets: list[Any] | None = None,
        pages_for_goto: dict[str, FakeBackgroundPage] | None = None,
    ) -> None:
        self.bg_page = bg_page
        self.targets_list = targets or []
        self.pages_for_goto = pages_for_goto or {}
        self.new_page_calls: list[str] = []
        self.goto_calls: list[str] = []

    async def targets(self) -> list[Any]:
        return self.targets_list

    def new_page(self) -> Any:
        # 返回一个 capture 用的 mock,实际跳转由 goto 单独处理
        mock = type("M", (), {})()
        return mock


class TestStartRecording:
    async def test_start_recording_polls_url_and_extracts_audio_id(
        self, recorder: TabAudioRecorder
    ) -> None:
        # start_recording 的核心: 启动录制后,扩展跳转 editor.html?id=<audio_id>,
        # 我们轮询 bg_page.url 提取 audio_id。
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
        # start_recording 第一步会 evaluate("startTabRecording()" 或等价 JS),
        # 然后轮询 url;我们让 bg.advance() 在 evaluate 后推进一次。

        original_evaluate = bg.evaluate

        async def evaluate_then_advance(js: str) -> Any:
            result = await original_evaluate(js)
            bg.advance()
            return result

        bg.evaluate = evaluate_then_advance  # type: ignore[assignment]

        # start_recording 需要 driver.targets() 返回包含 bg page 的列表
        driver = FakeDriver(targets=[bg])

        # 简化版 start_recording 会:
        #   1. 遍历 driver.targets() 找到 type='background_page' or url 含 _generated_background_page.html
        #   2. evaluate 启动录制
        #   3. 轮询 url 直到匹配 editor.html?id=(\d+)
        # 测试只需要验证 audio_id 提取正确;内部如何找 bg page 由实现决定。
        # 这里我们直接在 FakeDriver 上挂一个辅助方法(供实现 hook):
        driver.bg_page = bg  # type: ignore[attr-defined]

        audio_id = await recorder.start_recording(driver, "https://example.com/video", 60)
        assert audio_id == "4242"
        assert bg.evaluate_calls, "start_recording 应该至少调用一次 evaluate 启动录制"
