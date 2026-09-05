# F2-5 — ScreenshotPhaseController (FR-2.28 part 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `ScreenshotPhaseController` that triggers the 4-phase screenshot pipeline (A 开头 / B 后台 / C 末尾 / D 审计) when Tab Audio Recorder fallback path ② runs.

**Architecture:**
- New module: `src/vla/capture/screenshot_phase_controller.py` (`ScreenshotPhaseController` + `ScreenshotIndexEntry` dataclass + 4-phase async API)
- New tests: `tests/test_screenshot_phase_controller.py` (8 tests covering phase A/B/C/D + partial failure + doctor pre-warm + Warn-on-TCC-deny)
- One helper extension to `src/vla/capture/screen_capture.py` from F2-4: `request_fullscreen_warmup(page) -> bool` (returns True if `page.evaluate("requestFullscreen")` resolved; False on `NotAllowedError`)

**Tech Stack:** Python 3.12, asyncio, pydantic v2 (index entry), existing `vla.ui.macos_notify.MacOSNotifier`

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md` §3.5 + §3.7 + §4.5 (FR-2.28)

**User rulings locked:** Q1=Polling / Q7=Silent / Q8=**Warn** (TCC deny → warning, exit 0) / Q3+Q9=Keep generic PluginStatus

## Global Constraints

- `tests/` is the test root; fixtures in `tests/fixtures/`
- TDD: write failing test → run → minimal impl → run → commit
- asyncio_mode = "auto" (pytest config already set)
- macOS TCC: `requestFullscreen()` may be denied by browser; controller must catch `NotAllowedError` and treat as Warn (Q8)
- Path ② (Tab Audio Recorder) is the ONLY trigger for screenshot phase controller (path ① yt-dlp has no browser page)
- B-level notifier used for "准备截图,请稍候" toast (FR-2.28.2d)
- Index format: `logs/screenshots/index.jsonl` lines `{bvid, start_ts, end_ts, duration_estimate, partial_flags}` (FR-2.28.2e)
- Disk cap: 256 GB machine, peak < 1 GB
- pydantic v2 for `ScreenshotIndexEntry`

## Interfaces from Earlier Plans (F2-1 / F2-2 / F2-4) — exact signatures this plan consumes

```python
# src/vla/capture/screen_capture.py (F2-4)
class ScreenCapture:
    def __init__(self, save_dir: Path = Path("./logs/screenshots")) -> None: ...
    async def capture_full_screen(self, save_path: Path) -> bool: ...
    async def prepare_for_screenshot(self, page: Any) -> None: ...
    def write_index_entry(self, bvid: str, start_ts: float, end_ts: float,
                          duration_estimate: int,
                          partial_flags: list[str] | None = None) -> None: ...
```

If `prepare_for_screenshot` is not yet implemented (F2-4 still pending), this plan mocks it. The signature above is the contract F2-4 must produce.

---

### Task 1: PHASE A — page bring-to-front + fullscreen + first screenshot

**Files:**
- Create: `src/vla/capture/screenshot_phase_controller.py`
- Modify: `src/vla/capture/screen_capture.py` (add `request_fullscreen_warmup` helper — needed for Phase A)
- Test: `tests/test_screenshot_phase_controller.py`

**Interfaces:**
- Produces: `ScreenshotPhaseController(driver, notifier, capture, save_dir)`
- Produces: `async phase_a_start(page, audio_id: str) -> float` — returns start_ts (monotonic); returns 0.0 on partial failure

- [ ] **Step 1: Write the failing test for PHASE A**

```python
# tests/test_screenshot_phase_controller.py
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
        mock_driver.evaluate.side_effect = [
            None,  # window.focus()
            None,  # window.moveTo/resizeTo
            Exception("NotAllowedError: requestFullscreen denied by TCC"),
        ]
        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        start_ts = asyncio.run(ctrl.phase_a_start(mock_driver, "Bv1_tcc"))
        assert start_ts == 0.0
        # 不抛异常,降级到 capture
        mock_capture.capture_full_screen.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screenshot_phase_controller.py::TestPhaseA -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'vla.capture.screenshot_phase_controller'`

- [ ] **Step 3: Add `request_fullscreen_warmup` to `screen_capture.py`** *(only if F2-4 already merged; otherwise inline as private helper below)*

```python
# src/vla/capture/screen_capture.py — append to existing class
class ScreenCapture:
    # ... existing methods ...

    async def request_fullscreen_warmup(self, page: Any) -> bool:
        """FR-2.28.2a: try page.evaluate('video.requestFullscreen()'); catch NotAllowedError.

        Returns: True if granted, False if denied by TCC.
        Page param duck-typed (Playwright Page).
        """
        try:
            await page.evaluate(
                "video.currentTime=0; video.pause(); video.requestFullscreen()"
            )
            return True
        except Exception:
            # Q8: Warn, 不抛
            return False
