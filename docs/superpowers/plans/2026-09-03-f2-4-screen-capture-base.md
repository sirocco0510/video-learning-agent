# F2-4 — Screen Capture Base (FR-2.28 part 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `ScreenCapture` class for system-level screenshots (macOS `screencapture -x` + Windows PowerShell + `System.Drawing`) with `prepare_for_screenshot` page prep and `write_index_entry` JSONL audit writer. The 4-phase trigger (`ScreenshotPhaseController`) is deferred to plan F2-5.

**Architecture:**
- New package: `src/vla/capture/__init__.py` (empty)
- New module: `src/vla/capture/screen_capture.py`
  - `ScreenshotIndexEntry` dataclass (frozen) — JSONL row shape per FR-2.28.2e
  - `ScreenCapture` class — platform-aware sync screenshot + page prep + audit writer
- New tests: `tests/test_screen_capture.py` (4 tests)
  - macOS uses `screencapture -x`
  - Windows uses PowerShell + `System.Drawing`
  - Returns `False` on subprocess failure
  - `write_index_entry` appends JSONL

**Tech Stack:** Python 3.12, asyncio (`create_subprocess_exec`), `platform.system()` for OS detection, pydantic not needed (dataclass), pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md` §3.5 (FR-2.28 `ScreenCapture`) and §3.7 (4-phase architecture — **PHASE A/C/D only referenced, not implemented here**)

## Global Constraints

- **macOS TCC:** 首次 `screencapture` 触发屏幕录制权限请求; 用户拒绝 → `capture_full_screen` returns `False` (不抛错)
- **`prepare_for_screenshot` 失败兜底:** 不抛错, 后续 capture 会写 `partial_flags=["menu_bar_only"]` (消费方处理,本模块只记录调用失败 → 返回值 False)
- **Tests use `pytest-asyncio`** with `asyncio_mode = "auto"` (项目约定见 `.claude/CLAUDE.md`)
- **Tests mock `asyncio.create_subprocess_exec`** — 永远不真正调 `screencapture` (避免污染 CI / 触发 TCC)
- **`LANG=en_US.UTF-8` prefix** on every bash command (项目铁律)
- **256 GB disk cap:** screenshots ~200KB/PNG, 30min video = 2 PNGs (start + end) = 400KB total — 远低于磁盘上限
- **Disk cleanup:** screenshots 仅在 `quality_check` 通过 + 用户 review 后才能删; 失败保留供 audit (FR-2.28.2e 落盘 → `index.jsonl`)
- **Module layout:** `src/vla/capture/` 是新顶层 package, 不与 `subtitle/` / `transcribe/` 耦合
- **Frozen dataclass:** `ScreenshotIndexEntry` 必须 `@dataclass(frozen=True)`, 防止 downstream 误改导致 audit log 与磁盘 PNG 不一致

---

### Task 1: Write failing tests for `ScreenCapture` base

**Files:**
- Create: `src/vla/capture/__init__.py` (empty package init)
- Create: `tests/test_screen_capture.py`

**Interfaces:**
- `ScreenCapture(save_dir: Path = Path("./logs/screenshots"), platform_name: str | None = None)`
- `await ScreenCapture.capture_full_screen(save_path: Path) -> bool`
- `await ScreenCapture.prepare_for_screenshot(page: Any) -> None` (no return; failures swallowed)
- `ScreenCapture.write_index_entry(entry: ScreenshotIndexEntry) -> None`
- `ScreenshotIndexEntry(bvid: str, start_ts: float, end_ts: float, duration_estimate: int, partial_flags: list[str])`

- [ ] **Step 1: Create empty package init**

Create `src/vla/capture/__init__.py`:

```python
"""Screen capture module (FR-2.28).

Public surface:
- ScreenCapture: system-level screenshot helper (macOS screencapture, Windows PowerShell)
- ScreenshotIndexEntry: JSONL audit row dataclass

The 4-phase trigger (PHASE A start + PHASE B poll + PHASE C end + PHASE D audit)
lives in `screenshot_phase_controller.py` (plan F2-5).
"""
```

- [ ] **Step 2: Write 4 failing tests**

Create `tests/test_screen_capture.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail (no module yet)**

