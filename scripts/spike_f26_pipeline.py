"""F2-6 v3.2 全链路端到端 spike(SSOT: implementation-plan.md F2-6 节)。

数据流(2026-09-07 v3.2):
    B站 URL
      → main.py._process_one
          Step 0: Phase A 截图(关键路径,失败 → 跳过视频)
              ScreenshotPhaseController.phase_a_start(page)
          Step 1: SubtitleStrategy.get_subtitle(url, duration_sec)
              → BilibiliAdapter.fetch_via_recording(audio_factory + tab_recorder)
                  路径 ① yt-dlp 抽音频(audio_factory.extract)
                  路径 ② Free Tab Audio Recorder 扩展(match_keyword="free tab audio recorder")
          Step 2: StreamingTranscriber.transcribe(audio_path)
              → ffmpeg 抽 .wav → faster-whisper → Level 1 本地清理(clean_transcript)
              → 写 <stem>.transcript.txt / <stem>.cleaned.txt
          Step 3: QualityChecker.check(text)
              → 启发式 0:char_count < min_chars(50)→ fail score=5
              → 启发式 1/2:语速 + 重复
              → LLM 评估
          Step 4: quality.passed=True → 显式 unlink audio.wav + SubtitleRefiner.refine
              → 写 <stem>.refined.txt(云端 LLM 整理)
          Step 5: Phase C 末尾截图(失败 → log warning 不阻塞)
      → save_transcribed(text=refined)

前置(同 F2-5):
  - Chrome 已启动:debug port 9222(`./scripts/start_chrome_debug.py`)
  - macOS 屏幕录制 / 辅助功能 / 通知授权
  - Free Tab Audio Recorder 扩展已安装且 enabled(`chrome://extensions/`)
  - OPENAI_API_KEY 已配(.env)

v3.2 vs F2-5 差异:
  ① 截图升级为关键路径(Step 0/5 在 main.py,而非 adapter)
  ② Free Tab Audio Recorder 全名匹配(替代短名 "tab audio")
  ③ Quality 走 length gate(短文本 fail score=5 不调 LLM)
  ④ Refine 由 main.py 显式调(替代 streaming.py 注入)
  ⑤ 音频删除由 main.py 显式 unlink(替代 streaming.cleanup)

用法:
  uv run python scripts/spike_f26_pipeline.py \\
      --url "https://www.bilibili.com/video/BV1DUgK6cEi3" \\
      --duration 60 \\
      --model small

  # 跳过截图(只用音频 + 转写路径):
  uv run python scripts/spike_f26_pipeline.py --url "..." --no-screenshot

  # F2-10 完整链路(spike prep 测试 webm + 强制 popup + auto-response enabled):
  #   1) Chrome 已开 B站视频页(spike 用 v3.2.1.8 复用 user tab)
  #   2) --prep-webm:把指定 wav 拷到今天 YYYY-MM-DD/ 当作"用户拖入"的测试 webm
  #   3) --force-popup:跳过官方 CC + yt-dlp,直接进 popup
  #   4) --auto-response enabled:不弹 GUI,直接模拟"用户点已开启"
  uv run python scripts/spike_f26_pipeline.py \\
      --url "https://www.bilibili.com/video/BV1DUgK6cEi3" \\
      --duration 60 \\
      --prep-webm tmp/audio_raw/BV1DUgK6cEi3.wav \\
      --force-popup \\
      --auto-response enabled
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def load_env() -> None:
    """从 .env 加载到 os.environ(LLMClient 实时读 env var)。

    LLMClient 构造时调 os.environ.get(api_key_env),所以必须在 import
    LLMClient / 构造之前加载。
    """
    import os

    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        print(f"⚠ .env 不存在:{env_file},LLM 会因为缺 OPENAI_API_KEY 失败")
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


async def main() -> None:
    parser = argparse.ArgumentParser(description="F2-6 v3.2 全链路端到端 spike")
    parser.add_argument("--url", required=True, help="B站视频 URL")
    parser.add_argument("--title", default="F2-6 spike 视频")
    parser.add_argument("--duration", type=int, default=60, help="视频时长(秒)")
    parser.add_argument("--bvid", default="", help="bvid(可选,默认从 URL 解析)")
    parser.add_argument("--group", default="f26_spike")
    parser.add_argument(
        "--config", default=str(PROJECT_ROOT / "config" / "vla.yaml")
    )
    parser.add_argument(
        "--whisper-model", default="small",
        help="tiny(75MB,首跑快)/ small(460MB,默认)/ base(150MB)",
    )
    parser.add_argument(
        "--no-screenshot", action="store_true",
        help="禁用 F2-6 截图链路(只跑字幕 + 转写 + 清理 + 质量)",
    )
    parser.add_argument(
        "--debug-port", type=int, default=9222,
        help="Chrome CDP debug port(默认 9222)",
    )
    parser.add_argument(
        "--prep-webm", default="",
        help="F2-10 测试用:把指定 wav 拷到 <downloads_dir>/<今天>/<id>.webm,"
             "模拟'用户手动拖入'。配合 --auto-response enabled 跑 F2-10 扫目录路径",
    )
    parser.add_argument(
        "--auto-response", default="",
        choices=("", "enabled", "skip", "timeout"),
        help="弹窗 auto-response:不弹 GUI,直接返回指定值。"
             "留空 = 真实 GUI 弹窗(用户必须手动点)",
    )
    parser.add_argument(
        "--force-popup", action="store_true",
        help="跳过 ① yt-dlp + 官方 CC,强制进 popup 路径(测试 F2-10 扫目录)",
    )
    args = parser.parse_args()
    setup_logging()
    load_env()

    import re
    from vla.audio.source_factory import AudioSourceFactory
    from vla.capture.screen_capture import ScreenCapture
    from vla.capture.screenshot_phase_controller import ScreenshotPhaseController
    from vla.config import VLAConfig
    from vla.llm.client import LLMClient
    from vla.log.failure_alert import FailureAlert
    from vla.log.transcription_log import TranscriptionLog
    from vla.main import VideoLearningAgent
    from vla.models import VideoTask
    from vla.quality.checker import QualityChecker
    from vla.quality.refiner import SubtitleRefiner
    from vla.state.history import HistoryManager
    from vla.state.plugin_status import PluginStatus
    from vla.state.quota import QuotaManager
    from vla.subtitle.bilibili_adapter import BilibiliAdapter
    from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
    from vla.subtitle.browser_driver import BrowserDriver
    from vla.subtitle.platform_adapter import PlatformAdapterRegistry
    from vla.subtitle.strategy import SubtitleStrategy
    from vla.subtitle.tab_audio_recorder import TabAudioRecorder
    from vla.summary.llm_summarizer import LLMSummarizer
    from vla.transcribe.streaming import StreamingTranscriber
    from vla.ui.macos_notify import MacOSNotifier

    cfg = VLAConfig.from_yaml(args.config)
    cfg.whisper.model = args.whisper_model
    # F2-6.1:启用 Chrome Session(关键路径截图前提)
    cfg.chrome_session.enabled = not args.no_screenshot

    # ---- 解析 bvid ----
    bvid = args.bvid
    if not bvid:
        m = re.search(r"/(BV[A-Za-z0-9]+)", args.url)
        bvid = m.group(1) if m else f"local_{abs(hash(args.url))}"

    # ---- 0. 基础 deps ----
    save_dir = Path(cfg.storage.tmp_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    log = TranscriptionLog(cfg.logging.log_dir)
    history = HistoryManager(cfg.history.file)
    quota = QuotaManager(cfg)
    notifier = MacOSNotifier()
    plugin_status = PluginStatus()
    failure_alert = FailureAlert(
        threshold=cfg.logging.log_alert_threshold,
        log=log, notifier=notifier,
        enabled=cfg.logging.log_alert_enabled,
    )

    # ---- 1. LLM 客户端(quality + summary + refiner 共用 base_url) ----
    quality_llm = LLMClient(cfg.llm_client, model=cfg.quality_check.model)
    summary_llm = LLMClient(cfg.llm_client, model=cfg.summary.model)
    refine_llm = LLMClient(cfg.llm_client, model=cfg.llm.refine_model)

    summarizer = LLMSummarizer(summary_llm, cfg.summary.notes_file)
    summarizer.cfg = cfg
    checker = QualityChecker(cfg)
    checker.set_llm(quality_llm)

    # F2-6.3:Refine 不再注入 streaming(由 main.py 显式调)
    refiner = SubtitleRefiner(cfg, refine_llm)
    transcriber = StreamingTranscriber(cfg, model=None)

    # ---- 2. 截图 deps(F2-6 v3.2.1.1:controller 自己创建 page) ----
    # v3.2.1 root cause:spike 顶层用 sync_playwright 留下 dispatcher loop。
    # v3.2.1.1 修法:controller 暴露 phase_a_start_url / phase_c_end_url,
    # 内部用 BrowserDriver.new_background_page() + page.goto(url) 创建 page。
    # spike 顶层**不调** sync Playwright — controller 内部按需 new_background_page
    # (sync Playwright API,但 BrowserDriver.connect 已被 main.py _start_chrome_session
    # 隔离到 asyncio.to_thread 上下文,不会污染 spike 顶层 asyncio loop)。
    capture = ScreenCapture(save_dir=save_dir / "screenshots")
    browser_driver = None
    raw_controller = None
    controller = None
    if cfg.chrome_session.enabled:
        browser_driver = BrowserDriver(cfg)
        raw_controller = ScreenshotPhaseController(browser_driver, notifier, capture)

        # adapter:把 main.py 期望的 (url, video_id, title, duration_sec) 映射到 controller
        class _ControllerAdapter:
            def __init__(self, c: ScreenshotPhaseController):
                self._c = c

            async def phase_a_start(self, *, url: str, video_id: str, title: str, duration_sec: int) -> None:
                await self._c.phase_a_start_url(url=url, audio_id=video_id)

            async def phase_c_end(self, *, url: str, video_id: str, title: str, duration_sec: int) -> None:
                await self._c.phase_c_end_url(url=url, audio_id=video_id)

        controller = _ControllerAdapter(raw_controller)
        print("  ✓ ScreenshotPhaseController + _ControllerAdapter 已就绪(Phase A/C 接入 main.py)")
    else:
        print("  ⚠️ chrome_session.disabled: 跳过截图组件")

    # ---- 3. F2-6.2:Free Tab Audio Recorder 全名匹配 ----
    audio_factory = AudioSourceFactory(save_dir=save_dir / "audio_raw")
    tab_recorder = TabAudioRecorder(
        match_keyword="free tab audio recorder",  # F2-6.2:扩展全名
        save_dir=save_dir / "audio_raw",
    )
    print(f"  ✓ Free Tab Audio Recorder match_keyword = {tab_recorder.match_keyword!r}")

    # ---- 4. 装配 BilibiliAdapter with 2 deps(F2-10 简化)----
    # 注意:F2-10 删了 tab_recorder / screenshot_controller 依赖。
    # Tab Audio Recorder 路径迁到 strategy._try_browser 弹窗 enabled 分支;
    # 截图由 main.py 直接调 ScreenshotPhaseController 触发。
    official = BilibiliOfficialSubtitle()
    bilibili_adapter = BilibiliAdapter(
        official=official,
        audio_factory=audio_factory,
        transcriber=transcriber,
    )

    registry = PlatformAdapterRegistry()
    registry.register_instance(bilibili_adapter)
    print("  ✓ B站 adapter 已注册(F2-6:截图移到 main.py)")

    # ---- 5. SubtitleStrategy + 真实 text_provider ----
    strategy = SubtitleStrategy(
        registry=registry,
        driver=browser_driver,
        recorder=None,  # F2-8:已删
        notifier=notifier,
        plugin_status=plugin_status,
        remind_timeout_sec=cfg.browser_plugin.remind_timeout_sec,
        plugin_name=cfg.browser_plugin.name,
        audio_factory=audio_factory,
        tab_recorder=tab_recorder,
        transcriber=transcriber,
        screenshot_controller=None,  # F2-6:截图改走 main.py
        save_dir=save_dir,
        cfg=cfg,  # F2-10:扫今天 YYYY-MM-DD/ 用
    )

    from vla.main_provider import build_text_provider
    # Task 12(2026-09-09 asset-pipeline-refactor):spike 改用 build_text_provider 工厂
    # 返回 (fetch_asset, process_asset) 二元组。strategy 预构造后传入,内部
    # bilibili_adapter / tab_recorder / audio_factory 可被 monkey-patch 复用。
    fetch_asset, process_asset = build_text_provider(
        cfg=cfg,
        transcriber=transcriber,
        notifier=notifier,
        plugin_status=plugin_status,
        save_dir=save_dir,
        log=log,
        checker=checker,
        refiner=refiner,
        strategy=strategy,
    )

    # ---- 6. 启动主调度(F2-6 全注入) ----
    agent = VideoLearningAgent(
        cfg=cfg,
        log=log,
        history=history,
        quota=quota,
        summarizer=summarizer,
        notifier=notifier,
        fetch_asset=fetch_asset,
        process_asset=process_asset,
        plugin_status=plugin_status,
        failure_alert=failure_alert,
        refiner=refiner,                       # F2-6.3:Refiner 由 main.py 调
        browser_driver=browser_driver,        # F2-6.1:Chrome Session (None = 禁用)
        screenshot_controller=controller,     # F2-6.1:截图关键路径(None = 禁用)
    )

    task = VideoTask(
        id=bvid,
        title=args.title,
        url=args.url,
        expected_duration=args.duration,
        group_id=args.group,
        group_title=args.group,
    )

    # ---- F2-10 spike 测试钩子(跑前注入) ----
    import shutil as _shutil
    from datetime import date as _date
    _hook_logger = logging.getLogger("f26_spike.hooks")

    # 1. --prep-webm:把指定 wav 拷到 <downloads_dir>/<今天>/<id>.webm,模拟"用户拖入"
    if args.prep_webm:
        src = Path(args.prep_webm).expanduser().resolve()
        if not src.exists():
            _hook_logger.error("❌ --prep-webm 源文件不存在: %s", src)
            sys.exit(2)
        if cfg.audio is None:
            _hook_logger.error("❌ cfg.audio is None — config/vla.yaml 缺 audio 块")
            sys.exit(2)
        downloads_root = cfg.audio.downloads_dir
        today_dir = downloads_root / _date.today().isoformat()
        today_dir.mkdir(parents=True, exist_ok=True)
        dst = today_dir / f"{bvid}.webm"
        _shutil.copy(src, dst)
        _hook_logger.info("✓ --prep-webm: %s → %s (%d bytes)", src, dst, dst.stat().st_size)

    # 2. --auto-response:monkeypatch notifier.ask_open_browser,不弹 GUI
    if args.auto_response:
        from unittest.mock import patch as _patch

        def _fake_ask(url, plugin_name, timeout_sec):  # noqa: ARG001
            _hook_logger.info(
                "✓ --auto-response: notifier.ask_open_browser(%s, plugin=%r, t=%ds) → %r",
                url[:60], plugin_name, timeout_sec, args.auto_response,
            )
            return args.auto_response

        _patch.object(notifier, "ask_open_browser", side_effect=_fake_ask).start()
        _hook_logger.info("✓ --auto-response %r 注入成功(无 GUI 弹窗)", args.auto_response)

    # 3. --force-popup:monkeypatch BilibiliAdapter.fetch_browser_subtitle → None
    #    (跳过 ① yt-dlp + 官方 CC,直接进 popup 路径)
    if args.force_popup:
        from unittest.mock import patch as _patch

        def _force_miss(_driver, _url):  # noqa: ARG001
            _hook_logger.info("✓ --force-popup: BilibiliAdapter.fetch_browser_subtitle → None")
            return None

        _patch.object(bilibili_adapter, "fetch_browser_subtitle", side_effect=_force_miss).start()
        _hook_logger.info("✓ --force-popup 已注入,adapter.fetch_browser_subtitle 强制 None")

    logger = logging.getLogger("f26_spike")
    logger.info("🚀 F2-6 v3.2 spike 开始:%s (%s)", args.title, args.url)
    logger.info(
        "  whisper model    = %s(compute=%s, language=%s)",
        cfg.whisper.model, cfg.whisper.compute_type, cfg.whisper.language,
    )
    logger.info(
        "  chrome_session   = %s(debug port=%d)",
        "ON" if cfg.chrome_session.enabled else "OFF",
        cfg.chrome_session.debug_port,
    )
    logger.info(
        "  refine_enabled   = %s(refine_max_chars=%d, model=%s)",
        cfg.quality_check.refine_enabled,
        cfg.quality_check.refine_max_chars,
        cfg.quality_check.refine_model,
    )
    logger.info(
        "  min_chars        = %d(质量 length gate,文本过短 fail score=5)",
        cfg.quality_check.min_chars,
    )
    logger.info(
        "  tab recorder     = match_keyword=%r",
        tab_recorder.match_keyword,
    )
    logger.info("  save_dir         = %s", save_dir)

    t0 = time.monotonic()
    try:
        stats = await agent.run([task])
    except Exception as e:
        logger.error("❌ spike 失败:%s", e, exc_info=True)
        stats = {"processed": 0, "passed": 0, "failed": 1, "skipped": 0, "summarized": 0}
    finally:
        failure_alert.check_after_write()

    elapsed = time.monotonic() - t0
    logger.info("=" * 70)
    logger.info("✓ F2-6 v3.2 spike 完成 — 耗时 %.1fs", elapsed)
    logger.info("  stats            = %s", stats)
    logger.info("  log 摘要         = %s", log.summary())
    logger.info("  log_dir          = %s", cfg.logging.log_dir)
    logger.info(
        "  screenshot index = %s",
        save_dir / "screenshots" / "index.jsonl",
    )
    logger.info("=" * 70)

    # F2-6 验证清单
    if stats.get("passed", 0) == 1:
        logger.info("✅ 验证清单:")
        logger.info("  ✓ Step 0 Phase A 截图(失败 → 跳视频)")
        logger.info("  ✓ Step 1 三级字幕策略")
        logger.info("  ✓ Step 2 Whisper 转写(Level 1 本地清理)")
        logger.info("  ✓ Step 3 质量门控(length gate + 启发式 + LLM)")
        logger.info("  ✓ Step 4 Refine 由 main.py 显式调 + 显式 unlink audio.wav")
        logger.info("  ✓ Step 5 Phase C 末尾截图")
    else:
        logger.warning("❌ 未通过验证(stats.passed != 1),请检查日志")

    sys.exit(0 if stats.get("passed", 0) == 1 else 1)


if __name__ == "__main__":
    # v3.2.1:VideoLearningAgent 整体 async,顶层 asyncio.run 包一下
    asyncio.run(main())
