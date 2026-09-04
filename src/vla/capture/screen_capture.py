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