Run:
```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent && LANG=en_US.UTF-8 uv run pytest tests/test_screen_capture.py -v
```

Expected: `ModuleNotFoundError: No module named 'vla.capture'` (or similar). The 4 tests + 2 sub-tests fail with import error, NOT assertion errors — confirming the module is genuinely missing.

---

### Task 2: Implement `ScreenCapture` + `ScreenshotIndexEntry`

**Files:**
- Create: `src/vla/capture/screen_capture.py`

**Interfaces:**
- `class ScreenshotIndexEntry(frozen dataclass)` with 5 fields
- `class ScreenCapture` with `__init__` / `prepare_for_screenshot` / `capture_full_screen` / `_capture_macos` / `_capture_windows` / `write_index_entry`

- [ ] **Step 1: Write `screen_capture.py`**

Create `src/vla/capture/screen_capture.py`:

```python
"""Screen capture base module (FR-2.28 part 1, plan F2-4).

Responsibilities (this module):
- System-level full-screen capture (macOS `screencapture -x`, Windows PowerShell)
- Page-state preparation (`prepare_for_screenshot`)
- JSONL audit log writer (`write_index_entry`)

Out of scope (plan F2-5 → `screenshot_phase_controller.py`):
- 4-phase trigger (PHASE A start / B poll / C end / D audit)
- B-level notifier wiring
- `video.pause() / currentTime = X / play()` orchestration

Failure semantics (FR-2.28):
- Any capture failure (TCC deny / subprocess exit nonzero / timeout) → return False
- `prepare_for_screenshot` swallows page.evaluate errors → caller proceeds,
  capture itself will likely return False and downstream adds `partial_flags`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenshotIndexEntry:
    """One entry in `logs/screenshots/index.jsonl` (FR-2.28.2e audit trail).

    Fields:
    - bvid: B站 video id (BV-string). For non-B站 sources use stable hash.
    - start_ts: record_start_ts from PHASE A (monotonic seconds, `time.monotonic()`).
    - end_ts: record_end_ts from PHASE C (same clock as start_ts).
    - duration_estimate: int(end_ts - start_ts), used to sanity-check actual
      recording duration against video length (±5s tolerance per FR-2.28.2e).
    - partial_flags: list of strings flagging capture degradation:
      - "permission_denied" — macOS TCC deny
      - "menu_bar_only" — prepare_for_screenshot partially failed
      - "no_video_frame" — screenshot taken but video frame absent
      - "timeout" — capture subprocess exceeded 10s/15s timeout
      Empty list == full success (both screenshots + audit written).

    Frozen: audit log integrity — downstream consumers must not silently mutate.
    """

    bvid: str
    start_ts: float
    end_ts: float
    duration_estimate: int
    partial_flags: list[str]


class ScreenCapture:
    """FR-2.28: system-level screenshot helper.

    Args:
    - save_dir: directory for PNG screenshots + index.jsonl. Created on init.
    - platform_name: override OS detection (for tests). One of {"Darwin", "Windows"}.
      When None, uses `platform.system()`.

    Methods:
    - prepare_for_screenshot(page): bring_to_front + focus + moveTo/resizeTo + sleep(0.3)
    - capture_full_screen(save_path): returns True on success, False on failure
    - write_index_entry(entry): append one JSON line to save_dir/index.jsonl

    Thread/async model: all I/O is async (asyncio.create_subprocess_exec).
    Stateless beyond `save_dir` and resolved platform — safe to share across
    asyncio tasks within one process.
    """

    def __init__(
        self,
        save_dir: Path = Path("./logs/screenshots"),
        platform_name: str | None = None,
    ) -> None:
        self.save_dir = Path(save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._platform = platform_name if platform_name is not None else platform.system()

    async def prepare_for_screenshot(self, page: Any) -> None:
        """FR-2.28.2a: prepare page for a clean full-screen capture.

        Sequence (per design doc §3.5):
          ① page.bring_to_front()
          ② window.focus() via evaluate
          ③ window.moveTo(0,0) + window.resizeTo(screen.width, screen.height)
          ④ asyncio.sleep(0.3) — let the window manager settle

        Failure handling: ANY error is caught and logged at WARNING level.
        Caller (PHASE A/C in F2-5) proceeds with capture anyway; if capture
        itself fails, downstream writes `partial_flags=["menu_bar_only"]`.

        No return value — success/failure signalled only via log line.
        """
        try:
            await page.bring_to_front()
            await page.evaluate("window.focus()")
            await page.evaluate(
                "window.moveTo(0, 0); window.resizeTo(screen.width, screen.height);"
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning("⚠️ prepare_for_screenshot 部分失败: %s", e)

    async def capture_full_screen(self, save_path: Path) -> bool:
        """FR-2.28: capture the entire primary screen to `save_path`.

        Platform dispatch:
          - Darwin:  `screencapture -x <save_path>` (~0.3-0.5s, requires TCC)
          - Windows: PowerShell + System.Drawing + Forms (~1.5-3s, no special perm)
          - other:   log warning, return False

        Returns: True iff (returncode == 0 AND save_path exists after subprocess).

        Never raises — all exceptions caught and logged. Callers should check
        the boolean and add `partial_flags` to the index entry on False.
        """
        if self._platform == "Darwin":
            return await self._capture_macos(save_path)
        if self._platform == "Windows":
            return await self._capture_windows(save_path)
        logger.warning("⚠️ 不支持的平台: %s", self._platform)
        return False

    async def _capture_macos(self, save_path: Path) -> bool:
        """macOS path: `screencapture -x <save_path>`. ~0.3-0.5s.

        Flags:
          -x: no sound effect
        No `--type` (defaults to PNG based on extension).

        Timeout: 10s. Longer means TCC hung → treat as failure.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "screencapture",
                "-x",
                str(save_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode != 0:
                logger.warning(
                    "⚠️ screencapture 失败 (rc=%s): %s",
                    proc.returncode,
                    stderr.decode(errors="replace").strip(),
                )
                return False
            return save_path.exists()
        except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
            logger.warning("⚠️ screencapture 异常: %s", e)
            return False

    async def _capture_windows(self, save_path: Path) -> bool:
        """Windows path: PowerShell + System.Drawing + System.Windows.Forms.

        ~1.5-3s. No special permissions required (Windows doesn't gate screenshots
        the way macOS TCC does).

        Timeout: 15s (PowerShell cold-start is slow on first invocation).
        """
        # Note: we embed the path as-is into the PowerShell string. Path is supplied
        # by the caller (always internal — save_dir + audio_id + suffix), so injection
        # isn't a concern. We do NOT shell-escape.
        ps_script = (
            "Add-Type -AssemblyName System.Drawing\n"
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds\n"
            "$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height\n"
            "$g = [System.Drawing.Graphics]::FromImage($bmp)\n"
            "$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)\n"
            f'$bmp.Save("{save_path}", [System.Drawing.Imaging.ImageFormat]::Png)\n'
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-Command",
                ps_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            if proc.returncode != 0:
                logger.warning(
                    "⚠️ PowerShell 截图失败 (rc=%s): %s",
                    proc.returncode,
                    stderr.decode(errors="replace").strip(),
                )
                return False
            return save_path.exists()
        except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
            logger.warning("⚠️ PowerShell 截图异常: %s", e)
            return False

    def write_index_entry(self, entry: ScreenshotIndexEntry) -> None:
        """FR-2.28.2e: append one JSON line to `save_dir/index.jsonl`.

        File format: one JSON object per line, keys in dataclass declaration order
        (`asdict()` follows field order). UTF-8, ensure_ascii=False for CJK bvid.

        Append mode — multiple calls accumulate. File created on first call.

        Synchronous (file I/O is fast — <1ms for a single line).
        """
        index_path = self.save_dir / "index.jsonl"
        with index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
```

