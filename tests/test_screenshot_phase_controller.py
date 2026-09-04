"""ScreenshotPhaseController 测试(SSOT: spec 2026-09-03-fr2-fr3 §3.7)。

FR-2.28:4-phase screenshot pipeline triggered on Tab Audio Recorder path ②.
- PHASE A: 开头截图(page bring-to-front + fullscreen + screencapture)
- PHASE B: 后台 poll(30s 内每 5s 抓一次,失败兜底 partial)
- PHASE C: 末尾截图(audio 即将结束时)
- PHASE D: 落盘 index.jsonl(失败 partial_flags 标注)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from vla.capture.screenshot_phase_controller import (
    ScreenshotIndexEntry,
    ScreenshotPhaseController,
)


@pytest.fixture
def mock_driver() -> MagicMock:
    """Mock Playwright page."""
    page = MagicMock()
    page.url = "https://www.bilibili.com/video/Bv1"
    page.bring_to_front = AsyncMock()
    page.evaluate = AsyncMock()
    return page


@pytest.fixture
def mock_capture(tmp_path: Path) -> MagicMock:
    """Mock ScreenCapture from F2-4."""
    cap = MagicMock()
    cap.save_dir = tmp_path / "screenshots"
    cap.save_dir.mkdir(parents=True, exist_ok=True)
    cap.capture_full_screen = AsyncMock(return_value=True)
    cap.prepare_for_screenshot = AsyncMock()
    cap.write_index_entry = MagicMock()
    return cap


@pytest.fixture
def mock_notifier() -> MagicMock:
    """Mock MacOSNotifier."""
    n = MagicMock()
    n.info = MagicMock()
    return n


class TestPhaseA:
    def test_phase_a_returns_start_ts_on_success(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """PHASE A 成功 → return monotonic time, capture called, notifier.info called."""
        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        # asyncio_mode = "auto", so phase_a_start is awaitable
        start_ts = asyncio.run(ctrl.phase_a_start(mock_driver, "Bv1_test"))
        assert isinstance(start_ts, float)
        assert start_ts > 0
        mock_capture.prepare_for_screenshot.assert_awaited_once_with(mock_driver)
        mock_capture.capture_full_screen.assert_awaited_once()
        mock_notifier.info.assert_called_once()
        # notifier 信息必须含"截图"+"稍候"
        msg = mock_notifier.info.call_args.args[0]
        assert "截图" in msg
        assert "稍候" in msg

    def test_phase_a_returns_zero_on_fullscreen_deny(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """Q8: TCC 拒绝 → controller 不 raise,return 0.0 (partial_flags 后续补)。"""
        # requestFullscreen 抛 NotAllowedError
        # NOTE: brief verbatim set side_effect to a 3-item list expecting
        # `prepare_for_screenshot` to be the real impl, but here it is mocked
        # (AsyncMock), so only the controller's own evaluate call happens.
        # Raising on every evaluate call is sufficient to trigger the deny path.
        mock_driver.evaluate.side_effect = Exception(
            "NotAllowedError: requestFullscreen denied by TCC"
        )
        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        start_ts = asyncio.run(ctrl.phase_a_start(mock_driver, "Bv1_tcc"))
        assert start_ts == 0.0
        # 不抛异常,降级到 capture
        mock_capture.capture_full_screen.assert_awaited_once()


class TestPhaseBC:
    def test_phase_b_then_c_returns_end_ts(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """PHASE B (后台 poll) + PHASE C (末尾) → 返回 end_ts。"""
        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        # duration_sec=15, poll_interval_sec=0.1 → B 跑 3 次 (0,5,10) + C 1 次
        # Test runs in ~0.5s instead of 15s
        end_ts = asyncio.run(
            ctrl.phase_b_then_c(mock_driver, "Bv1_bc", duration_sec=15, poll_interval_sec=0.1)
        )
        assert isinstance(end_ts, float)
        assert end_ts > 0
        # B 阶段调用了 capture_full_screen 至少 2 次,C 阶段又 1 次 → 总 ≥3 次
        assert mock_capture.capture_full_screen.await_count >= 3

    def test_phase_b_partial_failure_continues(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """Q8: PHASE B 单次失败不中断,继续到 C。"""
        # 第一次 capture 失败,后续成功
        mock_capture.capture_full_screen = AsyncMock(side_effect=[False, True, True, True])
        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        end_ts = asyncio.run(
            ctrl.phase_b_then_c(mock_driver, "Bv1_partial", duration_sec=15, poll_interval_sec=0.1)
        )
        assert end_ts > 0
