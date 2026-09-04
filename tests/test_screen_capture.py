"""Tests for ScreenCapture base (FR-2.28 part 1, plan F2-4).

Scope: macOS/Windows subprocess dispatch + failure semantics + JSONL audit writer.
NOT covered here (deferred to F2-5): 4-phase trigger, notifier integration,
PHASE A→C page.evaluate sequence, doctor pre-warm.

Tests mock `asyncio.create_subprocess_exec` — no real `screencapture` invocation.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vla.capture.screen_capture import ScreenCapture, ScreenshotIndexEntry


@pytest.fixture
def capture_macos(tmp_path: Path) -> ScreenCapture:
    return ScreenCapture(save_dir=tmp_path, platform_name="Darwin")


@pytest.fixture
def capture_windows(tmp_path: Path) -> ScreenCapture:
    return ScreenCapture(save_dir=tmp_path, platform_name="Windows")


def _make_proc(returncode: int = 0, stderr: bytes = b"") -> AsyncMock:
    """Mock an asyncio.subprocess.Process with .communicate() returning (b'', stderr)."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


class TestCaptureMacOS:
    async def test_capture_macos_uses_screencapture_x(
        self, capture_macos: ScreenCapture, tmp_path: Path
    ):
        """macOS path must invoke `screencapture -x <path>` and return True on success."""
        save_path = tmp_path / "shot.png"
        # Pre-create the file to mimic screencapture's behaviour (subprocess writes the file)
        # and so save_path.exists() returns True. We verify the command, not the side effect.
        save_path.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header

        with patch(
            "vla.capture.screen_capture.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_make_proc(returncode=0)),
        ) as mock_exec:
            result = await capture_macos.capture_full_screen(save_path)

        assert result is True
        # Verify screencapture binary + -x flag + save_path passed
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        assert call_args.args[0] == "screencapture"
        assert call_args.args[1] == "-x"
        assert call_args.args[2] == str(save_path)


class TestCaptureWindows:
    async def test_capture_windows_uses_powershell(
        self, capture_windows: ScreenCapture, tmp_path: Path
    ):
        """Windows path must invoke PowerShell with System.Drawing + Forms scripts."""
        save_path = tmp_path / "shot.png"
        save_path.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch(
            "vla.capture.screen_capture.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_make_proc(returncode=0)),
        ) as mock_exec:
            result = await capture_windows.capture_full_screen(save_path)

        assert result is True
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        assert call_args.args[0] == "powershell"
        assert call_args.args[1] == "-Command"
        ps_script = call_args.args[2]
        assert "System.Drawing" in ps_script
        assert "System.Windows.Forms" in ps_script
        assert str(save_path) in ps_script


class TestCaptureFailureSemantics:
    async def test_capture_returns_false_on_subprocess_failure(
        self, capture_macos: ScreenCapture, tmp_path: Path
    ):
        """screencapture exit code != 0 → returns False (TCC deny / binary missing / etc.)."""
        save_path = tmp_path / "shot.png"
        # File does NOT exist — simulates permission denied / TCC block

        with patch(
            "vla.capture.screen_capture.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_make_proc(returncode=1, stderr=b"permission denied")),
        ):
            result = await capture_macos.capture_full_screen(save_path)

        assert result is False

    async def test_capture_returns_false_on_timeout(
        self, capture_macos: ScreenCapture, tmp_path: Path
    ):
        """asyncio.TimeoutError (subprocess hangs > 10s) → returns False."""
        save_path = tmp_path / "shot.png"

        async def _hang(*args, **kwargs):
            raise asyncio.TimeoutError("subprocess timeout")

        with patch(
            "vla.capture.screen_capture.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=_hang),
        ):
            result = await capture_macos.capture_full_screen(save_path)

        assert result is False

    async def test_capture_returns_false_on_unsupported_platform(
        self, tmp_path: Path
    ):
        """Linux / unknown platform → False (no exception)."""
        capture_linux = ScreenCapture(save_dir=tmp_path, platform_name="Linux")
        save_path = tmp_path / "shot.png"
        result = await capture_linux.capture_full_screen(save_path)
        assert result is False


class TestPrepareForScreenshot:
    async def test_prepare_for_screenshot_calls_full_sequence(
        self, capture_macos: ScreenCapture
    ):
        """bring_to_front → focus → moveTo/resizeTo → sleep(0.3), in order."""
        page = AsyncMock()
        await capture_macos.prepare_for_screenshot(page)

        page.bring_to_front.assert_awaited_once()
        # Two evaluate calls: focus + moveTo/resizeTo
        assert page.evaluate.await_count == 2
        first_eval = page.evaluate.await_args_list[0]
        assert first_eval.args[0] == "window.focus()"
        second_eval = page.evaluate.await_args_list[1]
        assert "window.moveTo(0, 0)" in second_eval.args[0]
        assert "window.resizeTo(screen.width, screen.height)" in second_eval.args[0]

    async def test_prepare_for_screenshot_swallows_errors(
        self, capture_macos: ScreenCapture
    ):
        """Any page.evaluate failure → logged warning, no exception raised."""
        page = AsyncMock()
        page.bring_to_front.side_effect = RuntimeError("page closed")
        # Must NOT raise
        await capture_macos.prepare_for_screenshot(page)


class TestWriteIndexEntry:
    def test_write_index_entry_appends_jsonl(
        self, capture_macos: ScreenCapture, tmp_path: Path
    ):
        """One JSON object per line, with all 5 fields, sorted keys for stable diff."""
        entry = ScreenshotIndexEntry(
            bvid="BV1xx411c7mD",
            start_ts=1000.5,
            end_ts=1830.2,
            duration_estimate=830,
            partial_flags=[],
        )
        capture_macos.write_index_entry(entry)

        index_path = tmp_path / "index.jsonl"
        assert index_path.exists()
        lines = index_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record == {
            "bvid": "BV1xx411c7mD",
            "start_ts": 1000.5,
            "end_ts": 1830.2,
            "duration_estimate": 830,
            "partial_flags": [],
        }

    def test_write_index_entry_appends_across_calls(
        self, capture_macos: ScreenCapture, tmp_path: Path
    ):
        """Two consecutive writes → 2 lines, no overwriting (FR-2.28.2e: append mode)."""
        for i in range(2):
            entry = ScreenshotIndexEntry(
                bvid=f"BV{i}",
                start_ts=float(i),
                end_ts=float(i) + 10.0,
                duration_estimate=10,
                partial_flags=[],
            )
            capture_macos.write_index_entry(entry)

        index_path = tmp_path / "index.jsonl"
        lines = index_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["bvid"] == "BV0"
        assert json.loads(lines[1])["bvid"] == "BV1"

    def test_write_index_entry_partial_flags_preserved(
        self, capture_macos: ScreenCapture, tmp_path: Path
    ):
        """partial_flags round-trip: written + read back identically (audit integrity)."""
        entry = ScreenshotIndexEntry(
            bvid="BV1",
            start_ts=1.0,
            end_ts=2.0,
            duration_estimate=1,
            partial_flags=["menu_bar_only", "no_video_frame"],
        )
        capture_macos.write_index_entry(entry)
        record = json.loads(
            (tmp_path / "index.jsonl").read_text(encoding="utf-8").strip()
        )
        assert record["partial_flags"] == ["menu_bar_only", "no_video_frame"]