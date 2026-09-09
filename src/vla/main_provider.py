"""Real text_provider 装配(SSOT: requirements.md 第七章 数据流 + Phase 9 集成)。

职责:
- 封装"字幕三级策略 → 视频源兜底 → Whisper 转写"完整链路
- 提供给 VideoLearningAgent 作为 text_provider 注入
- 处理真实环境中的副作用:临时目录管理、字幕/视频/音频文件清理

数据流:
    task → strategy.get_subtitle(url, duration_sec)
              ├── SubtitleResult(source="api"|"browser"|"whisper") → (text, source, None)
              └── None
                  ↓
                  source_factory.get(url, video_id, expected_duration)
                      ├── VideoSource(s) (mode="download"|"record")
                      └── transcriber.transcribe(source.path)
                              ├── FR-3.3:删视频源
                              └── return text
                          → (text, "whisper", audio_path)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from vla.config import VLAConfig
from vla.log.transcription_log import TranscriptionLog
from vla.models import Asset, ProcessResult, VideoTask
from vla.subtitle import audio_scan
from vla.transcribe.extract import extract_audio


logger = logging.getLogger(__name__)


# Task 9 SSOT(2026-09-09 asset-pipeline-refactor):
# build_text_provider 不再返回 RealTextProvider 实例,
# 而是返回 (fetch_asset, process_asset) 两个 callable。
# 调用方按需 await,中间可插入 main.py 的 step(quota / log / 状态)。
FetchAssetFn = Callable[["VideoTask"], Awaitable["Asset | None"]]
ProcessAssetFn = Callable[["Asset", "VideoTask"], Awaitable["ProcessResult | None"]]


class RealTextProvider:
    """真实环境的 text_provider 装配(Phase 9 完整集成)。"""

    def __init__(
        self,
        cfg: VLAConfig,
        strategy: Any,
        source_factory: Any,
        transcriber: Any,
        notifier: Any,
        plugin_status: Any,
        save_dir: Path | None = None,
        log: TranscriptionLog | None = None,
        checker: Any | None = None,
        refiner: Any | None = None,
        today_dir: Path | None = None,
    ) -> None:
        """
        Args:
            cfg: VLAConfig
            strategy: SubtitleStrategy(必填 — FR-2.5/2.6 popup 流程)
            source_factory: VideoSourceFactory(必填)
            transcriber: StreamingTranscriber(必填)
            notifier: MacOSNotifier(必填 — 弹窗)
            plugin_status: PluginStatus(必填 — session 单例)
            save_dir: 临时文件目录
            log: TranscriptionLog(可选,默认从 cfg.logging.log_dir 构造)
            checker: QualityChecker(可选,process_asset 质量门控用)
            refiner: SubtitleRefiner(可选,process_asset Refine 步骤用)
            today_dir: 用户手动下载音频的今日目录(F2-10 scan_today_dir 路径
                       §4.2 ④ 用;build_text_provider 自动从 cfg.audio.downloads_dir
                       计算,测试 fixture 也可手动注入,None 时 fetch_asset 路径 ④
                       仍走 audio_scan 但目录需由调用方保证存在)
        """
        self.cfg = cfg
        self.strategy = strategy
        self.source_factory = source_factory
        self.transcriber = transcriber
        self.notifier = notifier
        self.plugin_status = plugin_status
        self._save_dir = Path(save_dir) if save_dir else Path("./tmp")
        self.log = log or TranscriptionLog(cfg.logging.log_dir)
        self.checker = checker
        self.refiner = refiner
        self._today_dir = today_dir

    async def fetch_asset(self, task: VideoTask) -> Asset | None:
        """输入链(Task 7 实装):4 路径回落,产出 Asset 或 None。

        顺序:
          ① 字幕三级策略命中 text → Asset(text=..., source, None, False)
          ② internal_spider(m3u8) → extract → Asset(whisper_internal_download, wav, True)
          ③ VideoSourceFactory(MP4) → extract → unlink → Asset(whisper_download, wav, True)
          ④ scan_today_dir(webm, last) → extract → Asset(whisper_scan, wav, True)
        """
        url = str(task.url)
        duration_sec = task.expected_duration

        # 1. 字幕策略
        try:
            result = await self.strategy.get_subtitle(url, duration_sec)
        except Exception as e:
            logger.warning("策略调用异常,降级到 internal_spider: %s", e)
            result = None

        if result is not None and result.text is not None:
            return Asset(text=result.text, source=result.source, audio_path=None, deletable=False)

        # 2. internal_spider
        if result is not None and (result.source or "").startswith("internal"):
            video_url = (result.metadata or {}).get("video_url")
            if not video_url:
                logger.warning("internal spider 返回无 video_url,跳过: %s", result)
                return None
            wav_path = self._save_dir / "audio_raw" / f"{task.id}.wav"
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                extract_audio(Path(video_url), wav_path)
            except Exception as e:
                logger.warning("internal spider m3u8 抽音失败 %s: %s", video_url, e)
                return None
            return Asset(text=None, source="whisper_internal_download", audio_path=wav_path, deletable=True)

        # 3. VideoSourceFactory
        try:
            source = self.source_factory.get(url, task.id, duration_sec)
        except Exception as e:
            logger.warning("source_factory.get 失败: %s", e)
            source = None

        if source is not None:
            video_path = source.path
            wav_path = video_path.with_suffix(".wav")
            try:
                extract_audio(video_path, wav_path)
            except Exception as e:
                logger.warning("MP4 抽音失败 %s: %s", video_path, e)
            else:
                try:
                    video_path.unlink()
                except Exception as e:
                    logger.warning("unlink MP4 失败 %s: %s", video_path, e)
                return Asset(text=None, source="whisper_download", audio_path=wav_path, deletable=True)

        # 4. scan_today_dir (last)
        try:
            webm_path = audio_scan.scan_untranscribed_audio(self._today_dir)
        except Exception as e:
            logger.warning("scan_today_dir 失败: %s", e)
            return None

        if webm_path is None:
            return None
        wav_path = webm_path.with_suffix(".wav")
        try:
            extract_audio(webm_path, wav_path)
        except Exception as e:
            logger.warning("scan webm 抽音失败 %s: %s", webm_path, e)
            return None
        return Asset(text=None, source="whisper_scan", audio_path=wav_path, deletable=True)

    async def process_asset(self, asset: Asset, task: VideoTask) -> ProcessResult | None:
        """处理链(Task 8 实装):6 步骤。

        步骤:
          ① 转写(if needs_transcribe) → 失败 log + return None
          ② 质量门控 → 失败 log + 不 unlink(FR-3.7 v3.2 retry 保留) + browser 源 mark_unavailable
          ③ Refine(可选,cfg.quality_check.refine_enabled) → 失败 log warning + 用原文
          ④ save_transcribed(落盘 transcribed/)
          ⑤ cleanup:unlink wav if deletable(best-effort)
          ⑥ return ProcessResult
        """
        # Step 1: 转写
        if asset.needs_transcribe:
            try:
                text = await asyncio.to_thread(self.transcriber.transcribe, asset.audio_path)
            except Exception as e:
                self.log.log_transcribe_fail(
                    task.id, task.title, str(task.url),
                    stage="transcribe", error=str(e),
                )
                return None
            # scan 路径写 sidecar,避免下次又被扫到(FR-2.10)
            if asset.source == "whisper_scan" and asset.audio_path is not None:
                try:
                    asset.audio_path.with_suffix(".transcribed.txt").touch()
                except Exception as e:
                    logger.warning("touch sidecar 失败 %s: %s", asset.audio_path, e)
        else:
            text = asset.text

        # Step 2: 质量门控
        qr = self.checker.check(
            text=text, title=task.title,
            duration_sec=task.expected_duration,
            model_size=self.cfg.whisper.model,
        )

        # Step 3: 质量失败分支
        if not qr.passed:
            self.log.log_quality_fail(task.id, task.title, str(task.url), qr, text)
            if asset.source == "browser":
                self.plugin_status.mark_unavailable(reason="plugin_quality_fail")
            return None

        # Step 4: Refine(可选,云端 LLM 语义清理)
        if self.cfg.quality_check.refine_enabled and self.refiner is not None:
            try:
                refinement = self.refiner.refine(text, title=task.title)
                if refinement.cleaned_text:
                    text = refinement.cleaned_text
            except Exception as e:
                logger.warning("Refine 失败,使用原文继续: %s", e)

        # Step 5: 落盘 transcribed/<id>_<title>.txt(供 Phase 7 总结读)
        self.log.save_transcribed(
            video_id=task.id, title=task.title, text=text,
            quality=qr, source=asset.source,
            duration_sec=task.expected_duration,
        )

        # Step 6: 清理 wav(best-effort)
        if asset.deletable and asset.audio_path is not None and asset.audio_path.exists():
            try:
                asset.audio_path.unlink()
            except Exception as e:
                logger.warning("删音频失败 %s,主流程继续: %s", asset.audio_path, e)

        return ProcessResult(
            text=text, qr=qr,
            source=asset.source,
            duration_sec=task.expected_duration,
        )

    async def __call__(self, task: VideoTask) -> tuple[Asset | None, ProcessResult | None]:
        asset = await self.fetch_asset(task)
        if asset is None:
            return None, None
        result = await self.process_asset(asset, task)
        return asset, result


def build_text_provider(
    cfg: VLAConfig,
    transcriber: Any | None = None,
    notifier: Any = None,
    *,
    plugin_status: Any = None,
    save_dir: Path | None = None,
    driver: Any = None,
    recorder: Any | None = None,  # F2-8:deprecated,保留以兼容老调用方(始终 None)
    log: TranscriptionLog | None = None,
    checker: Any | None = None,
    refiner: Any | None = None,
    strategy: Any | None = None,
) -> tuple[FetchAssetFn, ProcessAssetFn]:
    """工厂函数:装配一个完整的 RealTextProvider,返回 (fetch_asset, process_asset) 两个 callable。

    Task 9(2026-09-09 asset-pipeline-refactor):返回二元组,调用方按需 await,
    中间可插入 quota 检查 / 日志 / 状态更新。

    Task 12(2026-09-09 spike 装配):新增 strategy/log/checker/refiner 可选 kwarg,
    允许 spike 在传入预构造 strategy 时复用其内部 adapter / recorder,以便
    monkey-patch 命中真实组件。

    Args:
        cfg: VLAConfig(若 cfg.audio 存在,装配时会自动算 today_dir = find_today_dir(
             cfg.audio.downloads_dir) 并注入 provider)
        transcriber: StreamingTranscriber(可选 — 测试 fixture 注入 MagicMock;
                  None 时内部 auto-create)
        notifier: MacOSNotifier(必填 — 弹窗)
        plugin_status: PluginStatus(可选 — cli.py 生产路径必传,测试 stub 可省)
        save_dir: 临时文件目录(默认 cfg.storage.tmp_dir)
        driver: BrowserDriver(可选,字幕策略需要)
        recorder: deprecated(F2-8:旧 Screen Recorder 已删,传参保留但运行时忽略)
        log: TranscriptionLog(可选,默认从 cfg.logging.log_dir 构造)
        checker: QualityChecker(可选,spike/装配时注入;None 时自动构造
                 `QualityChecker(cfg)`,供 process_asset 质量门控)
        refiner: SubtitleRefiner(可选,spike/装配时注入;None 且
                 `cfg.quality_check.refine_enabled=True` 时自动构造
                 `SubtitleRefiner(cfg)`,否则保持 None — 避免无谓 LLM 客户端浪费)
        strategy: SubtitleStrategy(可选 — spike 注入预构造的 strategy,内部
                  audio_factory / tab_recorder / bilibili_adapter 等组件可被
                  monkey-patch 复用;None 时内部 auto-construct)
                  装配后 today_dir 会被算出来传给 RealTextProvider(若 cfg.audio 不为 None),
                  fetch_asset 路径 ④ scan_today_dir(§4.2 ④)才能跑通。

    Returns:
        (fetch_asset, process_asset):两个独立 callable,分别对应"取资产"和"处理资产"。
        fetch_asset: VideoTask → Asset | None
        process_asset: Asset, VideoTask → ProcessResult | None
    """
    from vla.audio.source_factory import AudioSourceFactory
    from vla.source.video_source import VideoSourceFactory
    from vla.subtitle.audio_scan import find_today_dir
    from vla.subtitle.strategy import SubtitleStrategy
    from vla.subtitle.tab_audio_recorder import TabAudioRecorder
    from vla.transcribe.streaming import StreamingTranscriber

    save_dir = Path(save_dir) if save_dir else Path(cfg.storage.tmp_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    # T14 (2026-09-09 asset-pipeline-refactor):算今天日期目录并注入 provider,
    # 让 fetch_asset 路径 ④ audio_scan.scan_untranscribed_audio(self._today_dir)
    # 在生产里不再 AttributeError。audio 为 None 时保持 None(向后兼容测试)。
    today_dir: Path | None = None
    if cfg.audio is not None:
        today_dir = find_today_dir(Path(cfg.audio.downloads_dir))

    log = log or TranscriptionLog(cfg.logging.log_dir)

    # T13 (2026-09-09 asset-pipeline-refactor):auto-construct QualityChecker +
    # SubtitleRefiner。修复 cli._build_real_provider 不传 checker / refiner 时
    # process_asset 在 self.checker.check(...) 处 AttributeError 的生产路径 bug
    # (spike 显式传了 checker / refiner 所以未暴露)。
    if checker is None:
        from vla.quality.checker import QualityChecker
        checker = QualityChecker(cfg)
    if refiner is None and cfg.quality_check.refine_enabled:
        from vla.quality.refiner import SubtitleRefiner
        refiner = SubtitleRefiner(cfg)

    source_factory = VideoSourceFactory(tmp_dir=save_dir, log=log, config=cfg)
    if transcriber is None:
        transcriber = StreamingTranscriber(cfg)

    if strategy is None:
        # F2-8:不再自动构造旧 Screen Recorder。弹窗 enabled 路径已废弃 —
        # 真实录屏兜底走策略 ③ adapter.fetch_via_recording(只剩 yt-dlp path ①)。
        # driver 仍按需自动连 Chrome CDP。
        if driver is None:
            driver = _try_connect_chrome(cfg, transcriber, notifier)

        audio_factory = AudioSourceFactory(save_dir=save_dir / "audio_raw")
        tab_recorder = TabAudioRecorder(
            match_keyword=getattr(
                getattr(cfg, "extension", None), "tab_audio_recorder.match_keyword", "tab audio",
            ),
            save_dir=save_dir / "audio_raw",
        )

        strategy = SubtitleStrategy(
            registry=_build_registry(cfg, save_dir=save_dir),
            driver=driver,
            recorder=recorder,  # 保留(测试 fixture 注入 MagicMock,enabled 路径 stub)
            notifier=notifier,
            plugin_status=plugin_status,
            remind_timeout_sec=cfg.browser_plugin.remind_timeout_sec,
            plugin_name=cfg.browser_plugin.name,
            audio_factory=audio_factory,
            tab_recorder=tab_recorder,
            transcriber=transcriber,
            save_dir=save_dir,
            cfg=cfg,  # F2-10:扫今天 YYYY-MM-DD/ 用
        )

    provider = RealTextProvider(
        cfg=cfg,
        strategy=strategy,
        source_factory=source_factory,
        transcriber=transcriber,
        notifier=notifier,
        plugin_status=plugin_status,
        save_dir=save_dir,
        log=log,
        checker=checker,
        refiner=refiner,
        today_dir=today_dir,
    )

    return provider.fetch_asset, provider.process_asset


def _build_registry(
    cfg: VLAConfig,
    *,
    save_dir: Path,
) -> Any:
    """装配 PlatformAdapterRegistry(2026-09-02 修复:之前一直是空的!)

    装配顺序:
    1. BilibiliAdapter(cfg.platforms.bilibili.enabled) — 实例注册(带 F2-10 2 deps)
    2. InternalSiteAdapter(cfg.platforms.internal_site.enabled) — 类注册(无 deps)

    B站 → 实例注册的原因:BilibiliAdapter 构造需要 `official`(B站官方 API 客户端)
    和 2 REQUIRED deps(audio_factory / transcriber),没法用 registry 默认的无参构造。

    **F2-10 (2026-09-08)**:tab_recorder / screenshot_controller 已从 BilibiliAdapter
    构造参数删除(分别由 strategy._try_browser 弹窗 enabled 分支和 main.py 的
    ScreenshotPhaseController 接管)。
    """
    from vla.audio.source_factory import AudioSourceFactory
    from vla.subtitle.bilibili_adapter import BilibiliAdapter
    from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
    from vla.subtitle.internal_site_adapter import InternalSiteAdapter
    from vla.subtitle.platform_adapter import PlatformAdapterRegistry
    from vla.transcribe.streaming import StreamingTranscriber

    registry = PlatformAdapterRegistry()

    if cfg.platforms.bilibili.enabled:
        official = BilibiliOfficialSubtitle()
        # F2-10:2 deps(audio_factory + transcriber);tab_recorder 改由 strategy 持有,
        # screenshot_controller 改由 main.py 持有。
        audio_factory = AudioSourceFactory(save_dir=save_dir / "audio_raw")
        transcriber = StreamingTranscriber(cfg)
        adapter = BilibiliAdapter(
            official=official,
            audio_factory=audio_factory,
            transcriber=transcriber,
        )
        registry.register_instance(adapter)
        logger.info(
            "✓ B站 adapter 已注册(official=%s)",
            type(official).__name__,
        )

    if cfg.platforms.internal_site.enabled:
        registry.register(InternalSiteAdapter)
        logger.info("✓ 内部网站 adapter 已注册(class)")

    return registry


def _try_connect_chrome(cfg, transcriber, notifier) -> Any:
    """尝试连本地 Chrome CDP(cfg.puppeteer.debugging_port),成功返回 driver。

    F2-8:不再构造旧 Screen Recorder(已删)。新架构下,driver 仅给策略 ②
    BrowserDriver.fetch_subtitle_via_browser 用;录屏兜底走策略 ③
    adapter.fetch_via_recording(audio_factory + tab_recorder)。

    失败(端口未监听 / playwright 未装 / connect 异常)→ 返回 None,
    调用方继续走 ffmpeg 兜底。
    """
    import socket

    port = cfg.puppeteer.debugging_port
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    except (OSError, ConnectionRefusedError):
        logger.info("Chrome CDP 端口 %d 未监听,跳过自动连接", port)
        return None

    try:
        from vla.subtitle.browser_driver import BrowserDriver
    except ImportError as e:
        logger.warning("导入 BrowserDriver 失败:%s", e)
        return None

    try:
        driver = BrowserDriver(cfg)
        driver.connect()
        logger.info(
            "✓ Chrome CDP 已连接 port=%d,driver 已就绪", port,
        )
        return driver
    except Exception as e:
        logger.warning("Chrome CDP 连接失败 port=%d:%s", port, e)
        return None


def default_probe_registry() -> Any:
    """组装默认的探针注册表(R-14 SSOT)。

    F2-8:Probes 暂时不再被旧 Screen Recorder 装配触发(该类已删);
    但 ProbeRegistry / 各 Probe 类仍保留,供后续 F2-14 ProbeStrategy 重构
    接入 PlatformAdapter.prefetch_url(预探测 URL 是否能拿到 cookie / referer)。

    顺序 = head → referer → cookie(SSOT:R-14 plan task 3)。
    新增平台探针 = 一个新类 + 一次 register(),不动已有逻辑。
    """
    from vla.subtitle.probe_strategy import ProbeRegistry
    from vla.subtitle.probes import (
        CookieWarmupProbe,
        HeadRequestProbe,
        RefererCheckProbe,
    )

    reg = ProbeRegistry()
    reg.register(HeadRequestProbe())
    reg.register(RefererCheckProbe())
    reg.register(CookieWarmupProbe())
    return reg