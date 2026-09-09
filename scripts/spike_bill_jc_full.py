"""b-learning.bill-jc.com end-to-end spike — Phase 9.6 验收脚本。

3 种模式:
  1. --list-only:爬目录树 + 视频列表,打印 kng_id / title / url。
  2. --kng-id <id>:端到端跑单视频(爬 m3u8 → 抽音 → 转写 → 质量门控)。
  3. --kng-id + --resolution 360p:走低分辨率档(其他参数化同理)。

前置:用户手动启 Chrome debug 并登录 b-learning.bill-jc.com:
  chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &

用法:
  uv run python scripts/spike_bill_jc_full.py \\
      --college-id <cid> --list-only --root-label "技术分享" --limit 5

  uv run python scripts/spike_bill_jc_full.py \\
      --college-id <cid> --kng-id <kng_id>

  uv run python scripts/spike_bill_jc_full.py \\
      --college-id <cid> --kng-id <kng_id> --resolution 360p
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path


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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# 默认 config 路径(同 cli.py 的 CONFIG_FILE)
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "vla.yaml"

import typer  # noqa: E402

app = typer.Typer(add_completion=False, help="b-learning.bill-jc.com end-to-end spike")


@app.command()
def run(
    kng_id: str | None = typer.Option(
        None, "--kng-id", help="单视频模式:直接跑这个 kng_id",
    ),
    list_only: bool = typer.Option(
        False, "--list-only", help="只跑 list_tasks 打印目录 + 视频",
    ),
    root_label: str | None = typer.Option(
        None, "--root-label", help="list 模式:限定根目录 label(如 '技术分享')",
    ),
    limit: int = typer.Option(
        10, "--limit", help="list 模式:最多返回几条 VideoTask",
    ),
    college_id: str = typer.Option(
        ..., "--college-id", help="collegeId(必填,从 b-learning.bill-jc.com URL 取)",
    ),
    cdp_url: str = typer.Option(
        "http://localhost:9222", "--cdp-url", help="Chrome CDP debug URL",
    ),
    resolution: str = typer.Option(
        "720p", "--resolution", help="视频分辨率档(360p / 480p / 720p / 1080p)",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG, "--config", help="配置文件路径(默认 config/vla.yaml)",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="DEBUG 级日志"),
) -> None:
    """3 种模式:list-only / 端到端单视频 / 参数化(改 --resolution 等)。"""
    setup_logging()
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    load_env()

    from vla.config import VLAConfig
    from vla.subtitle.internal_site_spider import InternalSiteSpider

    cfg = VLAConfig.from_yaml(config_path)
    spider = InternalSiteSpider(
        cdp_url=cdp_url,
        college_id=college_id,
        resolution=resolution,
    )

    if list_only:
        _run_list_mode(spider, root_label, limit)
        return

    if kng_id is None:
        typer.echo("ERROR: --kng-id 或 --list-only 至少一个", err=True)
        raise typer.Exit(1)

    _run_endtoend(cfg, spider, kng_id)


def _run_list_mode(
    spider: InternalSiteSpider,
    root_label: str | None,
    limit: int,
) -> None:
    """模式 1:爬目录树 + 视频列表,打印 kng_id / title / url。"""
    logger = logging.getLogger("spike.list")
    logger.info("[LIST] root_label=%r limit=%d", root_label, limit)
    try:
        tasks = asyncio.run(spider.list_tasks(root_label=root_label, limit=limit))
    except ValueError as e:
        logger.error("[FAIL] %s", e)
        raise typer.Exit(1)
    except Exception as e:
        logger.error("[FAIL] list_tasks 异常:%s", e, exc_info=True)
        raise typer.Exit(1)

    logger.info("[LIST] 找到 %d 条:", len(tasks))
    for t in tasks:
        typer.echo(f"  {t.id} | {t.title} | {t.url}")


def _run_endtoend(
    cfg: "VLAConfig",  # noqa: F821 - forward ref to avoid runtime import in type hints
    spider: InternalSiteSpider,
    kng_id: str,
) -> None:
    """模式 2 + 3:端到端跑单视频(爬 m3u8 → 抽音 → 转写 → 质量门控)。"""
    from vla.main_provider import build_text_provider
    from vla.models import VideoTask

    logger = logging.getLogger("spike.e2e")

    # fetch_asset 路径 ②(InternalSiteSpider → extract_m3u8_audio)需要 InternalSiteSpider
    # 单例。build_text_provider 内部 auto-construct 一个;为了命中真实 spider,这里注入。
    from vla.subtitle.strategy import SubtitleStrategy
    from vla.subtitle.tab_audio_recorder import TabAudioRecorder
    from vla.subtitle.browser_driver import BrowserDriver
    from vla.audio.source_factory import AudioSourceFactory
    from vla.subtitle.platform_adapter import PlatformAdapterRegistry
    from vla.subtitle.bilibili_adapter import BilibiliAdapter
    from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
    from vla.subtitle.internal_site_adapter import InternalSiteAdapter
    from vla.transcribe.streaming import StreamingTranscriber
    from vla.state.plugin_status import PluginStatus
    from vla.ui.macos_notify import MacOSNotifier
    from vla.log.transcription_log import TranscriptionLog
    from vla.subtitle.audio_scan import find_today_dir
    from vla.quality.checker import QualityChecker
    from vla.quality.refiner import SubtitleRefiner

    save_dir = Path(cfg.storage.tmp_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    notifier = MacOSNotifier()
    plugin_status = PluginStatus()
    log = TranscriptionLog(cfg.logging.log_dir)
    transcriber = StreamingTranscriber(cfg)

    # driver 可选(若 Chrome debug 未启 → None,不影响 fetch_asset 路径 ②)
    driver = None
    try:
        driver = BrowserDriver(cfg)
        driver.connect()
        logger.info("✓ Chrome CDP 已连接")
    except Exception as e:
        logger.warning("⚠ Chrome CDP 连接失败(driver=None,继续):%s", e)
        driver = None

    audio_factory = AudioSourceFactory(save_dir=save_dir / "audio_raw")
    tab_recorder = TabAudioRecorder(
        match_keyword=getattr(
            getattr(cfg, "extension", None), "tab_audio_recorder.match_keyword", "tab audio",
        ),
        save_dir=save_dir / "audio_raw",
    )

    registry = PlatformAdapterRegistry()
    if cfg.platforms.bilibili.enabled:
        official = BilibiliOfficialSubtitle()
        registry.register_instance(BilibiliAdapter(
            official=official,
            audio_factory=audio_factory,
            transcriber=transcriber,
        ))
    if cfg.platforms.internal_site.enabled:
        registry.register(InternalSiteAdapter)

    strategy = SubtitleStrategy(
        registry=registry,
        driver=driver,
        recorder=None,
        notifier=notifier,
        plugin_status=plugin_status,
        remind_timeout_sec=cfg.browser_plugin.remind_timeout_sec,
        plugin_name=cfg.browser_plugin.name,
        audio_factory=audio_factory,
        tab_recorder=tab_recorder,
        transcriber=transcriber,
        save_dir=save_dir,
        cfg=cfg,
    )

    checker = QualityChecker(cfg)
    refine_llm = None
    if cfg.quality_check.refine_enabled:
        from vla.llm.client import LLMClient
        refine_llm = LLMClient(cfg.llm_client, model=cfg.quality_check.refine_model)
    refiner = SubtitleRefiner(cfg, refine_llm) if cfg.quality_check.refine_enabled else None

    today_dir = find_today_dir(Path(cfg.audio.downloads_dir)) if cfg.audio is not None else None

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

    task = VideoTask(
        id=kng_id,
        title=f"bill-jc-{kng_id}",
        url=f"https://b-learning.bill-jc.com/learn/{kng_id}",
        expected_duration=3600,
    )

    logger.info("[FETCH] kng_id=%s resolution=%s", kng_id, spider.resolution)
    asset = asyncio.run(fetch_asset(task))
    if asset is None:
        logger.error("[FAIL] fetch_asset 返回 None — 检查 Chrome debug + cookie")
        raise typer.Exit(1)

    if asset.audio_path is not None and asset.audio_path.exists():
        wav_size_mb = asset.audio_path.stat().st_size / 1e6
        logger.info(
            "[OK] Asset source=%s wav=%s size=%.1fMB",
            asset.source, asset.audio_path, wav_size_mb,
        )
    else:
        logger.info(
            "[OK] Asset source=%s text=%d chars(字幕直接命中,无需抽音)",
            asset.source, len(asset.text or ""),
        )

    logger.info("[PROCESS] 转写 + 质量门控 ...")
    result = asyncio.run(process_asset(asset, task))
    if result is None:
        logger.error("[FAIL] process_asset 返回 None(质量 fail / 转写异常)")
        raise typer.Exit(1)

    logger.info("[OK] Quality score=%d passed=%s", result.qr.score, result.qr.passed)
    logger.info("[OK] Duration: %ds source=%s", result.duration_sec, result.source)
    logger.info(
        "[DONE] 字幕落盘: %s/%s.txt",
        cfg.storage.transcribed_dir, kng_id,
    )


if __name__ == "__main__":
    app()
