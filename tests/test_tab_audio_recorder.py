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
