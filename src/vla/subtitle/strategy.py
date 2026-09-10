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

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vla.models import SubtitleResult

if TYPE_CHECKING:
    from vla.audio.source_factory import AudioSourceFactory
    from vla.capture.screenshot_phase_controller import ScreenshotPhaseController
    from vla.config import VLAConfig
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
    """无匹配平台 adapter 时使用,直接调 BrowserDriver / 自管 recorder。

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
        内部仍用旧 Screen Recorder 路径,F2-8 后保留为可注入 stub(测试用)。
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
        *,
        audio_factory: "AudioSourceFactory",
        transcriber: "AudioTranscriber",
        screenshot_controller: "ScreenshotPhaseController | None" = None,
        log: logging.Logger | None = None,
        save_dir: Path | None = None,
        cfg: "VLAConfig | None" = None,
    ) -> None:
        """
        Args:
            registry: PlatformAdapterRegistry
            driver: BrowserDriver(可选)
            recorder: 旧 Screen Recorder 桩(测试 fixture 注入 MagicMock;F2-8 后
                无生产实例,代码路径保留以兼容既有测试 + 弹窗 enabled 流程)
            notifier: NotifierLike(必填 — FR-2.5/2.6 弹窗;跨平台 NullNotifier/MacOSNotifier)
            plugin_status: PluginStatus(必填 — FR-2.9/2.10 session 单例)
            audio_factory: F2-7 必填 — 传给 adapter.fetch_via_recording path ①
            transcriber: F2-7 必填 — 传给 adapter.fetch_via_recording
            screenshot_controller: F2-7 可选 — FR-2.28 PHASE A/B/C/D 触发器
            log: logger
            save_dir: 录制目录
            cfg: F2-10 必填 — VLAConfig(cfg.audio.downloads_dir 用于扫今天 YYYY-MM-DD/)

        2026-09-10 轻量化:tab_recorder / remind_timeout_sec / plugin_name 已删除。
        Tab Audio Recorder 浏览器扩展依赖已移除,弹窗询问统一走 notifier.ask_open_browser
        (macOS 走 MacOSNotifier 真实弹窗,Windows/Linux 走 NullNotifier 返回 "skip")。
        """
        self.registry = registry
        self.driver = driver
        self.recorder = recorder
        self.notifier = notifier
        self.plugin_status = plugin_status
        # F2-7:deps 必填,get_subtitle 转发给 adapter.fetch_via_recording
        self.audio_factory = audio_factory
        self.transcriber = transcriber
        self.screenshot_controller = screenshot_controller
        self.log = log or logging.getLogger(__name__)
        self._save_dir = save_dir
        # F2-10:扫今天 YYYY-MM-DD/ 用 — 暂 None 容忍(老测试 + 兜底)
        self.cfg = cfg

    async def get_subtitle(
        self, url: str, duration_sec: int = 600
    ) -> SubtitleResult | None:
        """三级降级。任一命中即返回;全失败返回 None。

        每级 try/except 内调用 SubtitleResult(...):pydantic 校验失败
        (MagicMock / None 等异常值)被当作 miss,降级到下一级。

        Phase 9.6:在 ① 与 ② 之间增加一个"internal_spider"分支 — 仅当 adapter
        有 `fetch_via_spider` 方法时启用(默认 attribute lookup,duck typing;
        duck typing 让旧 adapter / 测试 fixture 不受影响)。命中后返回
        SubtitleResult(text=None, source="internal_spider", metadata={"video_url":
        m3u8_url, ...}),让 fetch_asset 路径 ② 走 extract_m3u8_audio 抽音。
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

        # ①-a InternalSiteSpider(Phase 9.6:bill-jc 域)
        # 仅当 adapter 有 fetch_via_spider 方法(duck typing)才走 — 其他
        # adapter / 测试 fixture 不受影响。
        spider_result = await self._try_internal_spider(adapter, url)
        if spider_result is not None:
            self.log.info("✓ 策略 ①-a 命中(internal_spider)")
            return spider_result

        # ② Browser(FR-2.5~2.8 字幕探测 + FR-2.14 Screen Recorder + FR-2.21 popup)
        # v3.2.1.6: _try_browser 内部 run_on_driver_thread + ask_open_browser 同步阻塞,
        # 直接 await 会冻死 asyncio loop(用户不响应弹窗 → 整个 session 挂起)。
        # 包 to_thread 隔离到 default executor,event loop 可继续。
        browser_result = await asyncio.to_thread(
            self._try_browser, adapter, url, duration_sec,
        )
        if browser_result is not None:
            text, meta = browser_result
            try:
                # FR-2.12 enum:source ∈ {api, browser, whisper}
                # - 字幕探测纯命中 → "browser"
                # - 录制路径 → "whisper"
                #   (metadata.via 区分具体路径)
                source = "whisper" if (
                    isinstance(meta, dict)
                    and meta.get("via") in ("screen_recorder", "tab_audio_recorder")
                ) else "browser"
                return SubtitleResult(
                    text=text, source=source, metadata=meta
                )
            except Exception as e:
                self.log.warning("策略 ② SubtitleResult 构造失败: %s", e)

        # ③ Recording(F2-7:传 4 REQUIRED kwargs 给 base impl)
        # v3.2.1.6: fetch_via_recording 内部可能调 run_on_driver_thread(同步阻塞),
        # 改为 to_thread 隔离到 default executor。
        try:
            # F2-10:tab_recorder / screenshot_controller 已从 PlatformAdapter.fetch_via_recording
            # 签名删除 — 不再传。scan 路径迁到 fetch_asset path ④ 兜底;
            # 截图由 main.py 直接调 ScreenshotPhaseController 触发。
            result = await asyncio.to_thread(
                adapter.fetch_via_recording,
                self.driver,
                url,
                duration_sec,
                audio_factory=self.audio_factory,
                transcriber=self.transcriber,
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

    async def _try_internal_spider(
        self, adapter: Any, url: str
    ) -> SubtitleResult | None:
        """Phase 9.6:让 adapter 调 InternalSiteSpider 拿 m3u8。

        duck typing:有 fetch_via_spider 方法且返回非 None 才走。方法内部
        已用 to_thread 把同步实现包装(隔离 event loop),此处直接 await。
        """
        fetch_fn = getattr(adapter, "fetch_via_spider", None)
        if fetch_fn is None:
            return None
        try:
            # adapter.fetch_via_spider 是同步函数(内部 asyncio.run 桥接),
            # 在 asyncio context 中需 to_thread 隔离 — 否则新 loop 与当前
            # loop 嵌套会报 "asyncio.run() cannot be called from a running
            # event loop"。
            result = await asyncio.to_thread(fetch_fn, url)
        except Exception as e:
            self.log.warning("策略 ①-a (internal_spider) 异常:%s", e)
            return None
        if result is None:
            return None
        # adapter 返回 (text, meta);内部约定 text=None + meta["video_url"]
        _, meta = result
        if not (isinstance(meta, dict) and meta.get("video_url")):
            self.log.warning(
                "策略 ①-a 返回 meta 缺 video_url:%s", meta,
            )
            return None
        return SubtitleResult(
            text=None, source="internal_spider", metadata=meta,
        )

    def _try_browser(
        self, adapter: Any, url: str, duration_sec: int
    ) -> tuple[str, dict] | None:
        """策略 ②:浏览器侧字幕探测 + Screen Recorder 录屏兜底(FR-2.21)。

        流程:
        1. FR-2.10:session 已标记 unavailable → 跳过 ②
        2. 第一次尝试 adapter.fetch_browser_subtitle(FR-2.5~2.8)
        2.5 FR-11.11:adapter.plugin_popup_enabled=False(内部站)→ 跳过弹窗,降级 ③
        3. miss → 暂停页面视频 + 弹 A 级 dialog 询问用户是否已开启 Screen Recorder
        4. 用户响应:
           - "enabled" → 调已注入 recorder 的 record_and_transcribe(FR-2.14 的 stub)
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

        # 2.5 平台闸门(FR-11.11,2026-09-10):不依赖浏览器插件的平台(内部站)
        # 直接降级 ③。弹窗问的是"是否已开启字幕插件",内部站没有插件可开 ——
        # 用户无法给出有意义的回答,只会白等 timeout 再降级。
        # 探测(2.)照跑:未来内部站若有 DOM 字幕仍能命中,只是不再弹窗。
        if not getattr(adapter, "plugin_popup_enabled", True):
            self.log.info(
                "adapter 不依赖浏览器插件(plugin_popup_enabled=False),跳过策略 ② 弹窗,降级 ③",
            )
            return None

        # 3. 暂停视频 + 弹窗(若 driver 为 None,跳过 pause 但仍弹窗)
        # v3.2.1.9 (2026-09-08):复用用户已开 B站 tab,不开新空白页 — 同 Phase A/C 思路
        page = self._new_page_safely(url)
        if page is not None:
            from vla.subtitle.page_control import pause_page_video
            # v3.2.1.3: page.evaluate 必须在 dispatcher thread;driver 提供
            # run_on_driver_thread wrapper,避免 greenlet mismatch。
            try:
                self.driver.run_on_driver_thread(pause_page_video, page)
            except Exception as e:
                self.log.warning("暂停页面视频失败(best-effort):%s", e)

        self.log.info(
            "策略 ② 第一次未拿到字幕,触发弹窗询问用户开启字幕/录制方案",
        )
        response = self.notifier.ask_open_browser(
            url=url,
            plugin_name="字幕",  # 2026-09-10:TabAudioRecorder 已删,弹窗文案简化
            timeout_sec=30,
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

        # 5. "enabled" → T4 委派给 fetch_asset (T7) 接管扫今天目录与转写
        # 本分支不再调 scan_untranscribed_audio / transcriber.transcribe / 抽音 / sidecar touch
        # (Ruling 1 from progress.md: 消除 T4 与 T7 之间的 double-scan 风险。
        #  T7 path ④ 是唯一扫今天目录的地方;plugin_status.mark_available 也留给 fetch_asset。)
        if page is not None:
            _safe_close_page(page)  # 弹窗后 page 用完,关掉释放
        self.log.info(
            "弹窗 enabled → 返回 None,扫今天目录与转写由 fetch_asset path ④ 接管"
        )
        return None

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

    def _new_page_safely(self, url: str) -> Any | None:
        """v3.2.1.9 (2026-09-08):复用用户已开的 B站 tab 来 pause video(不再开新空白页)。

        旧实现调 driver.new_background_page() 开空白 tab — 跟 Phase A/C v3.2.1.8 修复的
        "开了新空白页tab" 抱怨同源。修法:跟 Phase A 一致,从 url 提 bvid,用
        find_page_by_url_substring 复用现有 tab;找不到 → return None
        (popup 仍会弹,但跳过 pause video — 用户已手动录屏后,暂停不重要)。

        设计动机:
        - 用户已经在 Chrome 打开 B站(他自己按播放);popup 期间我们不应再加 tab
        - 复用现有 tab 可避免 Chrome memory saver 关闭 background tab 的 race
        - 跟 Phase A 统一路径,降低认知成本

        Args:
            url: 视频 URL(用来提 bvid 找 user tab)

        Returns:
            已开 B站 tab 的 page 对象;找不到 / driver=None / URL 无 bvid → None
        """
        if self.driver is None:
            return None
        import re
        bvid_m = re.search(r"([Bb][Vv][A-Za-z0-9]+)", url)
        if not bvid_m:
            self.log.warning(
                "_new_page_safely: URL 不含 bvid (%s) → 返回 None", url,
            )
            return None
        bvid = bvid_m.group(1)
        try:
            return self.driver.find_page_by_url_substring(bvid)
        except Exception as e:
            self.log.warning(
                "_new_page_safely: find_page_by_url_substring 失败(不影响 popup):%s", e,
            )
            return None