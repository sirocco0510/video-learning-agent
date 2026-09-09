"""BilibiliAdapter(SSOT: requirements.md FR-2.0/2.1/2.17 + implementation-plan.md Phase 3.3 + F2-7/F2-10)。

B站平台的 PlatformAdapter 实现:
- 策略 ①: 委托给 BilibiliOfficialSubtitle(FR-2.1 官方 CC)
- 策略 ②: 用 BrowserDriver 跑 4 种 JS 探测
- 策略 ③: 继承 PlatformAdapter 默认实现(F2-10 后只剩 path ① yt-dlp)

构造依赖(F2-10 后):
- official: 必填(策略 ① 必需,直接持有 BilibiliOfficialSubtitle)
- audio_factory: 必填(策略 ③ path ① yt-dlp)
- transcriber: 必填(策略 ③ transcribe + cleanup)

设计: audio_factory / transcriber 由 strategy 层统一注入
(BilibiliAdapter 不自己构造),保证职责单一 + 易测试。

**F2-10 (2026-09-08) 删 tab_recorder / screenshot_controller 依赖**:
  改成"用户手动下载 → 代码扫今天目录",路径迁到 strategy._try_browser
  弹窗 enabled 分支。截图由 main.py 直接调 ScreenshotPhaseController。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
from vla.subtitle.platform_adapter import PlatformAdapter

if TYPE_CHECKING:
    from vla.audio.source_factory import AudioSourceFactory
    from vla.transcribe.streaming import AudioTranscriber


logger = logging.getLogger(__name__)


class BilibiliAdapter(PlatformAdapter):
    """B站平台字幕适配器(继承 PlatformAdapter,F2-7 改造,F2-10 简化)。

    fetch_via_recording 转发到 base impl(F2-10 后只剩 path ① yt-dlp),
    子类只 override 自己关心的 _make_stem(用 B站 bvid 做文件 stem)。
    """

    def __init__(
        self,
        official: BilibiliOfficialSubtitle,
        *,
        audio_factory: "AudioSourceFactory",
        transcriber: "AudioTranscriber",
    ) -> None:
        self.official = official
        self._audio_factory = audio_factory
        self._transcriber = transcriber

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

    # ---- 策略 ③:转发到 base impl(F2-10 后只剩 path ① yt-dlp) ----

    def fetch_via_recording(
        self,
        driver: Any,
        url: str,
        duration_sec: int,
        **kwargs: Any,
    ) -> tuple[str, dict] | None:
        """转发到 PlatformAdapter.fetch_via_recording(F2-10 后只剩 path ① yt-dlp)。

        kwargs 由 base impl 强制接受 2 REQUIRED:audio_factory / transcriber。
        本方法用 self 持有的 2 deps 填充,调用方传 kwargs 也允许(覆盖 self 持有的)—
        但当前 strategy 层统一不传,2 deps 完全由 self 注入。

        注:F2-10 删了 tab_recorder / screenshot_controller kwargs — 这两个能力
        (扫描今天目录 / 关键路径截图)分别由 strategy._try_browser 弹窗 enabled
        分支和 main.py 的 ScreenshotPhaseController 接管。
        """
        kwargs.setdefault("audio_factory", self._audio_factory)
        kwargs.setdefault("transcriber", self._transcriber)
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