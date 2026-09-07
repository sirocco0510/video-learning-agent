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


class TestPhaseD:
    def test_phase_d_writes_index_entry(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """PHASE D: 调 ScreenCapture.write_index_entry 写 index.jsonl.

        F2-4 actual signature takes ScreenshotIndexEntry dataclass, not 5 positional args.
        Test asserts the constructed dataclass, not positional args.
        """
        from vla.capture.screenshot_phase_controller import ScreenshotIndexEntry

        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        ctrl.phase_d_write_index(
            "Bv1_d", start_ts=100.0, end_ts=370.0, duration_estimate=270,
            partial_flags=["fullscreen_denied"],
        )
        expected_entry = ScreenshotIndexEntry(
            bvid="Bv1_d",
            start_ts=100.0,
            end_ts=370.0,
            duration_estimate=270,
            partial_flags=["fullscreen_denied"],
        )
        mock_capture.write_index_entry.assert_called_once_with(expected_entry)

    def test_phase_d_with_no_partial_flags(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """partial_flags=None → 写空 list (dataclass 不能 None)。"""
        from vla.capture.screenshot_phase_controller import ScreenshotIndexEntry

        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        ctrl.phase_d_write_index("Bv1_clean", 50.0, 320.0, 270)
        expected_entry = ScreenshotIndexEntry(
            bvid="Bv1_clean",
            start_ts=50.0,
            end_ts=320.0,
            duration_estimate=270,
            partial_flags=[],
        )
        mock_capture.write_index_entry.assert_called_once_with(expected_entry)


class TestDoctorPreWarm:
    def test_doctor_prewarm_warns_on_tcc_deny(self) -> None:
        """Q8: TCC 拒绝 → (False, msg 含 WARN/TCC/权限)。函数返回元组,不 print。

        Brief verbatim had capsys assertion; dropped because _check_screenshot_tcc
        returns a tuple, not prints. Caller (doctor command) is responsible for
        printing the returned message via typer.echo.
        """
        from vla.cli import _check_screenshot_tcc
        fake_page = MagicMock()
        fake_page.bring_to_front = AsyncMock()
        fake_page.evaluate = AsyncMock(side_effect=Exception("NotAllowedError"))
        fake_driver = MagicMock()
        fake_driver.page = fake_page

        ok, msg = _check_screenshot_tcc(fake_driver)
        assert ok is False
        assert "WARN" in msg or "TCC" in msg or "权限" in msg

    def test_doctor_prewarm_ok_on_grant(self) -> None:
        """fullscreen 成功 → (True, msg 含 OK)。"""
        from vla.cli import _check_screenshot_tcc
        fake_page = MagicMock()
        fake_page.bring_to_front = AsyncMock()
        fake_page.evaluate = AsyncMock(return_value=None)
        fake_driver = MagicMock()
        fake_driver.page = fake_page
        ok, msg = _check_screenshot_tcc(fake_driver)
        assert ok is True
        assert "OK" in msg

    def test_doctor_prewarm_no_page_returns_warn(self) -> None:
        """driver 无 .page 属性 → (False, WARN)。

        Brief verbatim only covered deny+grant; added this to cover the
        `getattr(driver, 'page', None) is None` branch in _try().
        """
        from vla.cli import _check_screenshot_tcc
        fake_driver = MagicMock(spec=[])  # no .page attribute
        ok, msg = _check_screenshot_tcc(fake_driver)
        assert ok is False
        assert "WARN" in msg or "权限" in msg


# ---------------- FR-2.28.2i: 多帧模式(3 start + 3 end-window + 1 end-final) ----------------


def _make_stateful_evaluate(time_sequence: list[float], ended: bool = True):
    """构造 evaluate side_effect:按 script 内容区分。

    - 含 'addEventListener' + 'ended' → 返回 ended (Promise resolve)
    - 含 'currentTime' 且其后**无** ' = ' (read) → 返回 next(time_sequence)
    - 含 'currentTime' 且其后**有** ' = ' (write/assign) → 返回 None
    - 其它 action (pause/play/requestFullscreen) → 返回 None
    """
    queue = list(time_sequence)

    async def fake_evaluate(script: str):
        # ended 事件 Promise
        if "addEventListener" in script and "ended" in script:
            return ended
        # currentTime 读/写 区分
        if "currentTime" in script:
            after = script.split("currentTime", 1)[1]
            if "= " not in after:  # 读(?.,?.currentTime)
                return queue.pop(0) if queue else 0.0
            # 写(v.currentTime = 0)→ action
        return None

    return fake_evaluate


class TestMultiFrame:
    def test_index_entry_has_frame_role_field(self) -> None:
        """ScreenshotIndexEntry 加 frame_role 字段。"""
        entry = ScreenshotIndexEntry(
            bvid="Bv1", start_ts=0.0, end_ts=0.0, duration_estimate=10,
            partial_flags=[], frame_role="start",
        )
        assert entry.frame_role == "start"

    def test_index_entry_default_frame_role_empty(self) -> None:
        """frame_role 缺省 → '' (向后兼容老 phase_d 路径)。"""
        entry = ScreenshotIndexEntry(
            bvid="Bv1", start_ts=0.0, end_ts=0.0, duration_estimate=10,
            partial_flags=[],
        )
        assert entry.frame_role == ""

    def test_multi_frame_long_video_captures_7_frames(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """duration=300s (长视频) → start 3 + end-window 3 + end-final 1 = 7 帧。"""
        # currentTime 序列:start 3 (0.0/0.6/1.5) + end-window 3 polls (290/293/296) + final 300
        time_seq = [0.0, 0.6, 1.5, 290.0, 293.0, 296.0, 300.0]
        mock_driver.evaluate = AsyncMock(side_effect=_make_stateful_evaluate(time_seq))

        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        result = asyncio.run(
            ctrl.phase_multi_frame(mock_driver, audio_id="Bv1_long", bvid="Bv1_long",
                                    duration_sec=300, poll_interval_sec=0.01)
        )
        # 7 capture_full_screen 调用
        assert mock_capture.capture_full_screen.await_count == 7
        # 返回 7 帧 (role, currentTime)
        assert len(result) == 7
        roles = [r[0] for r in result]
        assert roles == ["start", "start", "start", "end", "end", "end", "end_final"]

    def test_multi_frame_short_video_uses_adjusted_offsets(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """duration=10s (<13s) → end-window 退到 [duration*0.5, duration-1] = [5, 7, 9]。"""
        # currentTime 序列:start 3 + end-window 3 polls (5/7/9) + final 10
        time_seq = [0.0, 0.6, 1.5, 5.0, 7.0, 9.0, 10.0]
        mock_driver.evaluate = AsyncMock(side_effect=_make_stateful_evaluate(time_seq))

        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        asyncio.run(
            ctrl.phase_multi_frame(mock_driver, audio_id="Bv1_short", bvid="Bv1_short",
                                    duration_sec=10, poll_interval_sec=0.01)
        )
        # 仍然 7 帧
        assert mock_capture.capture_full_screen.await_count == 7

    def test_multi_frame_writes_index_with_frame_role(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """每次 capture 后 write_index_entry 调,带 frame_role 字段。"""
        time_seq = [0.0, 0.6, 1.5, 290.0, 293.0, 296.0, 300.0]
        mock_driver.evaluate = AsyncMock(side_effect=_make_stateful_evaluate(time_seq))

        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        asyncio.run(
            ctrl.phase_multi_frame(mock_driver, audio_id="Bv1_idx", bvid="Bv1_idx",
                                    duration_sec=300, poll_interval_sec=0.01)
        )
        # 7 个 write_index_entry,frame_role 正确
        write_calls = mock_capture.write_index_entry.call_args_list
        assert len(write_calls) == 7
        roles = [c.args[0].frame_role for c in write_calls]
        assert roles[:3] == ["start", "start", "start"]
        assert roles[3:6] == ["end", "end", "end"]
        assert roles[6] == "end_final"

    def test_multi_frame_filenames_contain_role_and_timestamp(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """文件名格式:<bvid>__<role>__t<timestamp>.png。"""
        time_seq = [0.0, 0.6, 1.5, 290.0, 293.0, 296.0, 300.0]
        mock_driver.evaluate = AsyncMock(side_effect=_make_stateful_evaluate(time_seq))

        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        asyncio.run(
            ctrl.phase_multi_frame(mock_driver, audio_id="Bv1_fname", bvid="Bv1fname",
                                    duration_sec=300, poll_interval_sec=0.01)
        )
        save_paths = [c.args[0] for c in mock_capture.capture_full_screen.await_args_list]
        # start 帧 3 个
        assert any("Bv1fname__start__t" in str(p) for p in save_paths)
        # end 帧 3 个
        assert any("Bv1fname__end__t" in str(p) for p in save_paths)
        # end_final 帧 1 个
        assert any("Bv1fname__end_final__t" in str(p) for p in save_paths)

    def test_multi_frame_capture_failure_marks_partial(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """单帧 capture 失败 → partial_flags 含 'capture_failed',继续后续帧。"""
        time_seq = [0.0, 0.6, 1.5, 290.0, 293.0, 296.0, 300.0]
        mock_driver.evaluate = AsyncMock(side_effect=_make_stateful_evaluate(time_seq))
        # 第 2 次 capture 失败
        mock_capture.capture_full_screen = AsyncMock(
            side_effect=[True, False, True, True, True, True, True]
        )

        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        asyncio.run(
            ctrl.phase_multi_frame(mock_driver, audio_id="Bv1_partial", bvid="Bv1p",
                                    duration_sec=300, poll_interval_sec=0.01)
        )
        # 7 次 capture 尝试(失败的不抛)
        assert mock_capture.capture_full_screen.await_count == 7
        # 7 个 write_index_entry
        write_calls = mock_capture.write_index_entry.call_args_list
        assert len(write_calls) == 7
        # 第 2 个(start idx 1)partial_flags 含 capture_failed
        second_entry = write_calls[1].args[0]
        assert "capture_failed" in second_entry.partial_flags
        # 其它 start 帧 partial_flags=[]
        assert write_calls[0].args[0].partial_flags == []
        assert write_calls[2].args[0].partial_flags == []

    def test_multi_frame_ended_timeout_continues(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """ended 事件超时 → 仍截 end_final 帧,不抛。"""
        time_seq = [0.0, 0.6, 1.5, 290.0, 293.0, 296.0, 300.0]
        # ended=False → 走 wait_for 超时分支
        mock_driver.evaluate = AsyncMock(side_effect=_make_stateful_evaluate(time_seq, ended=False))

        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        # 用更短的超时参数减少测试时长
        result = asyncio.run(
            ctrl.phase_multi_frame(mock_driver, audio_id="Bv1_timeout", bvid="Bv1t",
                                    duration_sec=300, poll_interval_sec=0.01,
                                    ended_timeout_sec=0.5)
        )
        # 仍然 7 帧
        assert mock_capture.capture_full_screen.await_count == 7
        assert result[-1][0] == "end_final"

    def test_multi_frame_does_not_raise_on_evaluate_failure(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """evaluate 任意一次抛 → log warning + 仍尝试后续帧。"""
        # currentTime read 抛,其它 action 仍工作
        async def fake_eval_with_failure(script: str):
            if "currentTime" in script and "=" not in script:
                raise Exception("network blip")
            if "ended" in script and "addEventListener" in script:
                return True
            return None

        mock_driver.evaluate = AsyncMock(side_effect=fake_eval_with_failure)

        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        # 不抛
        result = asyncio.run(
            ctrl.phase_multi_frame(mock_driver, audio_id="Bv1_blip", bvid="Bv1b",
                                    duration_sec=300, poll_interval_sec=0.01,
                                    poll_max_iterations=10)
        )
        # 7 帧仍尝试(capture_full_screen 仍被调 7 次)
        assert mock_capture.capture_full_screen.await_count == 7
        assert len(result) == 7
