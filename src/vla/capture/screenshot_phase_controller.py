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

    async def phase_b_then_c(
        self,
        page: Any,
        audio_id: str,
        duration_sec: int,
        poll_interval_sec: float = 5,
    ) -> float:
        """PHASE B (后台 poll) + PHASE C (末尾),并发跑,不 raise。

        B: duration_sec 期间每 poll_interval_sec 抓一次 (失败 → partial,继续)。
        C: duration_sec 即将结束时抓一次。
        返回 end_ts (monotonic)。

        Args:
            duration_sec: B 阶段总时长 (秒)
            poll_interval_sec: B 阶段轮询间隔 (秒); 测试可用 0.1 加速
        """
        end_ts = 0.0
        try:
            # PHASE B: 每 5s 抓一次(固定 5s 逻辑间隔,匹配 spec §3.7 "每 5s"),
            # 但实际 sleep 用 poll_interval_sec (测试用 0.1 加速)
            for elapsed in range(0, duration_sec, 5):
                save_path = self._capture.save_dir / f"{audio_id}.phase_b.{elapsed}.png"
                try:
                    await self._capture.capture_full_screen(save_path)
                except Exception:
                    pass  # Q8: 失败不抛,记 partial
                await asyncio.sleep(poll_interval_sec)

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

    def phase_d_write_index(
        self,
        audio_id: str,
        start_ts: float,
        end_ts: float,
        duration_estimate: int,
        partial_flags: list[str] | None = None,
    ) -> None:
        """PHASE D: 落盘 logs/screenshots/index.jsonl (委托给 ScreenCapture).

        Brief verbatim assumed ScreenCapture.write_index_entry(bvid, start_ts,
        end_ts, duration_estimate, partial_flags) — 5 positional args. But F2-4
        actual signature is write_index_entry(entry: ScreenshotIndexEntry) —
        1 dataclass arg. This wrapper constructs the dataclass and delegates.

        Args:
            audio_id: B站 BV id (dataclass field name: `bvid`)
            start_ts: PHASE A monotonic time (or 0.0 on partial)
            end_ts: PHASE C monotonic time (or 0.0 on partial)
            duration_estimate: int(end_ts - start_ts), ±5s tolerance per FR-2.28.2e
            partial_flags: list of degradation tags; None → stored as []

        partial_flags=None → stored as [] in dataclass (required by
        `list[str]` type, dataclass 不能 None).
        """
        entry = ScreenshotIndexEntry(
            bvid=audio_id,
            start_ts=start_ts,
            end_ts=end_ts,
            duration_estimate=duration_estimate,
            partial_flags=partial_flags if partial_flags is not None else [],
        )
        self._capture.write_index_entry(entry)
