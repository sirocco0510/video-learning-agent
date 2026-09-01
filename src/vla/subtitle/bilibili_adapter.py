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

from pathlib import Path
from typing import Any

from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
from vla.subtitle.browser_record import BrowserRecorder


DEFAULT_RECORDING_DIR = Path("./tmp/recordings")


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
        """录屏 + 转写(recorder 未注入时返回 None,跳过策略 ③)。"""
        if self.recorder is None:
            return None
        page = driver.new_background_page()
        text = self.recorder.record_and_transcribe(
            page, url, duration_sec, self._save_dir
        )
        return text, {"method": "recording", "platform": "bilibili"}