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
        """PHASE A: 开头截图 (page bring-to-front + fullscreen + screencapture)。

        Returns: monotonic start_ts (>0 on success, 0.0 on fullscreen deny per Q8).
        Never raises — partial failures all continue.
        """
        try:
            await self._capture.prepare_for_screenshot(page)
            fullscreen_ok = True
            try:
                await page.evaluate(
                    "video.currentTime=0; video.pause(); video.requestFullscreen()"
                )
            except Exception:
                # Q8: Warn, 不抛 — but signal partial via 0.0 return
                fullscreen_ok = False
            await asyncio.sleep(2.0)  # 等全屏动画
            self._notifier.info("准备截图,请稍候")  # B 级 (FR-2.28.2d)

            save_path = self._capture.save_dir / f"{audio_id}.phase_a.png"
            ok = await self._capture.capture_full_screen(save_path)
            if not ok or not fullscreen_ok:
                return 0.0
            return time.monotonic()
        except Exception:
            return 0.0
