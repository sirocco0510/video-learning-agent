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

import logging
from pathlib import Path
from typing import Any, Callable

from vla.config import VLAConfig
from vla.log.transcription_log import TranscriptionLog
from vla.models import VideoTask


logger = logging.getLogger(__name__)


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
        """
        self.cfg = cfg
        self.strategy = strategy
        self.source_factory = source_factory
        self.transcriber = transcriber
        self.notifier = notifier
        self.plugin_status = plugin_status
        self._save_dir = Path(save_dir) if save_dir else Path("./tmp")

    def __call__(self, task: VideoTask) -> tuple[str, str, Path | None]:
        """返回 (text, source, audio_path_or_None)。

        audio_path 在走 Whisper 兜底时返回,质量通过后由主调度清理;
        走官方/插件字幕时返回 None(没有 audio 需要清理)。
        """
        url = str(task.url)
        duration_sec = task.expected_duration

        # 1. 字幕三级策略(含 FR-2.5/2.6 popup 流程)
        try:
            result = self.strategy.get_subtitle(url, duration_sec)
        except Exception as e:
            logger.warning("策略调用异常,降级到 source_factory: %s", e)
            result = None

        if result is not None:
            # 字幕命中 → 不需要 video/audio 路径
            logger.info(
                "✓ %s 字幕来源: %s", task.title, result.source,
            )
            return (result.text, result.source, None)

        # 2. 全失败 → source_factory.get + transcriber
        logger.info(
            "📼 %s:字幕三级全失败,走兜底(下载/录屏 + Whisper)",
            task.title,
        )
        try:
            source = self.source_factory.get(url, task.id, duration_sec)
        except Exception as e:
            raise RuntimeError(f"source_factory.get failed: {e}") from e

        video_path = source.path
        # 3. 转写(FR-3.3 内部删视频源)
        try:
            text = self.transcriber.transcribe(video_path)
        except Exception as e:
            # 保留 .wav(若已生成)供重试
            audio_path = video_path.with_suffix(".wav")
            raise RuntimeError(f"transcribe failed: {e}") from e

        audio_path = video_path.with_suffix(".wav")
        return (text, "whisper", audio_path)


def build_text_provider(
    cfg: VLAConfig,
    *,
    notifier: Any,
    plugin_status: Any,
    save_dir: Path | None = None,
    driver: Any = None,
    recorder: Any = None,  # F2-8:deprecated,保留以兼容老调用方(始终 None)
) -> Callable[[VideoTask], tuple[str, str, Path | None]]:
    """工厂函数:装配一个完整的 RealTextProvider,供 CLI / E2E 使用。

    Args:
        cfg: VLAConfig
        notifier: MacOSNotifier(必填 — 弹窗)
        plugin_status: PluginStatus(必填 — session 单例)
        save_dir: 临时文件目录(默认 cfg.storage.tmp_dir)
        driver: BrowserDriver(可选,字幕策略需要)
        recorder: deprecated(F2-8:旧 Screen Recorder 已删,传参保留但运行时忽略)

    Returns:
        可调用对象:(task) → (text, source, audio_path)
    """
    from vla.source.video_source import VideoSourceFactory
    from vla.subtitle.strategy import SubtitleStrategy
    from vla.subtitle.platform_adapter import PlatformAdapterRegistry
    from vla.transcribe.streaming import StreamingTranscriber

    save_dir = Path(save_dir) if save_dir else Path(cfg.storage.tmp_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    log = TranscriptionLog(cfg.logging.log_dir)
    source_factory = VideoSourceFactory(tmp_dir=save_dir, log=log, config=cfg)
    transcriber = StreamingTranscriber(cfg)

    # F2-8:不再自动构造旧 Screen Recorder。弹窗 enabled 路径已废弃 —
    # 真实录屏兜底走策略 ③ adapter.fetch_via_recording(yt-dlp / Tab Audio Recorder)。
    # driver 仍按需自动连 Chrome CDP。
    if driver is None:
        driver = _try_connect_chrome(cfg, transcriber, notifier)

    strategy = SubtitleStrategy(
        registry=_build_registry(cfg, save_dir=save_dir),
        driver=driver,
        recorder=recorder,  # 保留(测试 fixture 注入 MagicMock,enabled 路径 stub)
        notifier=notifier,
        plugin_status=plugin_status,
        remind_timeout_sec=cfg.browser_plugin.remind_timeout_sec,
        plugin_name=cfg.browser_plugin.name,
        save_dir=save_dir,
    )

    return RealTextProvider(
        cfg=cfg,
        strategy=strategy,
        source_factory=source_factory,
        transcriber=transcriber,
        notifier=notifier,
        plugin_status=plugin_status,
        save_dir=save_dir,
    )


def _build_registry(
    cfg: VLAConfig,
    *,
    save_dir: Path,
) -> Any:
    """装配 PlatformAdapterRegistry(2026-09-02 修复:之前一直是空的!)

    装配顺序:
    1. BilibiliAdapter(cfg.platforms.bilibili.enabled) — 实例注册(带 F2-7 4 deps)
    2. InternalSiteAdapter(cfg.platforms.internal_site.enabled) — 类注册(无 deps)

    B站 → 实例注册的原因:BilibiliAdapter 构造需要 `official`(B站官方 API 客户端)
    和 4 REQUIRED deps(audio_factory/tab_recorder/transcriber/screenshot_controller),
    没法用 registry 默认的无参构造。
    """
    from vla.subtitle.bilibili_adapter import BilibiliAdapter
    from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
    from vla.subtitle.internal_site_adapter import InternalSiteAdapter
    from vla.subtitle.platform_adapter import PlatformAdapterRegistry

    registry = PlatformAdapterRegistry()

    if cfg.platforms.bilibili.enabled:
        official = BilibiliOfficialSubtitle()
        adapter = BilibiliAdapter(
            official=official,
            save_dir=save_dir,
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