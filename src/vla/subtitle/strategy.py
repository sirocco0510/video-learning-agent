"""字幕策略编排器(SSOT: requirements.md FR-2.5/2.6/2.8/2.9/2.10/2.11)。

三级降级 + 弹窗状态机:

  ┌──────────────────────────────────────────────────────────────┐
  │ get_subtitle(url)                                            │
  └──────────────────────────────────────────────────────────────┘
         │
         ▼
  ┌────────────────┐   hit   → SubtitleResult(source="official")
  │ ① Official API │
  └────────────────┘   miss
         │
         ▼
  ┌──────────────────┐
  │ plugin_status ?  │
  └──────────────────┘
     │         │          │
   unknown  available  unavailable ───→ return None (caller → ③)
     │         │
     │         ▼
     │   ┌──────────────────────────┐ hit → SubtitleResult(source="plugin")
     │   │ ② find → wait            │
     │   └──────────────────────────┘ miss → return None (caller → ③)
     │
     ▼
  ┌────────────────────┐
  │ notifier.ask_open  │
  └────────────────────┘
     │          │           │
   "opened"  "skip"     "timeout"
     │          │           │
     ▼          ▼           ▼
  mark_avail  return SKIP   mark_unavailable
  + wait_sub   (status     + return None
  → hit? : None  unknown)   (caller → ③)

返回值:
  SubtitleResult    命中(① / ②)
  SKIP 哨兵          用户主动跳过
  None              ① ② 均失败 → 调用方走 ③ faster-whisper
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..models import SubtitleResult
from ..state.plugin_status import PluginStatus
from ..ui.macos_notify import MacOSNotifier

if TYPE_CHECKING:
    from ..config import VLAConfig
    from .bilibili_official import BilibiliOfficialSubtitle
    from .browser_plugin import BrowserPluginSubtitle


@dataclass(frozen=True)
class _SkipSignal:
    """哨兵:用户主动跳过当前视频(弹窗点了"跳过该视频")。"""

    reason: str = "user_skip"


SKIP = _SkipSignal()


class SubtitleStrategy:
    """三级字幕策略编排器。"""

    def __init__(
        self,
        official: "BilibiliOfficialSubtitle",
        plugin: "BrowserPluginSubtitle",
        plugin_status: PluginStatus,
        notifier: MacOSNotifier,
        config: "VLAConfig",
    ) -> None:
        self.official = official
        self.plugin = plugin
        self.plugin_status = plugin_status
        self.notifier = notifier
        self.config = config

    def get_subtitle(self, url: str) -> SubtitleResult | _SkipSignal | None:
        """先 ①;失败后根据 plugin_status 决定 ② / 弹窗 / 降级。"""
        # ① B站官方
        official = self.official.get_subtitle(url)
        if official is not None:
            text, metadata = official
            return SubtitleResult(text=text, source="official", metadata=metadata)

        # ② 浏览器插件
        status = self.plugin_status.get()
        if status == "available":
            return self._try_plugin_now(url)
        if status == "unavailable":
            return None  # 直接降级,不弹窗

        # status == "unknown" → 弹窗
        return self._prompt_user_and_try(url)

    def _try_plugin_now(self, url: str) -> SubtitleResult | None:
        """plugin_status=available 时直接扫描 find → wait。"""
        bvid = self.official.extract_bv_id(url)
        title = ""  # find_subtitle 允许空 title,直接走模糊

        path = self.plugin.find_subtitle(bvid, title)
        if path is None:
            path = self.plugin.wait_for_subtitle(bvid, title)
        if path is None:
            return None
        return self._build_plugin_result(bvid, path)

    def _prompt_user_and_try(self, url: str) -> SubtitleResult | _SkipSignal | None:
        """plugin_status=unknown 时弹窗,按用户反应分支。"""
        choice = self.notifier.ask_open_browser(
            title=f"启用 {self.config.browser_plugin.name} 字幕插件",
            url=url,
            timeout_sec=self.config.browser_plugin.remind_timeout_sec,
        )

        if choice == "skip":
            return SKIP

        if choice == "timeout":
            self.plugin_status.mark_unavailable("弹窗超时未响应")
            return None

        # choice == "opened"
        self.plugin_status.mark_available()
        bvid = self.official.extract_bv_id(url)
        path = self.plugin.wait_for_subtitle(bvid, "")
        if path is None:
            self.plugin_status.mark_unavailable("用户已开启但插件未生成字幕")
            return None
        return self._build_plugin_result(bvid, path)

    def _build_plugin_result(self, bvid: str, path) -> SubtitleResult:
        text = self.plugin.parse(path)
        metadata = {
            "bvid": bvid,
            "path": str(path),
            "suffix": path.suffix.lower(),
        }
        return SubtitleResult(text=text, source="plugin", metadata=metadata)
