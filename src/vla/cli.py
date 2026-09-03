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
from pathlib import Path
from typing import Any, Callable

import typer
import yaml

from vla.utils.bvid import extract_bvid

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


@app.command()
def doctor() -> None:
    """检测本机环境:Python、ffmpeg、核心 Python 包、.env、配置。"""
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

    all_ok = True
    for name, ok, detail in checks:
        mark = "OK" if ok else "FAIL"
        typer.echo(f"[{mark}] {name}: {detail}")
        if not ok:
            all_ok = False

    if not all_ok:
        raise typer.Exit(code=1)


# ---------------- assemble helper ----------------


def _assemble_components(cfg_path: Path) -> dict:
    """从 config 装配所有依赖(用于 CLI 各命令)。

    ⚠️ Phase 8 CLI 是结构性占位 — 完整 Phase 3 字幕策略 + Phase 2 视频源工厂
    集成在 Phase 9 E2E 阶段落地。这里 text_provider 用 stub,process/batch
    命令跑起来后只走框架,真实字幕取需要外部组装(text_provider 注入)。
    """
    from vla.config import VLAConfig
    from vla.llm.client import LLMClient
    from vla.log.transcription_log import TranscriptionLog
    from vla.quality.checker import QualityChecker
    from vla.state.history import HistoryManager
    from vla.state.plugin_status import PluginStatus
    from vla.state.quota import QuotaManager
    from vla.summary.llm_summarizer import LLMSummarizer
    from vla.ui.macos_notify import MacOSNotifier

    cfg = VLAConfig.from_yaml(cfg_path)

    log = TranscriptionLog(cfg.logging.log_dir)
    history = HistoryManager(cfg.history.file)
    quota = QuotaManager(cfg)
    summarizer = LLMSummarizer(
        LLMClient(cfg.llm_client, model=cfg.summary.model),
        cfg.summary.notes_file,
    )
    summarizer.cfg = cfg
    checker = QualityChecker(cfg)
    checker.set_llm(LLMClient(cfg.llm_client, model=cfg.quality_check.model))
    notifier = MacOSNotifier()
    plugin_status = PluginStatus()

    return {
        "cfg": cfg,
        "log": log,
        "history": history,
        "quota": quota,
        "summarizer": summarizer,
        "notifier": notifier,
        "plugin_status": plugin_status,
        "checker": checker,
    }


def _stub_text_provider(task):
    """CLI 占位 text_provider — 在 process/batch 没注入真实 provider 时使用。

    真实集成在 main_provider.build_text_provider;通过 --real-provider 标志启用。
    """
    raise NotImplementedError(
        f"text_provider 是 stub:{task.title}。"
        f"完整 Phase 3 字幕策略 + Phase 2 视频源工厂集成在 main_provider.build_text_provider;"
        f"用 --real-provider 标志启用。"
    )


def _build_real_text_provider(
    cfg: VLAConfig, *, notifier: Any, plugin_status: Any,
) -> Callable:
    """装配真实 text_provider(Phase 9 完整集成)。

    Args:
        cfg: VLAConfig
        notifier: MacOSNotifier(必需 — FR-2.5/2.6 弹窗)
        plugin_status: PluginStatus(必需 — session 单例)
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

    provider = (
        _build_real_text_provider(
            comps["cfg"],
            notifier=comps["notifier"],
            plugin_status=comps["plugin_status"],
        )
        if real_provider else _stub_text_provider
    )

    agent = VideoLearningAgent(
        cfg=comps["cfg"],
        checker=comps["checker"],
        log=comps["log"],
        history=comps["history"],
        quota=comps["quota"],
        summarizer=comps["summarizer"],
        notifier=comps["notifier"],
        text_provider=provider,
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

    provider = (
        _build_real_text_provider(
            comps["cfg"],
            notifier=comps["notifier"],
            plugin_status=comps["plugin_status"],
        )
        if real_provider else _stub_text_provider
    )

    agent = VideoLearningAgent(
        cfg=comps["cfg"],
        checker=comps["checker"],
        log=comps["log"],
        history=comps["history"],
        quota=comps["quota"],
        summarizer=comps["summarizer"],
        notifier=comps["notifier"],
        text_provider=provider,
        plugin_status=comps["plugin_status"],
    )

    try:
        stats = agent.run(tasks)
    except NotImplementedError as e:
        typer.echo(f"⚠️ {e}")
        raise typer.Exit(code=2)

    typer.echo(f"\n📊 批量处理结果:{stats}")


# ---------------- summarize ----------------


@app.command()
def summarize(
    config_path: Path = typer.Option(CONFIG_FILE, "--config", help="配置文件路径"),
    clear: bool = typer.Option(True, "--clear/--keep", help="总结后是否清空 transcribed/"),
) -> None:
    """手动触发总结(无需等 6h 配额)。读 transcribed/*.txt → LLM 总结 → 写 notes_file。"""
    comps = _assemble_components(config_path)

    content = comps["summarizer"].summarize_batch(
        comps["log"].transcribed_dir,
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