"""FFmpeg helper — 从输入(任意 ffmpeg 支持格式)抽 wav。

调用方约定:
- input_path 可以是 mp4/webm/m3u8 URL/本地路径
- output_path 必须 .wav 后缀
- 失败时调用方负责决策(降级 / 报警);半截 wav 会被本模块清掉
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path


logger = logging.getLogger(__name__)


def extract_audio(input_path: Path, output_path: Path) -> None:
    """ffmpeg 抽 input → wav(output_path)。

    Args:
        input_path: 任意 ffmpeg 支持格式(mp4/webm/m3u8/...)
        output_path: 目标 wav 路径(需 .wav 后缀)

    Raises:
        RuntimeError: ffmpeg 返回非 0
        FileNotFoundError: ffmpeg 二进制缺失或 input 不存在

    失败语义: 半截 wav 在 finally 里被删,避免 3 小时视频抽到一半崩了
    留 345MB 残文件占磁盘。
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-ar", "16000", "-ac", "1", "-f", "wav", str(output_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"extract_audio failed: {input_path} → {output_path}: {proc.stderr}"
            )
    except Exception:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                logger.warning("清理半截 wav 失败 %s,继续", output_path)
        raise
