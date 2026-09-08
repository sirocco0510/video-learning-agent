"""ScreenshotPhaseController:4-phase 截图管线(SSOT: spec 2026-09-03-fr2-fr3 §3.7)。

FR-2.28 触发点:PlatformAdapter.fetch_via_recording path ② (Tab Audio Recorder fallback)。
路径① yt-dlp 不触发(无浏览器 page)。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


logger = logging.getLogger(__name__)


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
    """FR-2.28.2e:logs/screenshots/index.jsonl 每行 schema。

    frame_role (FR-2.28.2i):
        - "start" / "end" / "end_final" — multi_frame 模式
        - "" — 老 phase_d 路径(向后兼容)
    """
    bvid: str
    start_ts: float
    end_ts: float
    duration_estimate: int
    partial_flags: list[str]
    frame_role: str = ""


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

        v3.2.1.3: page 操作必须跑在 driver executor 内部 thread(connect 时
        dispatcher fiber 所在 thread)。这里把 driver 传给 prepare_for_screenshot,
        capture 内部用 driver.run_on_driver_thread 包装。page.evaluate 也走 driver。
        """
        try:
            await self._capture.prepare_for_screenshot(page, driver=self._driver)
            fullscreen_ok = True
            try:
                # v3.2.1.3: page.evaluate 走 driver executor(async 版本不阻塞 loop)
                await self._driver.arun_on_driver_thread(
                    page.evaluate,
                    "video.currentTime=0; video.pause(); video.requestFullscreen()",
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

    # ---- 同步包装方法 (用于 sync 代码) ----

    def phase_a_start_sync(self, page: Any, audio_id: str) -> float:
        """同步版本的 phase_a_start (用于 sync 代码)。

        通过 asyncio.run 在后台运行异步版本。
        """
        return asyncio.run(self.phase_a_start(page, audio_id))

    async def phase_a_start_url(self, *, url: str, audio_id: str) -> float:
        """F2-6 v3.2.1:PHASE A url 入口。

        v3.2.1.8 (2026-09-08):**复用用户已开的 B站 tab**,不再 `new_background_page + goto`。
        旧实现每次都开空白 tab + 跳 B站 — 浪费时间、抢 Chrome 资源、用户在 tab 栏
        看到空白页闪一下。修法:从 driver 所有 pages 里找 URL 含目标 bvid 的 page;
        找不到 → 决策 A 跳过视频(让用户先在 Chrome 手动打开)。

        设计动机:
        - 用户已经在 Chrome 打开 B站(他自己要点播放);我们不应再加一个 tab
        - 用户实际看到的视频 = 他手动开的那一个;截图也应是对那个 tab 截
        - 复用现有 tab 可避免 Chrome memory saver 关闭 background tab 的 race

        Returns: monotonic start_ts 或 0.0(没找到匹配 tab / page 已 closed / 截图失败)。
        Never raises — 失败时返回 0.0 走决策 A。

        v3.2.1.3 fix: page 操作必须跑在 driver executor 内部 thread(connect 时
        dispatcher fiber 所在 thread)。search + bring_to_front + is_closed 全走
        driver.arun_on_driver_thread。
        """
        # 1. 从 url 提 bvid 作为匹配 key(case-insensitive,容许 "BV1" / "Bv1")
        import re
        bvid_m = re.search(r"([Bb][Vv][A-Za-z0-9]+)", url)
        if not bvid_m:
            logger.warning(
                "phase_a_start_url: URL 不含 bvid (%s) → 决策 A 跳视频", url,
            )
            return 0.0
        bvid = bvid_m.group(1)

        # 2. 找用户已开的 B站 tab
        page = await self._driver.arun_on_driver_thread(
            self._driver.find_page_by_url_substring, bvid,
        )
        if page is None:
            logger.warning(
                "phase_a_start_url: 找不到已开 B站 tab (bvid=%s) → 决策 A 跳视频。"
                "请先在 Chrome 打开 %s", bvid, url,
            )
            return 0.0

        # 3. bring_to_front 让 Chrome 不 unload
        try:
            await self._driver.arun_on_driver_thread(page.bring_to_front)
        except Exception as e:
            logger.debug("phase_a_start_url: bring_to_front best-effort 失败: %s", e)

        # 4. 检查 page 是否还活着
        try:
            is_closed = await self._driver.arun_on_driver_thread(
                lambda: page.is_closed()
            )
        except Exception as e:
            logger.warning("phase_a_start_url: page.is_closed() 检查失败: %s", e)
            is_closed = False

        if is_closed:
            logger.warning(
                "phase_a_start_url: page 已关闭(Chrome memory saver / 反爬),"
                "Phase A 失败 → 决策 A 跳过视频"
            )
            return 0.0

        # 5. 委托给 phase_a_start(pause + capture)
        return await self.phase_a_start(page, audio_id)

    async def phase_c_end_url(self, *, url: str, audio_id: str) -> None:
        """F2-6 v3.2.1:PHASE C url 入口(只截末尾 1 帧)。

        v3.2.1.8:同 phase_a_start_url,复用用户已开的 B站 tab,不再开新空白页。
        找不到 → log warning,return(决策 A 只对 Phase A,Phase C 失败不阻塞主流程)。
        """
        import re
        bvid_m = re.search(r"([Bb][Vv][A-Za-z0-9]+)", url)
        if not bvid_m:
            logger.warning("phase_c_end_url: URL 不含 bvid (%s),跳过截图", url)
            return
        bvid = bvid_m.group(1)

        page = await self._driver.arun_on_driver_thread(
            self._driver.find_page_by_url_substring, bvid,
        )
        if page is None:
            logger.warning(
                "phase_c_end_url: 找不到已开 B站 tab (bvid=%s),跳过截图", bvid,
            )
            return
        try:
            await self._driver.arun_on_driver_thread(page.bring_to_front)
        except Exception:
            pass
        await self.phase_c_only(page, audio_id)

    async def phase_c_only(self, page: Any, audio_id: str) -> float:
        """PHASE C only:只截末尾 1 帧(Phase C 单独入口,不跑 phase_b 轮询)。

        Returns: monotonic end_ts 或 0.0(失败时)。
        """
        save_path = self._capture.save_dir / f"{audio_id}.phase_c.png"
        try:
            ok = await self._capture.capture_full_screen(save_path)
            if not ok:
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

    # ---- 同步包装方法 (用于 sync 代码) ----

    def phase_b_then_c_sync(
        self,
        page: Any,
        audio_id: str,
        duration_sec: int,
        poll_interval_sec: float = 5,
    ) -> float:
        """同步版本的 phase_b_then_c (用于 sync 代码)。

        通过 asyncio.run 在后台运行异步版本。
        """
        return asyncio.run(self.phase_b_then_c(page, audio_id, duration_sec, poll_interval_sec))

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
            frame_role="",  # 老 phase_d 路径不用
        )
        self._capture.write_index_entry(entry)

    # ---------------- FR-2.28.2i: 多帧模式 ----------------

    async def phase_multi_frame(
        self,
        page: Any,
        audio_id: str,
        bvid: str,
        duration_sec: int,
        poll_interval_sec: float = 0.5,
        ended_timeout_sec: float = 30.0,
        poll_max_iterations: int = 1000,
    ) -> list[tuple[str, float]]:
        """多帧模式:3 start + 3 end-window + 1 end-final = 7 帧。

        Args:
            page: Playwright page(目标 B站视频页)
            audio_id: 兼容旧字段(本方法用 bvid 作文件名)
            bvid: 用于文件名(<bvid>__<role>__t<TIMESTAMP>.png)
            duration_sec: 视频总秒数
            poll_interval_sec: 末尾等待 currentTime 轮询间隔
            ended_timeout_sec: 等待 ended 事件超时

        Returns:
            list of (frame_role, currentTime) 7 个元素。
            失败帧仍占位返回(但 partial_flags 标记)。

        失败降级:
            - 任意 capture_full_screen 失败 → partial_flags=["capture_failed"]
            - evaluate 抛 → log warning + 该帧用 default 值占位
            - ended 事件超时 → 仍截 end_final,cur 用 duration_sec
        """
        # 决定 end-window 偏移(短视频 < 13s 退到 [duration*0.5, duration-1])
        if duration_sec < 13:
            end_offsets = (
                duration_sec * 0.5,
                duration_sec * 0.7,
                max(duration_sec - 1.0, 0.0),
            )
        else:
            end_offsets = (
                duration_sec - 10.0,
                duration_sec - 7.0,
                max(duration_sec - 4.0, 0.0),
            )

        captured: list[tuple[str, float]] = []

        # ---- START: 3 frames at cur ≈ 0.0/0.6/1.5 ----
        try:
            await page.evaluate(
                "() => { const v = document.querySelector('video'); "
                "if (v) { v.pause(); v.currentTime = 0; } }"
            )
        except Exception as e:
            logger.warning("multi_frame: pause+seek failed: %s", e)

        for target_t in (0.0, 0.6, 1.5):
            await asyncio.sleep(0.2)  # 等帧稳定
            cur_t = await _safe_get_current_time(page, default=target_t)
            save_path = self._capture.save_dir / f"{bvid}__start__t{cur_t:05.1f}.png"
            ok, ts = await _capture_and_record(
                self._capture, save_path=save_path, bvid=bvid,
                duration_estimate=duration_sec, frame_role="start",
            )
            captured.append(("start", cur_t))
            _ = ts

        # play 启动(B站 fullscreen 也会触发 play,这里再保险一下)
        try:
            await page.evaluate("() => document.querySelector('video')?.play()")
        except Exception:
            pass

        # ---- END-WINDOW: 3 frames at end_offsets ----
        for target_t in end_offsets:
            # 等待 currentTime >= target_t(有 poll_max_iterations 安全帽,防止 evaluate
            # 一直失败导致无限循环;production 默认 1000 次足够 1h 视频)
            poll_count = 0
            while poll_count < poll_max_iterations:
                poll_count += 1
                cur = await _safe_get_current_time(page, default=-1.0)
                if cur < 0:
                    await asyncio.sleep(poll_interval_sec)
                    continue
                if cur >= target_t:
                    break
                await asyncio.sleep(poll_interval_sec)
            else:
                logger.warning(
                    "multi_frame: end-window poll reached max iterations "
                    "(target_t=%s, dur=%s) — 截默认帧",
                    target_t, duration_sec,
                )

            save_path = self._capture.save_dir / f"{bvid}__end__t{target_t:05.1f}.png"
            ok, ts = await _capture_and_record(
                self._capture, save_path=save_path, bvid=bvid,
                duration_estimate=duration_sec, frame_role="end",
            )
            captured.append(("end", target_t))

        # ---- END-FINAL: 1 frame after ended ----
        try:
            await asyncio.wait_for(
                page.evaluate(
                    "() => new Promise(r => { "
                    "const v = document.querySelector('video'); "
                    "if (!v) return r(false); "
                    "if (v.ended) return r(true); "
                    "v.addEventListener('ended', () => r(true), {once: true}); "
                    "})"
                ),
                timeout=ended_timeout_sec,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("multi_frame: ended wait failed: %s", e)

        final_t = await _safe_get_current_time(page, default=float(duration_sec))
        save_path = self._capture.save_dir / f"{bvid}__end_final__t{final_t:05.1f}.png"
        ok, ts = await _capture_and_record(
            self._capture, save_path=save_path, bvid=bvid,
            duration_estimate=duration_sec, frame_role="end_final",
        )
        captured.append(("end_final", final_t))

        return captured


# ---------------- module-level helpers(便于 mock) ----------------


async def _safe_get_current_time(page: Any, default: float = 0.0) -> float:
    """调 page.evaluate 取 video.currentTime;失败返回 default,不抛。"""
    try:
        cur = await page.evaluate(
            "() => document.querySelector('video')?.currentTime"
        )
        if cur is None or cur < 0:
            return default
        return float(cur)
    except Exception as e:
        logger.warning("multi_frame: get currentTime failed: %s", e)
        return default


async def _capture_and_record(
    capture: Any,
    save_path: Path,
    bvid: str,
    duration_estimate: int,
    frame_role: str,
) -> tuple[bool, float]:
    """调 capture_full_screen + write_index_entry;失败 partial_flags 标记。

    Returns: (capture_ok, monotonic_ts)
    """
    try:
        ok = await capture.capture_full_screen(save_path)
    except Exception as e:
        logger.warning("multi_frame: capture failed: %s", e)
        ok = False
    ts = time.monotonic()
    entry = ScreenshotIndexEntry(
        bvid=bvid,
        start_ts=ts,
        end_ts=ts,
        duration_estimate=duration_estimate,
        partial_flags=[] if ok else ["capture_failed"],
        frame_role=frame_role,
    )
    capture.write_index_entry(entry)
    return ok, ts