- [ ] **Step 2: Run the 4 tests**

Run:
```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent && LANG=en_US.UTF-8 uv run pytest tests/test_screen_capture.py -v
```

Expected: All 9 tests pass (`TestCaptureMacOS::test_capture_macos_uses_screencapture_x`, `TestCaptureWindows::test_capture_windows_uses_powershell`, `TestCaptureFailureSemantics::test_capture_returns_false_on_subprocess_failure`, `::test_capture_returns_false_on_timeout`, `::test_capture_returns_false_on_unsupported_platform`, `TestPrepareForScreenshot::test_prepare_for_screenshot_calls_full_sequence`, `::test_prepare_for_screenshot_swallows_errors`, `TestWriteIndexEntry::test_write_index_entry_appends_jsonl`, `::test_write_index_entry_appends_across_calls`, `::test_write_index_entry_partial_flags_preserved`).

- [ ] **Step 3: Verify ruff + mypy clean**

Run:
```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent && LANG=en_US.UTF-8 uv run ruff check src/vla/capture/ tests/test_screen_capture.py
```

Expected: All checks passed (or 0 errors). If ruff flags `E501` line-too-long on the embed PowerShell string, split with `\n` continuation — but the string already uses multi-line concatenation.

(Optional) Run:
```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent && LANG=en_US.UTF-8 uv run mypy src/vla/capture/screen_capture.py
```

