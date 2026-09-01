"""typer CLI 入口。完整命令在 Phase 8 实现。"""

import re
import shutil
import sys
from pathlib import Path

import typer
import yaml

app = typer.Typer(no_args_is_help=True, help="视频挂机学习 Agent")

# 项目根目录:src/vla/cli.py → 上两级
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "config" / "vla.yaml"


@app.callback()
def _root() -> None:
    """视频挂机学习 Agent。"""


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
    """检测本机环境：Python、ffmpeg、核心 Python 包、.env、配置。"""
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

    # .env 文件 + OPENAI_API_KEY
    if ENV_FILE.is_file():
        api_key = _parse_env_value(ENV_FILE.read_text(encoding="utf-8"), "OPENAI_API_KEY")
        if api_key:
            checks.append((".env + OPENAI_API_KEY", True, "已设置"))
        else:
            checks.append((".env + OPENAI_API_KEY", False, "key 为空或缺失"))
    else:
        checks.append((".env", False, f"未找到 {ENV_FILE.relative_to(PROJECT_ROOT)}"))

    # config/vla.yaml
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


if __name__ == "__main__":
    app()
