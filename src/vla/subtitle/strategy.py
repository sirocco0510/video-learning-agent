"""字幕策略编排器(SSOT: requirements.md FR-2.5/2.6/2.8 + implementation-plan.md Phase 3.5)。

三级降级 + 平台无关:

  ┌──────────────────────────────────────────────────────────────┐
  │ get_subtitle(url)                                            │
  └──────────────────────────────────────────────────────────────┘
         │
         ▼
  registry.get_for_url(url)
         │
         ├── adapter ─→ ① fetch_api_subtitle
         │              miss ↓
         │              ② fetch_browser_subtitle(driver, url)
         │              miss ↓
         │              ③ fetch_via_recording(driver, url, duration_sec)
         │              miss → return None
         │
         └── None ─→ FallbackAdapter(直接用 driver / recorder,跳 ①)

返回 SubtitleResult(source='api'|'browser'|'whisper') 或 None(全失败)。
旧逻辑(弹窗 + SKIP + plugin_status 状态机)在 Phase 3.7 删除。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from vla.models import SubtitleResult


_DEFAULT_RECORDING_DIR = Path("./tmp/recordings")


class FallbackAdapter:
    """无匹配平台 adapter 时使用,直接调 BrowserDriver / BrowserRecorder。

    不实现 PlatformAdapter Protocol(无 match 类方法),仅在 strategy 内部构造。
    """

    def __init__(
        self,
        driver: Any,
        recorder: Any,
        save_dir: Path | None = None,
    ) -> None:
        self.driver = driver
        self.recorder = recorder
        self._save_dir = save_dir or _DEFAULT_RECORDING_DIR

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """无 API → 直接 miss。"""
        return None

    def fetch_browser_subtitle(
        self, driver: Any, url: str
    ) -> tuple[str, dict] | None:
        page = self.driver.new_background_page()
        text, meta = self.driver.fetch_subtitle_via_browser(page, url)
        if text is None:
            return None
        return text, {**(meta or {}), "platform": "fallback"}

    def fetch_via_recording(
        self, driver: Any, url: str, duration_sec: int
    ) -> tuple[str, dict] | None:
        if self.recorder is None:
            return None
        page = self.driver.new_background_page()
        text = self.recorder.record_and_transcribe(
            page, url, duration_sec, self._save_dir
        )
        return text, {"method": "recording", "platform": "fallback"}


class SubtitleStrategy:
    """三级字幕策略编排器(平台无关)。"""

    def __init__(
        self,
        registry: Any,
        driver: Any,
        recorder: Any,
        log: logging.Logger | None = None,
        save_dir: Path | None = None,
    ) -> None:
        self.registry = registry
        self.driver = driver
        self.recorder = recorder
        self.log = log or logging.getLogger(__name__)
        self._save_dir = save_dir

    def get_subtitle(
        self, url: str, duration_sec: int = 600
    ) -> SubtitleResult | None:
        """三级降级。任一命中即返回;全失败返回 None。"""
        # 选 adapter
        adapter = self.registry.get_for_url(url) if self.registry else None
        if adapter is None:
            self.log.warning("无匹配 adapter,使用 FallbackAdapter: %s", url)
            adapter = FallbackAdapter(self.driver, self.recorder, self._save_dir)

        # ① API
        try:
            result = adapter.fetch_api_subtitle(url)
            if result:
                text, meta = result
                self.log.info("✓ 策略 ① 命中(API)")
                return SubtitleResult(text=text, source="api", metadata=meta)
        except Exception as e:
            self.log.warning("策略 ① 失败: %s", e)

        # ② Browser(JS 探测)
        try:
            result = adapter.fetch_browser_subtitle(self.driver, url)
            if result:
                text, meta = result
                self.log.info(
                    "✓ 策略 ② 命中(browser: %s)", meta.get("method")
                )
                return SubtitleResult(
                    text=text, source="browser", metadata=meta
                )
        except Exception as e:
            self.log.warning("策略 ② 失败: %s", e)

        # ③ Recording(录屏 + Whisper)
        try:
            result = adapter.fetch_via_recording(
                self.driver, url, duration_sec
            )
            if result:
                text, meta = result
                self.log.info("✓ 策略 ③ 命中(whisper)")
                return SubtitleResult(
                    text=text, source="whisper", metadata=meta
                )
        except Exception as e:
            self.log.error("策略 ③ 失败(计入 transcribe_fail): %s", e)

        return None