Expected: No errors. `Any` is intentionally untyped (page is a Playwright Page, kept loose to avoid coupling this module to playwright imports).

---

### Task 3: Run full regression + commit

**Files:** (no source changes in this task)

- [ ] **Step 1: Run full test suite**

Run:
```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent && LANG=en_US.UTF-8 uv run pytest -v
```

Expected: All previously-passing tests still pass + the 9 new ScreenCapture tests pass. Baseline count was 491 (per design doc §7); new total = 491 + 9 = 500.

If any test fails:
- Read failure carefully — do NOT mass-edit
- If the failure is in a pre-existing test, your change to `__init__.py` or the new module likely imported something circular; check with `python -c "import vla.capture.screen_capture"`
- If it's a new test failure, run `uv run pytest tests/test_screen_capture.py -v` alone to isolate

- [ ] **Step 2: Run `vla doctor`**

Run:
```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent && LANG=en_US.UTF-8 uv run vla doctor
```

Expected: All checks green. `ScreenCapture` does not touch any external service (no LLM call, no API key, no ffmpeg), so doctor output is unchanged.

- [ ] **Step 3: Verify package exports**

Run:
```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent && LANG=en_US.UTF-8 uv run python -c "from vla.capture.screen_capture import ScreenCapture, ScreenshotIndexEntry; from pathlib import Path; cap = ScreenCapture(save_dir=Path('/tmp/vla_f2_4_smoke'), platform_name='Darwin'); print('OK', cap.save_dir)"
```

Expected: `OK /tmp/vla_f2_4_smoke` (directory created on instantiation). No exception.

- [ ] **Step 4: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent && git add src/vla/capture/__init__.py src/vla/capture/screen_capture.py tests/test_screen_capture.py
git commit -m "feat(capture): ScreenCapture base (FR-2.28 part 1, plan F2-4)

- macOS screencapture -x + Windows PowerShell + System.Drawing dispatch
- prepare_for_screenshot: bring_to_front + focus + moveTo/resizeTo + sleep(0.3)
- write_index_entry: append JSONL audit row (FR-2.28.2e)
- ScreenshotIndexEntry frozen dataclass (5 fields, audit integrity)
- 9 tests covering macOS/Windows subprocess + failure semantics + JSONL

PHASE A/B/C/D 4-phase trigger → plan F2-5 (screenshot_phase_controller)."
```

---

## Out of Scope (deferred)

- `ScreenshotPhaseController` 4-phase trigger — plan F2-5
- B-level notifier hook (FR-2.28.2d) — plan F2-5
- `video.pause() / currentTime = X / play()` orchestration — plan F2-5
- `vla doctor` `requestFullscreen()` pre-warm (FR-2.28.2c) — plan F2-5
- Integration into `PlatformAdapter.fetch_via_recording` (path ② only) — plan F2-7
- `BrowserRecorder` deletion — plan F2-8