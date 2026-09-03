"""BilibiliAdapter(SSOT: requirements.md FR-2.0/2.1/2.17 + implementation-plan.md Phase 3.3)。

B站平台的 PlatformAdapter 实现:
- 策略 ①: 委托给 BilibiliOfficialSubtitle(FR-2.1 官方 CC)
- 策略 ②: 用 BrowserDriver 跑 4 种 JS 探测
- 策略 ③: 用 BrowserRecorder 录屏 + Whisper

构造依赖:
- official: 必填(策略 ① 必需)
- recorder: 可选(策略 ③ 兜底;为 None 时 fetch_via_recording 返回 None)

设计: BrowserDriver / BrowserRecorder 不在 adapter 内部创建,由 strategy 层注入,
保证职责单一 + 易于测试。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
from vla.subtitle.browser_record import BrowserRecorder


logger = logging.getLogger(__name__)
DEFAULT_RECORDING_DIR = Path("./tmp/recordings")


def _safe_close_page(page: Any) -> None:
    """R-15 page lifecycle:关闭 background page(防御性 swallow)。

    page 可能已被外部 close / Chrome memory saver unload / popup 副作用关掉;
    关闭失败就 log debug 继续,不阻塞主流程。
    """
    if page is None:
        return
    try:
        page.close()
    except Exception as e:
        logger.debug("close page 失败(已被外部关闭?):%s", e)


class BilibiliAdapter:
    """B站平台字幕适配器(PlatformAdapter duck typing 实现)。"""

    def __init__(
        self,
        official: BilibiliOfficialSubtitle,
        recorder: BrowserRecorder | None = None,
        save_dir: Path | None = None,
    ) -> None:
        self.official = official
        self.recorder = recorder
        self._save_dir = save_dir or DEFAULT_RECORDING_DIR

    @classmethod
    def match(cls, url: str) -> bool:
        """匹配 bilibili.com / b23.tv。"""
        return "bilibili.com" in url or "b23.tv" in url

    # ---- 策略 ①:B站官方 API ----

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """委托给 BilibiliOfficialSubtitle.get_subtitle()。"""
        return self.official.get_subtitle(url)

    # ---- 策略 ②:BrowserDriver 探测 ----

    def fetch_browser_subtitle(
        self, driver: Any, url: str
    ) -> tuple[str, dict] | None:
        """新建后台标签页 → 交给 BrowserDriver 跑 4 种 JS 探测。

        BrowserDriver.fetch_subtitle_via_browser 完成后会关闭 page。
        """
        page = driver.new_background_page()
        text, meta = driver.fetch_subtitle_via_browser(page, url)
        if text is None:
            return None
        # 合并 meta,标记平台
        return text, {**(meta or {}), "platform": "bilibili"}

    # ---- 策略 ③:BrowserRecorder 录屏 ----

    def fetch_via_recording(
        self, driver: Any, url: str, duration_sec: int
    ) -> tuple[str, dict] | None:
        """录屏 + 转写(recorder 未注入时返回 None,跳过策略 ③)。

        recorder 新规:返回 transcript 文件路径(Path)— 读一次供 SubtitleResult.text。

        Fix 3:page.goto 后立即 pause_page_video,消除录屏启动和视频播放的时间差。

        R-15:caller owns page lifecycle — 本函数新建的 page 必须在
        recorder 完成后(或异常时)显式 close,避免 Chrome tab 累积。
        """
        if self.recorder is None:
            return None
        page = driver.new_background_page()
        try:
            # 跳转 URL → Screen Recorder 才能录到 B站 tab 音频(不是空白页静音)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            except Exception:
                pass  # 录屏仍继续,可能录到静音
            # 暂停视频 — 让用户手动 Play 触发录制与视频对齐
            try:
                from vla.subtitle.page_control import pause_page_video
                pause_page_video(page)
            except Exception:
                pass

            transcript_path = self.recorder.record_and_transcribe(
                page, url, duration_sec, self._save_dir
            )
        except Exception as e:
            logger.warning("B站 adapter 录屏失败:%s", e)
            _safe_close_page(page)
            return None

        # 成功路径:caller owns page lifecycle,显式 close
        _safe_close_page(page)

        try:
            text = Path(transcript_path).read_text(encoding="utf-8")
        except OSError:
            return None
        return text, {
            "method": "recording",
            "platform": "bilibili",
            "transcript_path": str(transcript_path),
        }