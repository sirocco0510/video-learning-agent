"""typer CLI 入口(SSOT: requirements.md 第九章 CLI 接口 + Phase 8)。

命令:
- vla doctor             — 环境检查
- vla process            — 处理单条视频
- vla batch              — 批量处理(YAML/JSON 任务列表)
- vla summarize          — 手动触发总结(无需等 6h)

完整数据流在 src/vla/main.py + 依赖模块。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import typer
import yaml

from vla.utils.bvid import extract_bvid

if TYPE_CHECKING:  # 仅注解用:VLAConfig 真正的 import 在各命令函数内部(延迟加载)
    from vla.config import VLAConfig

app = typer.Typer(no_args_is_help=True, help="视频挂机学习 Agent")

# 项目根目录:src/vla/cli.py → 上两级
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "config" / "vla.yaml"


@app.callback()
def _root() -> None:
    """视频挂机学习 Agent。"""
    # 2026-09-02 修正:把 .env 加载进 os.environ,LLMClient 等模块直接读 os.environ 才能拿到 key
    _load_env_into_environ()


def _load_env_into_environ() -> None:
    """手动 parse .env 文件塞进 os.environ(LLMClient 只看 os.environ)。

    与 _parse_env_value 同款:简单 KEY=VALUE 格式,不支持引号转义注释。
    """
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        # 已设置的环境变量优先级最高(支持 CI 覆盖)
        os.environ.setdefault(k, v)


# ---------------- doctor ----------------


def _parse_env_value(text: str, key: str) -> str | None:
    """从 .env 文本中读取 key 的值(简单 KEY=VALUE 格式,不做引号/转义/注释)。"""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1)
    # 去掉首尾成对引号
    if len(value) >= 2 and (
        (value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")
    ):
        value = value[1:-1]
    return value


def _check_terminal_notifier() -> tuple[bool, str]:
    """FR-2.28.2h:检测 terminal-notifier(可点通知 CLI,brew 装)。

    Returns (ok, message)。ok=False 时 caller 只 print warning,不 raise。
    不在 PATH 不阻塞 doctor(降级到 osascript 不可点通知)。
    """
    tn = shutil.which("terminal-notifier")
    if tn:
        return True, f"{tn}(截图完成通知可点击 → Finder)"
    return False, (
        "未安装(FR-2.28.2h 可点通知不可用,将降级为 osascript 不可点通知;"
        "安装:brew install terminal-notifier)"
    )


def _check_screenshot_tcc(driver: Any) -> tuple[bool, str]:
    """FR-2.28.2c `vla doctor` pre-warm:Q8=TCC 拒绝 → warn+continue,exit 0。

    Returns (ok, message)。ok=False 时 caller 只 print warning,不 raise,不 sys.exit(1)。

    Brief verbatim included unused `capture = ScreenCapture(save_dir=save_dir)`
    — dropped because _try() never touches capture. Function only probes
    driver.page for fullscreen capability.
    """
    import asyncio

    async def _try() -> bool:
        page = getattr(driver, "page", None)
        if page is None:
            return False
        try:
            await page.bring_to_front()
            await page.evaluate(
                "video.currentTime=0; video.pause(); video.requestFullscreen()"
            )
            return True
        except Exception:
            return False

    granted = asyncio.run(_try())
    if granted:
        return True, "屏幕录制权限 OK (FR-2.28.2c)"
    return False, (
        "WARN: 屏幕录制权限被拒 — 截图功能不可,但音频转写可继续。"
        "可在 系统设置 → 隐私与安全性 → 屏幕录制 授权。"
    )


@app.command()
def doctor(
    check_screenshot: bool = typer.Option(
        False, "--check-screenshot",
        help="FR-2.28.2c 屏幕录制权限 pre-warm (Q8=Warn)"
    ),
) -> None:
    """检测本机环境:Python、ffmpeg、核心 Python 包、.env、配置。

    `--check-screenshot`:Q8 规则下尝试 requestFullscreen;失败时 WARN,不影响 doctor 退出码。
    当前 `doctor` 不持有 browser driver,传 MagicMock() 让 _try() 走到 except
    返回 WARN(代表 "未接入真实浏览器");接入 driver 后改为传真实 driver 即可。
    """
    checks: list[tuple[str, bool, str]] = []

    py_ok = sys.version_info >= (3, 11)
    checks.append(("Python >= 3.11", py_ok, sys.version.split()[0]))

    ffmpeg = shutil.which("ffmpeg")
    checks.append(("ffmpeg", ffmpeg is not None, ffmpeg or "未找到"))

    for pkg in ("faster_whisper", "yt_dlp", "httpx", "typer"):
        try:
            __import__(pkg)
            checks.append((pkg, True, "已安装"))
        except ImportError:
            checks.append((pkg, False, "未安装"))

    if ENV_FILE.is_file():
        api_key = _parse_env_value(ENV_FILE.read_text(encoding="utf-8"), "OPENAI_API_KEY")
        if api_key:
            checks.append((".env + OPENAI_API_KEY", True, "已设置"))
        else:
            checks.append((".env + OPENAI_API_KEY", False, "key 为空或缺失"))
    else:
        checks.append((".env", False, f"未找到 {ENV_FILE.relative_to(PROJECT_ROOT)}"))

    if CONFIG_FILE.is_file():
        try:
            yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
            checks.append(("config/vla.yaml", True, "存在且合法"))
        except yaml.YAMLError as e:
            checks.append(("config/vla.yaml", False, f"YAML 解析失败: {e}"))
    else:
        checks.append(
            ("config/vla.yaml", False, f"未找到 {CONFIG_FILE.relative_to(PROJECT_ROOT)}")
        )

    # yt-dlp 可用性探测(F2-1:AudioSourceFactory.is_downloadable 烟雾测试)
    # —— FR-2.14 path ① 兜底链路前置检查;失败时主调度降级到 path ② 录屏
    # WARN-only(Q8 同款策略):probe 失败不阻塞 doctor,因为:
    # 1. yt-dlp probe 走真实网络 → 受地区/防火墙影响大
    # 2. yt-dlp package 已 OK + path ② 兜底就够(FR-2.14 v3 设计)
    audio_warn = None
    try:
        from vla.audio.source_factory import AudioSourceFactory

        factory = AudioSourceFactory(simulate_timeout_sec=10)
        probe_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        t0 = time.monotonic()
        audio_ok = factory.is_downloadable(probe_url)
        elapsed = time.monotonic() - t0
        if audio_ok:
            audio_detail = f"yt-dlp available (simulate OK in {elapsed:.1f}s)"
        else:
            audio_detail = "yt-dlp MISSING — path ① 不可用,所有 URL 走 path ②"
            audio_warn = audio_detail
    except Exception as e:  # pragma: no cover — 防御性,probe 异常不阻塞 doctor
        audio_detail = f"yt-dlp probe 异常({type(e).__name__}):{e}"
        audio_warn = audio_detail

    all_ok = True
    for name, ok, detail in checks:
        mark = "OK" if ok else "FAIL"
        typer.echo(f"[{mark}] {name}: {detail}")
        if not ok:
            all_ok = False

    # FR-2.28.2c pre-warm: Q8 warn-only, 不 raise 不 sys.exit
    if check_screenshot:
        from unittest.mock import MagicMock

        ok, msg = _check_screenshot_tcc(driver=MagicMock())
        mark = "OK" if ok else "WARN"
        typer.echo(f"[{mark}] screenshot_tcc: {msg}")
        # Q8: 不 raise,不 sys.exit(1) — 仅 print

    # audio_source_factory 单独走 WARN-only:probe 失败不阻塞 doctor
    audio_mark = "WARN" if audio_warn else "OK"
    typer.echo(f"[{audio_mark}] audio_source_factory: {audio_detail}")

    # FR-2.28.2h: terminal-notifier(可点通知 CLI)检测 — WARN-only
    # 不在 PATH 不阻塞 doctor(降级到 osascript 不可点通知)
    tn_ok, tn_msg = _check_terminal_notifier()
    tn_mark = "OK" if tn_ok else "WARN"
    typer.echo(f"[{tn_mark}] terminal-notifier: {tn_msg}")

    if not all_ok:
        raise typer.Exit(code=1)


# ---------------- assemble helper ----------------


def _assemble_components(cfg_path: Path) -> dict:
    """从 config 装配所有依赖(用于 CLI 各命令)。

    ⚠️ Phase 8 CLI 是结构性占位 — 完整 Phase 3 字幕策略 + Phase 2 视频源工厂
    集成在 Phase 9 E2E 阶段落地。这里 fetch_asset/process_asset 用 stub,
    process/batch 命令跑起来后只走框架,真实字幕取需要外部组装(注入)。
    """
    from vla.config import VLAConfig
    from vla.llm.client import LLMClient
    from vla.log.transcription_log import TranscriptionLog
    from vla.state.history import HistoryManager
    from vla.state.plugin_status import PluginStatus
    from vla.state.quota import QuotaManager
    from vla.summary.llm_summarizer import LLMSummarizer
    from vla.ui.notifier import create_notifier

    cfg = VLAConfig.from_yaml(cfg_path)

    log = TranscriptionLog(cfg.logging.log_dir)
    history = HistoryManager(cfg.history.file)
    quota = QuotaManager(cfg)
    summarizer = LLMSummarizer(
        LLMClient(cfg.llm_client, model=cfg.llm.summary_model),
        cfg.summary.notes_file,
    )
    summarizer.cfg = cfg
    # 2026-09-10 轻量化:跨平台 notifier — macOS 走 MacOSNotifier,
    # Windows/Linux/CI 走 NullNotifier(策略 ② 弹窗已被 TabAudioRecorder
    # 移除,Windows 上 ask_open_browser 直接返 "skip")。
    notifier = create_notifier()
    plugin_status = PluginStatus()

    return {
        "cfg": cfg,
        "log": log,
        "history": history,
        "quota": quota,
        "summarizer": summarizer,
        "notifier": notifier,
        "plugin_status": plugin_status,
    }


def _stub_fetch_asset(task) -> None:
    """CLI 占位 fetch_asset — 在 process/batch 没注入真实 provider 时使用。

    真实集成在 main_provider.build_text_provider;通过 --real-provider 标志启用。
    """
    raise NotImplementedError(
        f"fetch_asset 是 stub:{task.title}。"
        f"完整 Phase 3 字幕策略 + Phase 2 视频源工厂集成在 main_provider.build_text_provider;"
        f"用 --real-provider 标志启用。"
    )


def _stub_process_asset(asset, task) -> None:
    """CLI 占位 process_asset — 同 _stub_fetch_asset。"""
    raise NotImplementedError(
        f"process_asset 是 stub:{task.title}。"
        f"用 --real-provider 标志启用完整链路。"
    )


def _build_real_provider(
    cfg: VLAConfig, *, notifier: Any, plugin_status: Any,
) -> tuple[Callable, Callable]:
    """装配真实 fetch_asset + process_asset(Phase 9 完整集成)。

    Args:
        cfg: VLAConfig
        notifier: NotifierLike(MacOSNotifier / NullNotifier,跨平台)
        plugin_status: PluginStatus(必需 — session 单例)

    Returns:
        (fetch_asset, process_asset) — Task 9 二元组接口
    """
    from vla.main_provider import build_text_provider
    return build_text_provider(
        cfg, notifier=notifier, plugin_status=plugin_status,
    )


# ---------------- process ----------------


@app.command()
def process(
    url: str = typer.Option(..., "--url", help="视频 URL"),
    title: str = typer.Option(..., "--title", help="视频标题"),
    duration: int = typer.Option(..., "--duration", help="估计时长(秒)"),
    bvid: str = typer.Option("", "--bvid", help="B站 bvid(可选,默认从 URL 提取)"),
    group: str = typer.Option("default", "--group", help="视频组 ID"),
    real_provider: bool = typer.Option(
        False, "--real-provider",
        help="使用真实 Phase 3 字幕策略 + Phase 2 视频源工厂(需要 driver/recorder 配置)",
    ),
    config_path: Path = typer.Option(CONFIG_FILE, "--config", help="配置文件路径"),
) -> None:
    """处理单条视频:下载/录屏 + 转写 + 质量门控 + 6h 触发总结。

    默认用 stub text_provider(返回 NotImplementedError);加 --real-provider
    启用真实字幕三级策略 + 视频源兜底链路。
    """
    from vla.main import VideoLearningAgent
    from vla.models import VideoTask

    comps = _assemble_components(config_path)

    if not bvid:
        extracted = extract_bvid(url)
        bvid = extracted or f"local_{abs(hash(url))}"

    task = VideoTask(
        id=bvid,
        title=title,
        url=url,
        expected_duration=duration,
        group_id=group,
        group_title=group,
    )

    fetch_asset, process_asset = (
        _build_real_provider(
            comps["cfg"],
            notifier=comps["notifier"],
            plugin_status=comps["plugin_status"],
        )
        if real_provider else (_stub_fetch_asset, _stub_process_asset)
    )

    agent = VideoLearningAgent(
        cfg=comps["cfg"],
        log=comps["log"],
        history=comps["history"],
        quota=comps["quota"],
        summarizer=comps["summarizer"],
        notifier=comps["notifier"],
        fetch_asset=fetch_asset,
        process_asset=process_asset,
        plugin_status=comps["plugin_status"],
    )

    try:
        stats = agent.run([task])
    except NotImplementedError as e:
        typer.echo(f"⚠️ {e}")
        typer.echo("💡 加 --real-provider 启用完整链路(需要 driver/recorder)。")
        raise typer.Exit(code=2)

    typer.echo(f"\n📊 处理结果:{stats}")


# ---------------- batch ----------------


@app.command()
def batch(
    tasks_file: Path = typer.Option(..., "--tasks", help="任务列表 YAML/JSON 文件"),
    real_provider: bool = typer.Option(
        False, "--real-provider",
        help="使用真实 Phase 3 字幕策略 + Phase 2 视频源工厂",
    ),
    config_path: Path = typer.Option(CONFIG_FILE, "--config", help="配置文件路径"),
) -> None:
    """批量处理任务列表(YAML/JSON 格式)。

    文件格式(YAML):
        tasks:
          - id: BV1xxx
            title: 视频标题
            url: https://...
            expected_duration: 1800
            group_id: Python基础
            group_title: Python基础
    """
    from vla.main import VideoLearningAgent
    from vla.models import VideoTask

    comps = _assemble_components(config_path)

    text = tasks_file.read_text(encoding="utf-8")
    if tasks_file.suffix in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)

    tasks = [VideoTask(**item) for item in data["tasks"]]

    fetch_asset, process_asset = (
        _build_real_provider(
            comps["cfg"],
            notifier=comps["notifier"],
            plugin_status=comps["plugin_status"],
        )
        if real_provider else (_stub_fetch_asset, _stub_process_asset)
    )

    agent = VideoLearningAgent(
        cfg=comps["cfg"],
        log=comps["log"],
        history=comps["history"],
        quota=comps["quota"],
        summarizer=comps["summarizer"],
        notifier=comps["notifier"],
        fetch_asset=fetch_asset,
        process_asset=process_asset,
        plugin_status=comps["plugin_status"],
    )

    try:
        stats = agent.run(tasks)
    except NotImplementedError as e:
        typer.echo(f"⚠️ {e}")
        raise typer.Exit(code=2)

    typer.echo(f"\n📊 批量处理结果:{stats}")


# ---------------- learn ----------------


def _build_learn_provider(
    cfg: VLAConfig, *, spider: Any, comps: dict,
) -> tuple[Callable, Callable]:
    """装配 learn 用的真实 fetch_asset / process_asset(bill-jc 课程路径)。

    与 `_build_real_provider` 的区别 —— **Refiner 必须早于 QualityChecker**:
      build_text_provider 的 auto-construct 会建 `StreamingTranscriber(cfg)`
      **不带 refiner**,于是 Level 4 云端清理被静默跳过(streaming.py
      `_maybe_refine` 在 self.refiner is None 时直接 return),未清理的文本
      进质量门控打分 → 2026-09-10 实测 score 45(不过)vs 注入后 88(过)。
      process_asset 的 Step 4 也 refine,但那在质量门控**之后**,救不回 fail。

    所以这里像 spike 一样自建 refiner(with LLM)+ transcriber,再传进去。
    """
    from vla.llm.client import LLMClient
    from vla.main_provider import build_text_provider
    from vla.quality.refiner import SubtitleRefiner
    from vla.transcribe.streaming import StreamingTranscriber

    refiner = None
    if cfg.quality_check.refine_enabled:
        refiner = SubtitleRefiner(
            cfg, LLMClient(cfg.llm_client, model=cfg.quality_check.refine_model)
        )
    transcriber = StreamingTranscriber(cfg, refiner=refiner)

    return build_text_provider(
        cfg,
        transcriber=transcriber,
        notifier=comps["notifier"],
        plugin_status=comps["plugin_status"],
        refiner=refiner,
        internal_spider=spider,
        # 2026-09-10 修复:这里原来传 `summarizer=comps["summarizer"]`,即把
        # **LLMSummarizer**(6h 批量总结,FR-5/FR-9)注进了单视频摘要槽位
        # (FR-2.15d 要的是 VideoSummarizer)。注入优先于兜底,于是
        # `summarize_one` AttributeError 被 process_asset 的宽 except 吞成一行
        # warning → 真机整批 5 条视频一条摘要都没产出。
        # 不传 ⇒ 走 build_text_provider 的兜底 `VideoSummarizer(cfg)`。
        # 形参也已改名 `video_summarizer`,误传会当场 TypeError 而非静默失败。
    )


@app.command()
def learn(
    college_id: str = typer.Option(
        ..., "--college-id",
        help="collegeId —— 课程目录页 URL 的 cid 参数",
    ),
    catalog_id: str = typer.Option(
        ..., "--catalog-id",
        help="catalogId —— 课程目录页 URL 的 catalogId 参数",
    ),
    limit: int = typer.Option(
        10, "--limit", help="每页取多少条(同时是翻页步长)",
    ),
    cdp_url: str = typer.Option(
        "http://localhost:9222", "--cdp-url",
        help="Chrome CDP debug URL(cookie/JWT 从已登录的 Chrome 借)",
    ),
    resolution: str = typer.Option(
        "720p", "--resolution", help="视频分辨率档(360p / 480p / 720p / 1080p)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="只翻页列出任务,不转写(不装配转写组件)",
    ),
    config_path: Path = typer.Option(CONFIG_FILE, "--config", help="配置文件路径"),
) -> None:
    """bill-jc 课程目录批量转写(FR-11,2026-09-10)。

    从课程目录页翻页取任务,逐页跑既有单条转写链路,直到累计配额(默认 6h)
    用尽或目录翻完。已转写的视频按 logs/transcribed_history.jsonl 自动跳过。

    目录页 URL 里两个参数都要复制过来:
      https://b-learning.bill-jc.com/kng/#/list?catalogId=<catalogId>&cid=<collegeId>&...
                                          ^^^^^^^^^^^          ^^^^^^^^^^^
                                          --catalog-id         --college-id

    前置:Chrome 已用 --remote-debugging-port=9222 启动并登录了 bill-jc。
    """
    import asyncio

    from vla.learn import iter_course_tasks, run_course_batch, with_duration_resolution
    from vla.main import VideoLearningAgent
    from vla.subtitle.internal_site_spider import InternalSiteSpider

    comps = _assemble_components(config_path)
    spider = InternalSiteSpider(
        cdp_url=cdp_url, college_id=college_id, resolution=resolution
    )

    if dry_run:
        # 只翻页列任务:不装配 transcriber / refiner / LLM,零凭据零副作用
        from vla.state.history import HistoryManager

        async def _list() -> tuple[int, int]:
            total = 0
            done = 0
            async for page in iter_course_tasks(spider, catalog_id, limit):
                for t in page:
                    total += 1
                    # 与 agent.run 的去重键**完全一致**(FR-9.6 / FR-10.2):
                    # main.py:_url_key 也是调这个 public static method
                    key = HistoryManager.make_url_key(t.group_id, t.id)
                    if comps["history"].is_already_done(key):
                        done += 1
                        typer.echo(f"  [{total:>4}] ⏭️  {t.id}  {t.title}(已转写)")
                    else:
                        typer.echo(f"  [{total:>4}] {t.id}  {t.title}")
            return total, done

        total, done = asyncio.run(_list())
        typer.echo(
            f"\n📋 dry-run:目录共 {total} 条 / 已转写 {done} 条 → "
            f"本次将处理 {total - done} 条"
        )
        return

    fetch_asset, process_asset = _build_learn_provider(
        comps["cfg"], spider=spider, comps=comps,
    )

    agent = VideoLearningAgent(
        cfg=comps["cfg"],
        log=comps["log"],
        history=comps["history"],
        quota=comps["quota"],
        summarizer=comps["summarizer"],
        notifier=comps["notifier"],
        fetch_asset=with_duration_resolution(fetch_asset),
        process_asset=process_asset,
        plugin_status=comps["plugin_status"],
        # 不传 browser_driver / screenshot_controller:批量路径不需要截图,
        # 且 main.py:_stop_chrome_session 会无条件对已注入的 driver 调
        # disconnect()(ctx.close + browser.close)→ 每页一次,可能关掉
        # 下一页还要借 cookie 的那个 Chrome 会话。
    )

    stats = asyncio.run(
        run_course_batch(spider=spider, agent=agent, catalog_id=catalog_id, limit=limit)
    )

    typer.echo(
        f"\n📊 课程批量结果:翻页 {stats['pages']} 页 / "
        f"处理 {stats['processed']} / 通过 {stats['passed']} / 失败 {stats['failed']} / "
        f"跳过(已转写){stats['skipped']} / 触发总结 {stats['summarized']}"
    )


# ---------------- summarize ----------------


@app.command()
def summarize(
    config_path: Path = typer.Option(CONFIG_FILE, "--config", help="配置文件路径"),
    clear: bool = typer.Option(True, "--clear/--keep", help="总结后是否清空 transcribed/"),
) -> None:
    """手动触发总结(无需等 6h 配额)。读 transcribed/*.txt → LLM 总结 → 写 notes_file。"""
    comps = _assemble_components(config_path)

    content = comps["summarizer"].summarize_batch(
        comps["log"].transcribed_root,
        group_title=comps["cfg"].summary.notes_section_header.lstrip("# ").strip() or None,
        clear_after=clear,
    )
    if not content:
        typer.echo("📭 transcribed/ 目录为空,无字幕可总结")
        raise typer.Exit(code=0)

    comps["summarizer"].write_to_notes(content)
    typer.echo(f"✅ 总结已写入 {comps['cfg'].summary.notes_file}")


if __name__ == "__main__":
    app()