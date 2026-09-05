"""字幕策略编排器(SSOT: requirements.md FR-2.5/2.6/2.8/2.9/2.10 + Phase 3.5)。

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
         │              ├── plugin_status.unavailable → 跳过(FR-2.10)
         │              ├── 第一次尝试返回 None → 暂停视频 + 弹窗(FR-2.5/2.6)
         │              │     ├── "已开启" → retry ② → 命中返回
         │              │     ├── "跳过该视频" → mark_unavailable + 跳过 ②
         │              │     └── "timeout" → mark_unavailable + 跳过 ②
         │              miss ↓
         │              ③ fetch_via_recording(driver, url, duration_sec)
         │              miss → return None
         │
         └── None ─→ FallbackAdapter(直接用 driver / recorder,跳 ①)

返回 SubtitleResult(source='api'|'browser'|'whisper') 或 None(全失败)。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vla.models import SubtitleResult

if TYPE_CHECKING:
    from vla.audio.source_factory import AudioSourceFactory
    from vla.capture.screenshot_phase_controller import ScreenshotPhaseController
    from vla.subtitle.tab_audio_recorder import TabAudioRecorder
    from vla.transcribe.streaming import AudioTranscriber


logger = logging.getLogger(__name__)
_DEFAULT_RECORDING_DIR = Path("./tmp/recordings")


def _safe_close_page(page: Any) -> None:
    """R-15 page lifecycle:关闭 background page(防御性 swallow)。

    为什么需要:page 可能已被外部 close / Chrome memory saver unload /
    popup 副作用关掉。调用方不应被这种异常打挂 — 我们只是清理资源,
    报错就 log debug 继续。

    Args:
        page: playwright sync Page 或 FakePage(测试);为 None 时 no-op。
    """
    if page is None:
        return
    try:
        page.close()
    except Exception as e:
        logger.debug("close page 失败(已被外部关闭?):%s", e)


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
        # 复用策略 ② first-try 已加载 URL 的 page(B站页面)。
        # 没它,③ 兜底会开空白页,Screen Recorder 录不到音频。
        self._video_page: Any = None

    def set_video_page(self, page: Any) -> None:
        """策略 ② 加载完 B站的 page → ③ 兜底复用。"""
        self._video_page = page

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """无 API → 直接 miss。"""
        return None

    def fetch_browser_subtitle(
        self, driver: Any, url: str
    ) -> tuple[str, dict] | None:
        page = self.driver.new_background_page()
        text, meta = self.driver.fetch_subtitle_via_browser(page, url)
        if text is None:
            # 探测失败:把 page 保留,留给 ③ 录屏复用(避免再开空白页录不到音频)
            # 注意:fetch_subtitle_via_browser 在 finally 已 close page → 这里只能 None
            self._video_page = None
            return None
        return text, {**(meta or {}), "platform": "fallback"}

    def fetch_via_recording(
        self,
        driver: Any,
        url: str,
        duration_sec: int,
        **_kwargs: Any,
    ) -> tuple[str, dict] | None:
        """F2-7:接受 **kwargs(由 strategy 传 4 deps)但忽略 —— FallbackAdapter
        内部仍用旧 BrowserRecorder 路径,F2-8 才统一删。
        """
        if self.recorder is None:
            return None
        # 优先复用策略 ② 已开过 URL 的 page(避免空白页录音频为静音)
        # FallbackAdapter 自己 fetch_browser_subtitle 会 close page,所以这里通常拿不到;
        # 真要复用得在策略里改 fetch_subtitle_via_browser 的 close 行为,见 strategy._try_browser。
        page = self._video_page
        if page is None:
            page = self.driver.new_background_page()
            # 兜底:把空白页导航到 URL,Screen Recorder 才能录到 B站 tab 音频
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                logger.info(
                    "③ 兜底:已导航 page 到 %s,等真实 tab 音频", url
                )
            except Exception as e:
                logger.warning(
                    "③ 兜底 goto %s 失败(仍继续录屏,可能录到静音):%s",
                    url, e,
                )

        # Fix 3:跳转后立即暂停视频 — 消除录屏启动和视频播放的时间差
        # (page.goto 后 video 可能自动播;不暂停则 recorder 已经 wait pre_grace,
        # 但用户手动按 hotkey + click Play 还要花时间,导致 duration 计时偏掉)
        try:
            from vla.subtitle.page_control import pause_page_video
            pause_page_video(page)
        except Exception as e:
            logger.warning("③ 兜底 pause_page_video 失败:%s", e)

        # recorder 返回 transcript 文件路径(Path)— 不在内存持文本(用户新规)
        try:
            transcript_path = self.recorder.record_and_transcribe(
                page, url, duration_sec, self._save_dir
            )
        except Exception as e:
            logger.warning(
                "③ 兜底录屏失败(FallbackAdapter):%s", e
            )
            # R-15:caller owns page lifecycle;本函数新建的 page 必须关
            # (复用 self._video_page 时不在此关 — set_video_page 注入方负责)
            if self._video_page is None:
                _safe_close_page(page)
            return None

        # R-15:caller owns page lifecycle
        if self._video_page is None:
            _safe_close_page(page)

        # 读一次供 SubtitleResult.text
        try:
            text = Path(transcript_path).read_text(encoding="utf-8")
        except OSError as e:
            logger.error(
                "③ 兜底读 transcript 失败 %s:%s",
                transcript_path, e,
            )
            return None
        return text, {
            "method": "recording",
            "platform": "fallback",
            "transcript_path": str(transcript_path),
        }


class SubtitleStrategy:
    """三级字幕策略编排器(平台无关 + FR-2.5/2.6 popup 流程)。"""

    def __init__(
        self,
        registry: Any,
        driver: Any,
        recorder: Any,
        notifier: Any,
        plugin_status: Any,
        remind_timeout_sec: int,
        *,
        audio_factory: "AudioSourceFactory",
        tab_recorder: "TabAudioRecorder",
        transcriber: "AudioTranscriber",
        screenshot_controller: "ScreenshotPhaseController | None" = None,
        plugin_name: str = "VideoTrans",
        log: logging.Logger | None = None,
        save_dir: Path | None = None,
    ) -> None:
        """
        Args:
            registry: PlatformAdapterRegistry
            driver: BrowserDriver(可选)
            recorder: BrowserRecorder(F2-8 才删,目前仍需给 _try_browser 旧路径用)
            notifier: MacOSNotifier(必填 — FR-2.5/2.6 弹窗)
            plugin_status: PluginStatus(必填 — FR-2.9/2.10 session 单例)
            remind_timeout_sec: 弹窗超时(秒),默认 30
            audio_factory: F2-7 必填 — 传给 adapter.fetch_via_recording path ①
            tab_recriber: F2-7 必填 — 传给 adapter.fetch_via_recording path ②
            transcriber: F2-7 必填 — 传给 adapter.fetch_via_recording
            screenshot_controller: F2-7 可选 — FR-2.28 PHASE A/B/C/D 触发器
            plugin_name: 弹窗里展示的插件名
            log: logger
            save_dir: 录制目录
        """
        self.registry = registry
        self.driver = driver
        self.recorder = recorder
        self.notifier = notifier
        self.plugin_status = plugin_status
        self.remind_timeout_sec = remind_timeout_sec
        # F2-7:4 deps 必填,get_subtitle 转发给 adapter.fetch_via_recording
        self.audio_factory = audio_factory
        self.tab_recorder = tab_recorder
        self.transcriber = transcriber
        self.screenshot_controller = screenshot_controller
        self.plugin_name = plugin_name
        self.log = log or logging.getLogger(__name__)
        self._save_dir = save_dir

    def get_subtitle(
        self, url: str, duration_sec: int = 600
    ) -> SubtitleResult | None:
        """三级降级。任一命中即返回;全失败返回 None。

        每级 try/except 内调用 SubtitleResult(...):pydantic 校验失败
        (MagicMock / None 等异常值)被当作 miss,降级到下一级。
        """
        adapter = self._pick_adapter(url)

        # ① API
        try:
            result = adapter.fetch_api_subtitle(url)
            if result:
                text, meta = result
                self.log.info("✓ 策略 ① 命中(API)")
                return SubtitleResult(
                    text=text, source="api", metadata=meta
                )
        except Exception as e:
            self.log.warning("策略 ① 失败: %s", e)

        # ② Browser(FR-2.5~2.8 字幕探测 + FR-2.14 Screen Recorder + FR-2.21 popup)
        browser_result = self._try_browser(adapter, url, duration_sec)
        if browser_result is not None:
            text, meta = browser_result
            try:
                # FR-2.12 enum:source ∈ {api, browser, whisper}
                # - 字幕探测纯命中 → "browser"
                # - Screen Recorder + Whisper → "whisper"(metadata.via 区分录制路径)
                source = "whisper" if (
                    isinstance(meta, dict) and meta.get("via") == "screen_recorder"
                ) else "browser"
                return SubtitleResult(
                    text=text, source=source, metadata=meta
                )
            except Exception as e:
                self.log.warning("策略 ② SubtitleResult 构造失败: %s", e)

        # ③ Recording(F2-7:传 4 REQUIRED kwargs 给 base impl)
        try:
            result = adapter.fetch_via_recording(
                self.driver,
                url,
                duration_sec,
                audio_factory=self.audio_factory,
                tab_recorder=self.tab_recorder,
                transcriber=self.transcriber,
                screenshot_controller=self.screenshot_controller,
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

    # ---------------- 内部步骤 ----------------

    def _pick_adapter(self, url: str) -> Any:
        adapter = self.registry.get_for_url(url) if self.registry else None
        if adapter is None:
            self.log.warning("无匹配 adapter,使用 FallbackAdapter: %s", url)
            adapter = FallbackAdapter(self.driver, self.recorder, self._save_dir)
        return adapter

    def _try_browser(
        self, adapter: Any, url: str, duration_sec: int
    ) -> tuple[str, dict] | None:
        """策略 ②:浏览器侧字幕探测 + Screen Recorder 录屏兜底(FR-2.21)。

        流程:
        1. FR-2.10:session 已标记 unavailable → 跳过 ②
        2. 第一次尝试 adapter.fetch_browser_subtitle(FR-2.5~2.8)
        3. miss → 暂停页面视频 + 弹 A 级 dialog 询问用户是否已开启 Screen Recorder
        4. 用户响应:
           - "enabled" → 调 BrowserRecorder.record_and_transcribe(FR-2.14)
             · 成功 → mark_available + 返回 text,meta={"via": "screen_recorder"}
             · 抛错 → 不 mark_unavailable,降级策略 ③(ffmpeg 兜底,FR-2.20)
           - "skip"    → mark_unavailable(user_skip) + return None
           - "timeout" → mark_unavailable(popup_timeout) + return None
                       (notifier 内部已发 B 级 warning 通知用户,FR-2.21)
        """
        # 1. session 单例:不可用 → 跳过
        if self.plugin_status.is_unavailable():
            self.log.info("插件已标记 unavailable,跳过策略 ②")
            return None

        # 2. 第一次尝试字幕探测
        first = self._fetch_browser_once(adapter, url, label="第一次")
        if first is not None:
            return first

        # 3. 暂停视频 + 弹窗(若 driver 为 None,跳过 pause 但仍弹窗)
        page = self._new_page_safely()
        if page is not None:
            from vla.subtitle.page_control import pause_page_video
            pause_page_video(page)

        self.log.info(
            "策略 ② 第一次未拿到字幕,触发弹窗询问用户开启 Screen Recorder"
        )
        response = self.notifier.ask_open_browser(
            url=url,
            plugin_name=self.plugin_name,
            timeout_sec=self.remind_timeout_sec,
        )

        # 4. 处理响应
        if response == "skip":
            self.plugin_status.mark_unavailable(reason="user_skip")
            self.log.info("用户跳过该视频,降级到策略 ③")
            return None

        if response == "timeout":
            self.plugin_status.mark_unavailable(reason="popup_timeout")
            self.log.info("弹窗超时未响应,降级到策略 ③")
            return None

        # 5. "enabled" → 触发 Screen Recorder(FR-2.14/2.15)
        if self.recorder is None or page is None:
            self.log.warning(
                "未注入 recorder / 无法创建 page,降级策略 ③(ffmpeg)"
            )
            return None

        self.log.info("用户已开启插件,触发 Screen Recorder 录屏 → Whisper")
        try:
            # recorder 返回转写文本的**文件路径**(Path),不是文本本身(用户新规:不存内存)
            transcript_path = self.recorder.record_and_transcribe(
                page, url, duration_sec, self._save_dir,
            )
        except Exception as e:
            # recorder 超时 / 抛错 → 不标记 unavailable,降级 ffmpeg(FR-2.20)
            self.log.warning(
                "Screen Recorder 录屏失败,降级策略 ③(ffmpeg):%s", e
            )
            _safe_close_page(page)
            return None

        # R-15:page lifecycle 由 caller(SubtitleStrategy)拥有 —
        # recorder 不再 close,我们在 finally 兜底关。
        _safe_close_page(page)

        # 读一次供 SubtitleResult.text 用(metadata 仍保留 transcript_path 给下游)
        try:
            text = Path(transcript_path).read_text(encoding="utf-8")
        except OSError as e:
            self.log.error(
                "读 transcript 失败 %s:%s — 转写文件可能丢失",
                transcript_path, e,
            )
            return None

        if not self.plugin_status.is_known():
            self.plugin_status.mark_available()
        return text, {
            "via": "screen_recorder",
            "method": "recording",
            "transcript_path": str(transcript_path),
        }

    def _fetch_browser_once(
        self, adapter: Any, url: str, *, label: str
    ) -> tuple[str, dict] | None:
        try:
            result = adapter.fetch_browser_subtitle(self.driver, url)
            if result:
                # 解包(text, meta)— MagicMock 在此会抛 ValueError,被外层 try 捕获
                text, meta = result
                self.log.info("✓ 策略 ② 命中(browser, %s)", label)
                return text, meta
        except Exception as e:
            self.log.warning("策略 ② %s 失败: %s", label, e)
        return None

    def _new_page_safely(self) -> Any:
        """开一个 background page 用来 pause video(若 driver=None 则返回 None)。"""
        if self.driver is None:
            return None
        try:
            return self.driver.new_background_page()
        except Exception as e:
            self.log.warning("new_background_page 失败(不影响 popup):%s", e)
            return None