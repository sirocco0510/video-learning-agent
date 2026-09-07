"""长视频末尾截图前的状态恢复(SSOT: requirements.md FR-2.28.2g)。

5 步骤:① 重走 tab_finder → ② frontmost check + activate Chrome →
③ bring_to_front + focus → ④ fullscreen → ⑤ resume if paused。

任一步异常 → log warning + 继续后续步骤,返回恢复后的 page。
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Any, Optional, Protocol

from vla.capture.tab_finder import find_bilibili_page


logger = logging.getLogger(__name__)


class _PageLike(Protocol):
    url: str
    async def bring_to_front(self) -> None: ...
    async def evaluate(self, script: str) -> Any: ...


class _BrowserLike(Protocol):
    contexts: list[Any]


# ---------------- helpers(独立可测) ----------------


def detect_frontmost_app() -> Optional[str]:
    """用 osascript 取前台 APP 名字;失败返回 None。"""
    try:
        result = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to get name of first process '
                "whose frontmost is true",
            ],
            check=False, capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def activate_chrome() -> bool:
    """osascript activate Google Chrome;成功返回 True。"""
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            check=False, capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------- main orchestration ----------------


async def reestablish_video_page(
    page: _PageLike, browser: _BrowserLike, bvid: str,
) -> _PageLike:
    """末尾截图前恢复 Chrome + B站 tab + 全屏 + playing。

    Args:
        page: 当前 Playwright page(可能是其他 tab/page)
        browser: Playwright Browser(CDP 连接)
        bvid: 目标 BV 号

    Returns:
        恢复后的 page(可能等于原 page)。任一步异常都被吞,
        caller 自己根据返回的 page.url 判定是否成功。

    Side effects:
        - 可能激活 Chrome 窗口
        - 可能触发 fullscreen
        - 可能调 video.play()
        - 不抢用户当前正在用的 APP(仅在末尾截图瞬间切,截完不主动切回)
    """
    # ① tab 找回(用户可能切 tab)
    if bvid not in page.url:
        try:
            new_page = find_bilibili_page(browser, bvid)
            if new_page is not None:
                page = new_page
        except Exception as e:
            logger.warning("reestablish step 1 (tab_finder) failed: %s", e)

    # ② frontmost check + activate Chrome
    try:
        front = detect_frontmost_app()
        if front and "Chrome" not in front:
            activate_chrome()
            await asyncio.sleep(0.3)
    except Exception as e:
        logger.warning("reestablish step 2 (frontmost) failed: %s", e)

    # ③ bring_to_front + focus
    try:
        await page.bring_to_front()
    except Exception as e:
        logger.warning("reestablish step 3 (bring_to_front) failed: %s", e)
    try:
        await page.evaluate("window.focus()")
    except Exception as e:
        logger.warning("reestablish step 3 (focus) failed: %s", e)

    # ④ fullscreen(B站 fullscreen 触发 play,这是好事)
    try:
        await page.evaluate(
            "() => { const v = document.querySelector('video'); "
            "if (v && v.requestFullscreen) v.requestFullscreen(); }"
        )
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning("reestablish step 4 (fullscreen) failed: %s", e)

    # ⑤ resume if paused(B站切后台会自动暂停)
    try:
        was_paused = await page.evaluate(
            "() => { const v = document.querySelector('video'); "
            "if (!v) return null; "
            "if (v.paused) { v.play(); return true; } "
            "return false; }"
        )
        if was_paused:
            await asyncio.sleep(0.3)
    except Exception as e:
        logger.warning("reestablish step 5 (resume) failed: %s", e)

    return page