```

- [ ] **Step 4: Implement minimal `screenshot_phase_controller.py`**

```python
# src/vla/capture/screenshot_phase_controller.py
"""ScreenshotPhaseController:4-phase 截图管线(SSOT: spec 2026-09-03-fr2-fr3 §3.7)。

FR-2.28 触发点:PlatformAdapter.fetch_via_recording path ② (Tab Audio Recorder fallback)。
路径① yt-dlp 不触发(无浏览器 page)。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class _NotifierLike(Protocol):
    """duck-typed MacOSNotifier.info 协议(同 state/plugin_status.py 风格)。"""
    def info(self, message: str) -> None: ...


class _CaptureLike(Protocol):
    """duck-typed ScreenCapture 协议(从 F2-4 引入)。"""
    save_dir: Path
    async def capture_full_screen(self, save_path: Path) -> bool: ...
    async def prepare_for_screenshot(self, page: Any) -> None: ...
    def write_index_entry(
        self, bvid: str, start_ts: float, end_ts: float,
        duration_estimate: int, partial_flags: list[str] | None = None,
    ) -> None: ...


@dataclass
class ScreenshotIndexEntry:
    """FR-2.28.2e:logs/screenshots/index.jsonl 每行 schema。"""
    bvid: str
    start_ts: float
    end_ts: float
    duration_estimate: int
    partial_flags: list[str]


class ScreenshotPhaseController:
    """FR-2.28 4-phase 截图协调器。"""

    def __init__(
        self,
        driver: Any,
        notifier: _NotifierLike,
        capture: _CaptureLike,
    ) -> None:
        self._driver = driver
        self._notifier = notifier
        self._capture = capture

    async def phase_a_start(self, page: Any, audio_id: str) -> float:
        """PHASE A:开头截图(page bring-to-front + fullscreen + screencapture)。

        Returns: monotonic start_ts (>0 on success, 0.0 on fullscreen deny per Q8)。
        不 raise — 截图失败/部分失败都继续 audio recording。
        """
        try:
            await self._capture.prepare_for_screenshot(page)
            # requestFullscreen 失败被 capture 内部吞掉(Q8 Warn),但 controller 这里也试一遍
            try:
                await page.evaluate(
                    "video.currentTime=0; video.pause(); video.requestFullscreen()"
                )
            except Exception:
                # Q8: 警告即可,不抛
                pass
            await asyncio.sleep(2.0)  # 等全屏动画
            self._notifier.info("准备截图,请稍候")  # B 级 (FR-2.28.2d)

            save_path = self._capture.save_dir / f"{audio_id}.phase_a.png"
            ok = await self._capture.capture_full_screen(save_path)
            if not ok:
                return 0.0
            return time.monotonic()
        except Exception:
            # 任何异常 → 降级到 0(后续 PHASE D partial_flags 标注)
            return 0.0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_screenshot_phase_controller.py::TestPhaseA -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add src/vla/capture/screenshot_phase_controller.py src/vla/capture/screen_capture.py tests/test_screenshot_phase_controller.py
git commit -m "feat(F2-5): ScreenshotPhaseController.phase_a_start (FR-2.28 part 2a)"
```

---

### Task 2: PHASE B+C — 后台 poll + 末尾截图(async, run concurrently with transcribe)

**Files:**
- Modify: `src/vla/capture/screenshot_phase_controller.py`
- Test: `tests/test_screenshot_phase_controller.py`

**Interfaces:**
- Produces: `async phase_b_then_c(page, audio_id, duration_sec) -> float` — returns end_ts; never raises

- [ ] **Step 1: Write the failing test for PHASE B+C**

Append to `tests/test_screenshot_phase_controller.py`:

```python
class TestPhaseBC:
    def test_phase_b_then_c_returns_end_ts(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """PHASE B(后台 poll 30s 内每 5s)+ PHASE C(末尾)→ 返回 end_ts。"""
        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        # duration_sec=15 → B 跑 2 次(0,5s)+ C 1 次
        end_ts = asyncio.run(ctrl.phase_b_then_c(mock_driver, "Bv1_bc", duration_sec=15))
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
        end_ts = asyncio.run(ctrl.phase_b_then_c(mock_driver, "Bv1_partial", duration_sec=15))
        assert end_ts > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screenshot_phase_controller.py::TestPhaseBC -v`
Expected: FAIL with `phase_b_then_c` not defined

- [ ] **Step 3: Implement `phase_b_then_c`**

Append to `ScreenshotPhaseController`:

```python
    async def phase_b_then_c(self, page: Any, audio_id: str, duration_sec: int) -> float:
        """PHASE B(后台 poll)+ PHASE C(末尾),并发跑,不 raise。

        B:duration_sec 期间每 5s 抓一次(失败 → partial,继续)。
        C:duration_sec 即将结束时抓一次。
        返回 end_ts(monotonic)。
        """
        end_ts = 0.0
        try:
            # PHASE B: 每 5s 抓一次,直到 duration_sec
            for elapsed in range(0, duration_sec, 5):
                save_path = self._capture.save_dir / f"{audio_id}.phase_b.{elapsed}.png"
                try:
                    await self._capture.capture_full_screen(save_path)
                except Exception:
                    pass  # Q8: 失败不抛,记 partial
                await asyncio.sleep(5)

            # PHASE C: 末尾截图
            save_path = self._capture.save_dir / f"{audio_id}.phase_c.png"
            try:
                await self._capture.capture_full_screen(save_path)
                end_ts = time.monotonic()
            except Exception:
                pass
        except Exception:
            pass
        return end_ts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screenshot_phase_controller.py::TestPhaseBC -v`
Expected: PASS (2 tests, total 4 tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add src/vla/capture/screenshot_phase_controller.py tests/test_screenshot_phase_controller.py
git commit -m "feat(F2-5): ScreenshotPhaseController.phase_b_then_c (FR-2.28 part 2b)"
```

---

### Task 3: PHASE D — 落盘 index.jsonl

**Files:**
- Modify: `src/vla/capture/screenshot_phase_controller.py`
- Test: `tests/test_screenshot_phase_controller.py`

**Interfaces:**
- Produces: `phase_d_write_index(audio_id, start_ts, end_ts, duration_estimate, partial_flags=None)` — sync, delegates to `ScreenCapture.write_index_entry`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_screenshot_phase_controller.py`:

```python
class TestPhaseD:
    def test_phase_d_writes_index_entry(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """PHASE D:调 ScreenCapture.write_index_entry 写 index.jsonl。"""
        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        ctrl.phase_d_write_index(
            "Bv1_d", start_ts=100.0, end_ts=370.0, duration_estimate=270,
            partial_flags=["fullscreen_denied"],
        )
        mock_capture.write_index_entry.assert_called_once_with(
            "Bv1_d", 100.0, 370.0, 270, ["fullscreen_denied"],
        )

    def test_phase_d_with_no_partial_flags(
        self, mock_driver: MagicMock, mock_capture: MagicMock, mock_notifier: MagicMock
    ) -> None:
        """partial_flags=None → 写空 list。"""
        ctrl = ScreenshotPhaseController(mock_driver, mock_notifier, mock_capture)
        ctrl.phase_d_write_index("Bv1_clean", 50.0, 320.0, 270)
        mock_capture.write_index_entry.assert_called_once_with(
            "Bv1_clean", 50.0, 320.0, 270, None,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screenshot_phase_controller.py::TestPhaseD -v`
Expected: FAIL with `phase_d_write_index` not defined

- [ ] **Step 3: Implement `phase_d_write_index`**

Append to `ScreenshotPhaseController`:

```python
    def phase_d_write_index(
        self,
        audio_id: str,
        start_ts: float,
        end_ts: float,
        duration_estimate: int,
        partial_flags: list[str] | None = None,
    ) -> None:
        """PHASE D:落盘 logs/screenshots/index.jsonl(委托给 ScreenCapture)。"""
        self._capture.write_index_entry(
            audio_id, start_ts, end_ts, duration_estimate, partial_flags,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screenshot_phase_controller.py::TestPhaseD -v`
Expected: PASS (2 tests, total 6 tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add src/vla/capture/screenshot_phase_controller.py tests/test_screenshot_phase_controller.py
git commit -m "feat(F2-5): ScreenshotPhaseController.phase_d_write_index (FR-2.28 part 2d)"
```

---

### Task 4: `vla doctor` pre-warm — Warn on TCC deny (Q8)

**Files:**
- Modify: `src/vla/cli.py` (add `--check-screenshot` option)
- Test: `tests/test_screenshot_phase_controller.py`

- [ ] **Step 1: Write the failing test for doctor pre-warm**

Append to `tests/test_screenshot_phase_controller.py`:

```python
class TestDoctorPreWarm:
    def test_doctor_prewarm_warns_on_tcc_deny(self, tmp_path: Path, capsys) -> None:
        """Q8: TCC 拒绝 → warn,exit 0,exit code 不非零。"""
        from vla.cli import _check_screenshot_tcc
        fake_page = MagicMock()
        fake_page.url = "https://www.bilibili.com/video/Bv1"
        fake_page.bring_to_front = AsyncMock()
        fake_page.evaluate = AsyncMock(side_effect=Exception("NotAllowedError"))
        fake_driver = MagicMock()
        fake_driver.page = fake_page

        # _check_screenshot_tcc 返回 (ok: bool, message: str)
        result = _check_screenshot_tcc(fake_driver, tmp_path)
        ok, msg = result
        assert ok is False
        assert "TCC" in msg or "权限" in msg
        captured = capsys.readouterr()
        # warning 写到 stdout(不 raise)
        assert "WARN" in captured.out or "warn" in captured.out.lower()

    def test_doctor_prewarm_ok_on_grant(self, tmp_path: Path) -> None:
        """fullscreen 成功 → (True, ok msg)。"""
        from vla.cli import _check_screenshot_tcc
        fake_page = MagicMock()
        fake_page.url = "https://www.bilibili.com/video/Bv1"
        fake_page.bring_to_front = AsyncMock()
        fake_page.evaluate = AsyncMock(return_value=None)
        fake_driver = MagicMock()
        fake_driver.page = fake_page
        ok, msg = _check_screenshot_tcc(fake_driver, tmp_path)
        assert ok is True
        assert "OK" in msg or "正常" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screenshot_phase_controller.py::TestDoctorPreWarm -v`
Expected: FAIL with `cannot import name '_check_screenshot_tcc' from 'vla.cli'`

- [ ] **Step 3: Implement `_check_screenshot_tcc` in `cli.py`**

Add to `src/vla/cli.py`:

```python
def _check_screenshot_tcc(driver: Any, save_dir: Path) -> tuple[bool, str]:
    """FR-2.28.2c `vla doctor` pre-warm:Q8=TCC 拒绝 → warn+continue,exit 0。

    Returns: (ok, message)。
    ok=False 时 caller 只 print warning,不 raise,不 sys.exit(1)。
    """
    import asyncio
    from vla.capture.screen_capture import ScreenCapture

    capture = ScreenCapture(save_dir=save_dir)

    async def _try() -> bool:
        page = getattr(driver, "page", None)
        if page is None:
            return False
        try:
            await page.bring_to_front()
            await page.evaluate(
                "video.currentTime=0; video.pause(); video.requestFullscreen()"
            )
            return True
        except Exception:
            return False

    granted = asyncio.run(_try())
    if granted:
        return True, "屏幕录制权限 OK (FR-2.28.2c)"
    return False, "WARN: 屏幕录制权限被拒 — 截图功能不可,但音频转写可继续。可在 系统设置 → 隐私与安全性 → 屏幕录制 授权。"
```

Also add typer option to `doctor` command:

```python
@app.command()
def doctor(
    check_screenshot: bool = typer.Option(
        False, "--check-screenshot", help="FR-2.28.2c 屏幕录制权限 pre-warm (Q8=Warn)"
    ),
) -> None:
    """...existing docstring..."""
    # ... existing checks ...
    if check_screenshot:
        ok, msg = _check_screenshot_tcc(driver, Path("./logs/screenshots"))
        typer.echo(msg)
        # Q8: 不 raise,不 sys.exit(1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screenshot_phase_controller.py::TestDoctorPreWarm -v`
Expected: PASS (2 tests, total 8 tests)

- [ ] **Step 5: Run full test file**

Run: `uv run pytest tests/test_screenshot_phase_controller.py -v`
Expected: 8 tests PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add src/vla/cli.py tests/test_screenshot_phase_controller.py
git commit -m "feat(F2-5): doctor pre-warm screenshot TCC check (Q8 Warn)"
```

---

## Acceptance Criteria

- [ ] `uv run pytest tests/test_screenshot_phase_controller.py -v` → 8/8 PASS
- [ ] `grep -rn "class ScreenshotPhaseController" src/vla/capture/` → matches
- [ ] `grep -rn "phase_a_start\|phase_b_then_c\|phase_d_write_index" src/vla/main.py` → **no matches** (integration is F2-7's job, NOT this plan's)
- [ ] `_check_screenshot_tcc` is importable from `vla.cli`
- [ ] All 8 tests cover: phase A success / phase A TCC deny / phase B+C success / phase B partial fail / phase D with flags / phase D no flags / doctor grant / doctor deny

## Dependency Note

This plan assumes `ScreenCapture` from F2-4 exists with signature `ScreenCapture(save_dir).{capture_full_screen, prepare_for_screenshot, write_index_entry}`. If F2-4 not yet merged, F2-5 tests will fail at instantiation — fix by either merging F2-4 first or temporarily replacing `_CaptureLike` with a manual mock that has the same surface.