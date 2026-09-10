"""b-learning.bill-jc.com end-to-end spike — Phase 9.6 验收脚本。

4 种模式(通过 flag 区分,2026-09-10 加 --parse-only):
  1. --list-only:爬目录树 + 视频列表,打印 kng_id / title / url。
  2. --kng-id <id>:端到端跑单视频(爬 m3u8 → 抽音 → 转写 → 质量门控)。
  3. --kng-id + --resolution 360p:走低分辨率档(其他参数化同理)。
  4. --parse-only:只解析单视频元数据(走 kngPlay API 拿 title / duration / collegeId),
     **不**跑转写。给一个 kng_id,返回 JSON,方便用户预检 / 调试。

前置:用户手动启 Chrome debug 并登录 b-learning.bill-jc.com:
  chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &

用法:
  uv run python scripts/spike_bill_jc_full.py \\
      --college-id <cid> --list-only --root-label "技术分享" --limit 5

  uv run python scripts/spike_bill_jc_full.py \\
      --college-id <cid> --kng-id <kng_id>

  uv run python scripts/spike_bill_jc_full.py \\
      --college-id <cid> --kng-id <kng_id> --resolution 360p

  uv run python scripts/spike_bill_jc_full.py \\
      --parse-only --kng-id <kng_id> --cdp-url http://localhost:9222
  (注:--parse-only 模式 --college-id 非必需)
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

from vla.config import VLAConfig
from vla.subtitle.internal_site_spider import InternalSiteSpider



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
        None, "--kng-id", help="单视频模式:直接跑这个 kng_id(parse / 端到端均需)",
    ),
    list_only: bool = typer.Option(
        False, "--list-only", help="只跑 list_tasks 打印目录 + 视频",
    ),
    parse_only: bool = typer.Option(
        False, "--parse-only",
        help="只解析单视频元数据(走 kngPlay API 拿 title / duration / collegeId),"
             "**不**跑转写。需同时给 --kng-id,可不给 --college-id",
    ),
    root_label: str | None = typer.Option(
        None, "--root-label", help="list 模式:限定根目录 label(如 '技术分享')",
    ),
    catalog_id: str | None = typer.Option(
        None, "--catalog-id",
        help="list 模式:直接指定 catalogId 跳过 tree(从 bill-jc URL 拿)",
    ),
    limit: int = typer.Option(
        10, "--limit", help="list 模式:最多返回几条 VideoTask",
    ),
    college_id: str = typer.Option(
        "", "--college-id",
        help="collegeId(parse 模式可不填,其他模式必填,从 b-learning.bill-jc.com URL 取)",
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
    """4 种模式:list-only / parse-only / 端到端单视频 / 参数化(--resolution 等)。"""
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
        if not college_id:
            typer.echo("ERROR: --list-only 必须同时给 --college-id", err=True)
            raise typer.Exit(1)
        _run_list_mode(spider, root_label, catalog_id, limit)
        return

    if parse_only:
        if not kng_id:
            typer.echo("ERROR: --parse-only 必须同时给 --kng-id", err=True)
            raise typer.Exit(1)
        _run_parse_mode(spider, kng_id)
        return

    # 端到端模式:college_id 必须,kng_id 必须
    if not college_id:
        typer.echo("ERROR: 端到端模式必须给 --college-id", err=True)
        raise typer.Exit(1)
    if kng_id is None:
        typer.echo("ERROR: --kng-id 或 --list-only 或 --parse-only 至少一个", err=True)
        raise typer.Exit(1)

    # 端到端模式:整段包在单个 asyncio.run 里 — fetch_asset / process_asset
    # 共享 HTTP client / CDP 连接,跨 loop 会 "Event loop is closed"。
    asyncio.run(_run_endtoend(cfg, spider, kng_id))


def _run_parse_mode(
    spider: "InternalSiteSpider",
    kng_id: str,
) -> None:
    """模式 4:--parse-only,只解析单视频元数据,不打字幕 / 不抽音 / 不跑质量门控。

    走 spider.fetch_metadata(kng_id) → 打印 JSON。
    失败不抛:打印错误 + typer.Exit(1)。
    """
    logger = logging.getLogger("spike.parse")
    logger.info("[PARSE] kng_id=%s resolution=%s", kng_id, spider.resolution)
    try:
        meta = asyncio.run(spider.fetch_metadata(kng_id))
    except Exception as e:
        logger.error("[FAIL] fetch_metadata 异常:%s", e, exc_info=True)
        typer.echo(
            json.dumps({"kng_id": kng_id, "error": str(e)}, ensure_ascii=False, indent=2),
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(json.dumps(meta, ensure_ascii=False, indent=2))
    logger.info("[OK] parse 完成,kng_id=%s title=%r", kng_id, meta.get("title"))


def _run_list_mode(
    spider: "InternalSiteSpider",
    root_label: str | None,
    catalog_id: str | None,
    limit: int,
) -> None:
    """模式 1:爬目录树 + 视频列表,打印 kng_id / title / url。

    优先级:catalog_id > root_label > 全树。
    catalog_id 是从 bill-jc 页面 URL 直接拿,跳过 tree API 更快。
    """
    logger = logging.getLogger("spike.list")
    logger.info(
        "[LIST] root_label=%r catalog_id=%r limit=%d", root_label, catalog_id, limit
    )
    try:
        tasks = asyncio.run(
            spider.list_tasks(
                root_label=root_label, catalog_id=catalog_id, limit=limit
            )
        )
    except ValueError as e:
        logger.error("[FAIL] %s", e)
        raise typer.Exit(1)
    except Exception as e:
        logger.error("[FAIL] list_tasks 异常:%s", e, exc_info=True)
        raise typer.Exit(1)

    logger.info("[LIST] 找到 %d 条:", len(tasks))
    for t in tasks:
        typer.echo(f"  {t.id} | {t.title} | {t.url}")


async def _run_endtoend(
    cfg: "VLAConfig",  # noqa: F821 - forward ref to avoid runtime import in type hints
    spider: "InternalSiteSpider",
    kng_id: str,
) -> None:
    """模式 2 + 3:端到端跑单视频(爬 m3u8 → 抽音 → 转写 → 质量门控)。

    整个流程在调用方的 event loop 里跑(fetch_asset / process_asset 共享
    HTTP client + CDP 连接 — 跨 loop 会 "Event loop is closed")。
    """
    from vla.main_provider import build_text_provider
    from vla.models import VideoTask

    logger = logging.getLogger("spike.e2e")

    # Critical 2:QualityChecker 走启发式过但仍需 LLM 评分(must avoid
    # "QualityChecker 没有 LLM 客户端…" RuntimeError 在 happy path 上)。
    from vla.llm.client import LLMClient
    from vla.subtitle.strategy import SubtitleStrategy
    from vla.subtitle.browser_driver import BrowserDriver
    from vla.audio.source_factory import AudioSourceFactory
    from vla.subtitle.platform_adapter import PlatformAdapterRegistry
    from vla.subtitle.bilibili_adapter import BilibiliAdapter
    from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle
    from vla.subtitle.internal_site_adapter import InternalSiteAdapter
    from vla.transcribe.streaming import StreamingTranscriber
    from vla.state.plugin_status import PluginStatus
    from vla.ui.notifier import create_notifier
    from vla.log.transcription_log import TranscriptionLog
    from vla.subtitle.audio_scan import find_today_dir
    from vla.quality.checker import QualityChecker
    from vla.quality.refiner import SubtitleRefiner

    save_dir = Path(cfg.storage.tmp_dir).resolve()
    save_dir.mkdir(parents=True, exist_ok=True)
    # 2026-09-10 轻量化:跨平台 notifier — macOS 走 MacOSNotifier,
    # Windows/Linux 走 NullNotifier。Windows 上 ask_open_browser 直接
    # 返 "skip",策略 ② 弹窗不阻塞。
    notifier = create_notifier()
    plugin_status = PluginStatus()
    log = TranscriptionLog(cfg.logging.log_dir)
    transcriber = StreamingTranscriber(cfg)

    # driver=None 跳过 BrowserDriver 后台 thread:bill-jc 路径走 InternalSiteSpider
    # + extract_browser_audio,两者都直接 connect_over_cdp(临时连接,不长期持有
    # Playwright)。BrowserDriver 后台 thread 也会连 cdp,跟 extract_browser_audio
    # 抢同一个 Chrome 实例的 Playwright lock 导致新 page 创建慢/超时。
    # 策略 ② 仍需要 driver=None 才能走通 — bill-jc 走策略 ①-a spider 路径,无影响。
    driver = None
    logger.info("✓ driver=None(bill-jc 路径走 spider + extract_browser_audio,无后台 thread)")

    audio_factory = AudioSourceFactory(save_dir=save_dir / "audio_raw")
    # 2026-09-10:TabAudioRecorder 已删,strategy 不再持有扩展依赖。

    # Critical 1 wiring:InternalSiteAdapter 必须以"实例注册 + spider 注入"形式
    # 装配,这样 strategy.get_subtitle 策略 ①-a 才能命中 bill-jc URL。
    # 类注册(spider=None) → fetch_via_spider 内部 _spider is None → None,
    # 策略 ①-a miss → 后续路径不会触发。
    #
    # Round 3 修复:本 spike 是 bill-jc 专用,不门控 internal_site.enabled —
    # 生产 config 默认 enabled=false,gate 会让 spike 静默降级到 path ③。
    # _build_registry 的 gate 保留(生产路径)。
    registry = PlatformAdapterRegistry()
    if cfg.platforms.bilibili.enabled:
        official = BilibiliOfficialSubtitle()
        registry.register_instance(BilibiliAdapter(
            official=official,
            audio_factory=audio_factory,
            transcriber=transcriber,
        ))
    registry.register_instance(InternalSiteAdapter(
        audio_factory=audio_factory,
        transcriber=transcriber,
        spider=spider,  # Phase 9.6:spider 注入,fetch_via_spider 才真走路径
    ))
    logger.info("✓ InternalSiteAdapter 已注册(instance, spider 已注入,无 gate)")

    strategy = SubtitleStrategy(
        registry=registry,
        driver=driver,
        recorder=None,
        notifier=notifier,
        plugin_status=plugin_status,
        audio_factory=audio_factory,
        transcriber=transcriber,
        save_dir=save_dir,
        cfg=cfg,
    )

    # Critical 2:QualityChecker 需要 LLM 客户端(否则启发式 + LLM 路径在
    # 启发式全过的 happy path 上抛 RuntimeError)。
    quality_llm = LLMClient(cfg.llm_client, model=cfg.quality_check.model)
    checker = QualityChecker(cfg)
    checker.set_llm(quality_llm)

    refine_llm = None
    if cfg.quality_check.refine_enabled:
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
        internal_spider=spider,  # Phase 9.6:兜底(spike 已自构造 registry,但保
                                 # 证 build_text_provider 内部 auto-construct 路径也对)
    )

    task = VideoTask(
        id=kng_id,
        title=f"bill-jc-{kng_id}",
        url=f"https://b-learning.bill-jc.com/learn/{kng_id}",
        # spike 端:从 wav ffprobe 拿真实 duration,避免 Phase 9.6 spider
        # placeholder 3600 让 QualityChecker 误判语速过低。生产 CLI 走
        # `cli.py:346` 传真实 duration;spider list_tasks 仍占位待后续 PR。
        expected_duration=3600,  # 下面 fetch_asset 后会被覆盖
    )

    logger.info("[FETCH] kng_id=%s resolution=%s", kng_id, spider.resolution)
    asset = await fetch_asset(task)
    if asset is None:
        logger.error("[FAIL] fetch_asset 返回 None — 检查 Chrome debug + cookie")
        raise typer.Exit(1)

    # 用 ffprobe 拿 wav 真实时长覆盖 placeholder 3600,避免 QualityChecker 误判
    if asset.audio_path is not None and asset.audio_path.exists():
        wav_size_mb = asset.audio_path.stat().st_size / 1e6
        logger.info(
            "[OK] Asset source=%s wav=%s size=%.1fMB",
            asset.source, asset.audio_path, wav_size_mb,
        )
        # ffprobe → duration_sec
        import json as _json
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(asset.audio_path)],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode == 0:
            try:
                dur = float(_json.loads(probe.stdout)["format"]["duration"])
                task.expected_duration = int(dur)
                logger.info("[OK] wav duration=%ds → task.expected_duration 覆盖 placeholder", int(dur))
            except (KeyError, ValueError, _json.JSONDecodeError) as e:
                logger.warning("⚠ ffprobe parse failed: %s,保留 placeholder 3600", e)
    else:
        logger.info(
            "[OK] Asset source=%s text=%d chars(字幕直接命中,无需抽音)",
            asset.source, len(asset.text or ""),
        )

    logger.info("[PROCESS] 转写 + 质量门控 ...")
    result = await process_asset(asset, task)
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
