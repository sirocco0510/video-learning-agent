"""BilibiliAdapter(SSOT: requirements.md FR-2.0/2.1/2.17 + implementation-plan.md Phase 3.3 + F2-7)。

B站平台的 PlatformAdapter 实现:
- 策略 ①: 委托给 BilibiliOfficialSubtitle(FR-2.1 官方 CC)
- 策略 ②: 用 BrowserDriver 跑 4 种 JS 探测
- 策略 ③: 继承 PlatformAdapter 默认实现
            (FR-2.14 v3:path ① yt-dlp → path ② Tab Audio Recorder,Q7 Silent fallback)

构造依赖(F2-7 后):
- official: 必填(策略 ① 必需,直接持有 BilibiliOfficialSubtitle)
- audio_factory: 必填(策略 ③ path ① yt-dlp)
- tab_recorder: 必填(策略 ③ path ② Tab Audio Recorder)
- transcriber: 必填(策略 ③ transcribe + cleanup)
- screenshot_controller: 可选(策略 ③ path ② 触发 FR-2.28 PHASE A/B/C/D)

设计: audio_factory / tab_recorder / transcriber / screenshot_controller 由
strategy 层统一注入(BilibiliAdapter 不自己构造),保证职责单一 + 易测试。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
from vla.subtitle.platform_adapter import PlatformAdapter

if TYPE_CHECKING:
    from vla.audio.source_factory import AudioSourceFactory
    from vla.capture.screenshot_phase_controller import ScreenshotPhaseController
    from vla.subtitle.tab_audio_recorder import TabAudioRecorder
    from vla.transcribe.streaming import AudioTranscriber


logger = logging.getLogger(__name__)


class BilibiliAdapter(PlatformAdapter):
    """B站平台字幕适配器(继承 PlatformAdapter,F2-7 改造)。

    fetch_via_recording 转发到 base impl(FR-2.14 v3),子类只 override 自己关心的:
    - _make_stem:用 B站 bvid 做文件 stem(避免 hash 抖动)
    """

    def __init__(
        self,
        official: BilibiliOfficialSubtitle,
        *,
        audio_factory: "AudioSourceFactory",
        tab_recorder: "TabAudioRecorder",
        transcriber: "AudioTranscriber",
        screenshot_controller: "ScreenshotPhaseController | None" = None,
    ) -> None:
        self.official = official
        self._audio_factory = audio_factory
        self._tab_recorder = tab_recorder
        self._transcriber = transcriber
        self._screenshot_controller = screenshot_controller

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

    # ---- 策略 ③:转发到 base impl(F2-7) ----

    def fetch_via_recording(
        self,
        driver: Any,
        url: str,
        duration_sec: int,
        **kwargs: Any,
    ) -> tuple[str, dict] | None:
        """转发到 PlatformAdapter.fetch_via_recording(FR-2.14 v3)。

        kwargs 由 base impl 强制接受 4 REQUIRED:audio_factory / tab_recorder /
        transcriber / screenshot_controller。本方法用 self 持有的 4 deps 填充,
        调用方传 kwargs 也允许(覆盖 self 持有的)— 但当前 strategy 层统一不传,
        4 deps 完全由 self 注入。
        """
        kwargs.setdefault("audio_factory", self._audio_factory)
        kwargs.setdefault("tab_recorder", self._tab_recorder)
        kwargs.setdefault("transcriber", self._transcriber)
        kwargs.setdefault("screenshot_controller", self._screenshot_controller)
        return super().fetch_via_recording(driver, url, duration_sec, **kwargs)

    # ---- stem override(F2-7) ----

    def _make_stem(self, url: str) -> str:
        """B站 URL 用 bvid 做文件 stem(base impl 默认 url hash)。

        bvid 提取失败时 fallback 到 base hash 行为。
        """
        try:
            from vla.utils.bvid import extract_bvid

            bvid = extract_bvid(url)
        except Exception:
            bvid = None
        return bvid if bvid else super()._make_stem(url)
