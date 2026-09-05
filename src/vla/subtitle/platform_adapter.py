"""PlatformAdapter 基类 + Registry(SSOT: requirements.md FR-2.0 + implementation-plan.md Phase 3.0 + F2-7)。

每个视频平台(B站、内部网站、未来 YouTube 等)继承 PlatformAdapter,
提供 3 个 fetch 方法(API / 浏览器 / 录屏);Registry 按注册顺序匹配 URL 域名。

设计演进:
- 2026-09-02 之前:`PlatformAdapter(Protocol)`,仅类型契约,具体类 duck typing。
- 2026-09-02(F2-7):改为普通 class,带 `fetch_via_recording` 默认实现(FR-2.14 v3),
  子类可继承后只 override 自己关心的 fetch 方法。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vla.audio.source_factory import AudioSourceFactory
    from vla.capture.screenshot_phase_controller import ScreenshotPhaseController
    from vla.subtitle.tab_audio_recorder import TabAudioRecorder
    from vla.transcribe.streaming import AudioTranscriber


_log = logging.getLogger(__name__)


class PlatformAdapter:
    """视频平台字幕适配器基类。

    子类必须 override:
    - `match`(classmethod,URL 域名判定)
    - `fetch_api_subtitle`(策略 ①)
    - `fetch_browser_subtitle`(策略 ②)

    可选 override:
    - `fetch_via_recording`(策略 ③)— base 提供 FR-2.14 v3 默认实现
      (path ① yt-dlp → path ② Tab Audio Recorder → None,Q7 Silent fallback)。
    """

    @classmethod
    def match(cls, url: str) -> bool:
        """该 adapter 能否处理此 URL。"""
        raise NotImplementedError

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """策略 ①:平台公开 API(httpx)。"""
        raise NotImplementedError

    def fetch_browser_subtitle(
        self, driver: Any, url: str
    ) -> tuple[str, dict] | None:
        """策略 ②:Puppeteer 通用 JS 探测。driver 是 playwright Browser 实例。"""
        raise NotImplementedError

    def fetch_via_recording(
        self,
        driver: Any,
        url: str,
        duration_sec: int,
        *,
        audio_factory: "AudioSourceFactory",
        tab_recorder: "TabAudioRecorder",
        transcriber: "AudioTranscriber",
        screenshot_controller: "ScreenshotPhaseController | None" = None,
    ) -> tuple[str, dict] | None:
        """FR-2.14 v3 默认实现。

        路径 ① yt-dlp(`audio_factory.is_downloadable` → `extract` → transcribe):
          - 命中 → cleanup audio_path,return (text, {"via": "yt-dlp", ...})
          - 失败(Exception)→ 静默 log.debug,自动 fallback 路径 ②(Q7 Silent)
        路径 ② Tab Audio Recorder(`probe_status` → `start_recording` → `click_download`
          → transcribe):仅路径 ① miss 时触发。
          - screenshot_controller 不为 None 时同步触发 FR-2.28 PHASE A/B/C/D。
        全部失败 → return None。
        """
        # ---- path ①:yt-dlp 抽音频 ----
        try:
            if audio_factory.is_downloadable(url):
                result = audio_factory.extract(url, stem=self._make_stem(url))
                text = transcriber.transcribe(result.audio_path)
                transcriber.cleanup(result.audio_path)
                _log.info("✓ 策略 ③ 命中 (path ① yt-dlp): %s", url)
                return text, {
                    "via": "yt-dlp",
                    "audio_path": str(result.audio_path),
                }
        except Exception as e:  # noqa: BLE001 — Q7 Silent:any failure → fallback path ②
            # Q7 Silent:不 log.warning,只 debug
            _log.debug("path ① yt-dlp 失败,自动 fallback 到 path ②: %s", e)

        # ---- path ②:Tab Audio Recorder ----
        try:
            status = asyncio.run(tab_recorder.probe_status(driver))
            if status != "enabled":
                _log.debug("path ② Tab Audio Recorder unavailable: status=%s", status)
                return None

            audio_id = asyncio.run(
                tab_recorder.start_recording(driver, url, duration_sec)
            )
            # FR-2.28 PHASE A(仅 path ② 触发)
            start_ts = 0.0
            if screenshot_controller is not None:
                page = getattr(driver, "page", driver)
                start_ts = asyncio.run(
                    screenshot_controller.phase_a_start(page, audio_id)
                )

            audio_path = asyncio.run(tab_recorder.click_download(driver, audio_id))

            # PHASE B+C(并发,顺序触发)
            if screenshot_controller is not None:
                page = getattr(driver, "page", driver)
                asyncio.run(
                    screenshot_controller.phase_b_then_c(page, audio_id, duration_sec)
                )

            text = transcriber.transcribe(audio_path)
            transcriber.cleanup(audio_path)
            _log.info("✓ 策略 ③ 命中 (path ② tab_recorder): %s", url)

            # FR-2.28 PHASE D
            if screenshot_controller is not None:
                end_ts = 0.0  # 简化:实际从 phase_b_then_c 返回值拿
                screenshot_controller.phase_d_write_index(
                    audio_id, start_ts, end_ts, duration_sec, partial_flags=[]
                )
            return text, {"via": "tab_recorder", "audio_id": audio_id}
        except Exception as e:
            _log.error("path ② 也失败: %s", e)
            return None

    def _make_stem(self, url: str) -> str:
        """url → 文件 stem。

        BilibiliAdapter 可 override 用 bvid;base 默认用 url hash(16 位 hex)。
        """
        return hashlib.md5(url.encode()).hexdigest()[:16]


class PlatformAdapterRegistry:
    """平台适配器注册表。

    支持两种注册方式(2026-09-02 扩展):
    - `register(adapter_cls)` — 无依赖 adapter,用 class 注册;每次 get_for_url
      返回新实例(状态隔离,测试友好)。
    - `register_instance(adapter)` — 带依赖 adapter(如 BilibiliAdapter 需要
      `official` 和 `recorder`),用 pre-built 实例注册,get_for_url 直接返回
      这个实例(共享 deps,每次同一对象)。

    实例优先于类匹配(同一 URL 实例先命中,再 fallback 到 class)。
    """

    def __init__(self) -> None:
        self._classes: list[type] = []
        self._instances: list[Any] = []

    def register(self, adapter_cls: type) -> None:
        """注册一个 adapter 类;重复注册同一类会被忽略。"""
        if adapter_cls not in self._classes:
            self._classes.append(adapter_cls)

    def register_instance(self, adapter: Any) -> None:
        """注册一个 pre-built adapter 实例(带 deps 的 adapter 用这个)。"""
        # 不去重 — 调用方应自己保证不重复;实例共享 deps 是预期行为
        self._instances.append(adapter)

    def list_adapters(self) -> list[type]:
        """返回所有已注册的 adapter 类(注册顺序)。

        注:实例不暴露在这里(legacy 兼容 — 旧测试只看 class list)。
        """
        return list(self._classes)

    def list_instances(self) -> list[Any]:
        """返回所有已注册的 adapter 实例(注册顺序,2026-09-02 新增)。"""
        return list(self._instances)

    def get_for_url(self, url: str) -> Any | None:
        """按注册顺序找首个匹配 URL 的 adapter。

        实例优先匹配;类匹配命中时返回新实例(避免状态污染)。

        None 表示无匹配 → 调用方应降级或跳过策略 ①。
        """
        # 1. 实例优先(pre-built,带 deps)
        for inst in self._instances:
            if inst.match(url):
                return inst
        # 2. 类(无 deps,每次新建)
        for cls in self._classes:
            if cls.match(url):
                return cls()
        return None
