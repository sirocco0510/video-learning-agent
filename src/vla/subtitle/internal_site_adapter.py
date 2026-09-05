"""InternalSiteAdapter stub(SSOT: requirements.md FR-2.18 + implementation-plan.md Phase 3.4 + F2-7)。

公司内部视频网站 adapter 占位实现。

当前状态:
- 无 API 格式(等公司下发账号后接入)
- 无字幕提取逻辑(等拿到页面结构后实现)
- fetch_api_subtitle / fetch_browser_subtitle / fetch_via_recording stub 返回 None,
  让 strategy 优雅降级

匹配规则:`internal.example.com` / `video.corp.local` 等预定义内部域名集合。
后续可由配置驱动(platforms.internal.domains)。

F2-7 改造:
- 继承 PlatformAdapter(base class),与 BilibiliAdapter 风格一致
- __init__ 接 4 F2-7 deps(audio_factory / tab_recorder / transcriber / screenshot_controller),
  存为 private attributes;fetch_via_recording stub 仍返回 None
  (等内部站真正接入后再考虑 override 转发 base impl)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vla.subtitle.platform_adapter import PlatformAdapter

if TYPE_CHECKING:
    from vla.audio.source_factory import AudioSourceFactory
    from vla.capture.screenshot_phase_controller import ScreenshotPhaseController
    from vla.subtitle.tab_audio_recorder import TabAudioRecorder
    from vla.transcribe.streaming import AudioTranscriber


# 预定义内部域名集合;后续可由 config.platforms.internal.domains 覆盖
_INTERNAL_DOMAINS: tuple[str, ...] = (
    "internal.example.com",
    "video.corp.local",
)


class InternalSiteAdapter(PlatformAdapter):
    """公司内部视频网站 adapter stub(占位实现,继承 PlatformAdapter)。"""

    def __init__(
        self,
        *,
        audio_factory: "AudioSourceFactory",
        tab_recorder: "TabAudioRecorder",
        transcriber: "AudioTranscriber",
        screenshot_controller: "ScreenshotPhaseController | None" = None,
    ) -> None:
        # F2-7:存为 private attributes,与 BilibiliAdapter 风格一致
        self._audio_factory = audio_factory
        self._tab_recorder = tab_recorder
        self._transcriber = transcriber
        self._screenshot_controller = screenshot_controller

    @classmethod
    def match(cls, url: str) -> bool:
        """匹配预定义的内部域名。"""
        return any(domain in url for domain in _INTERNAL_DOMAINS)

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """stub: 无 API,返回 None。"""
        return None

    def fetch_browser_subtitle(
        self, driver: Any, url: str
    ) -> tuple[str, dict] | None:
        """stub: 等拿到页面结构后实现。"""
        return None

    def fetch_via_recording(
        self, driver: Any, url: str, duration_sec: int
    ) -> tuple[str, dict] | None:
        """stub: 录屏兜底可后续接入,目前返回 None。

        注意:此处故意不 override 转发到 base impl —— 内部站拿到账号/页面结构前,
        默认 None 让 strategy 降级即可。等真正接入后再考虑 override。
        """
        return None
