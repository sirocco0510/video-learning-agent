"""扫用户手动下载目录(SSOT: F2-10 2026-09-08)。

约定:
- 根目录 cfg.audio.downloads_dir(用户配置)
- 每天一个子目录:YYYY-MM-DD/(代码自动 mkdir)
- 用户把 Tab Audio Recorder 录完的 webm 手动下载到今天的 YYYY-MM-DD/ 文件夹
- sidecar 标识:<audio_id>.transcribed.txt(空文件,转写成功后 touch)
- 转写输出:<audio_id>.txt(同文件夹,由 transcriber.transcribe(audio_path, out_dir=today_dir) 落盘)
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path


logger = logging.getLogger(__name__)


def find_today_dir(downloads_root: Path) -> Path:
    """返回 downloads_root/YYYY-MM-DD/ 路径,mkdir parents exist_ok。

    Args:
        downloads_root: 用户配置的根目录(cfg.audio.downloads_dir)

    Returns:
        downloads_root/<today>/ 路径(已确保存在)
    """
    today = date.today().isoformat()  # "2026-09-08"
    today_dir = downloads_root / today
    today_dir.mkdir(parents=True, exist_ok=True)
    return today_dir


def scan_untranscribed_audio(today_dir: Path) -> Path | None:
    """扫 today_dir/*.webm,过滤已有 .transcribed.txt,返回第 1 个未转写。

    顺序:`sorted()` 按文件名字典序,F2-10 单次只处理一个 → 保证多次跑选同文件。

    Args:
        today_dir: cfg.audio.downloads_dir/YYYY-MM-DD/

    Returns:
        第 1 个未转写 .webm 路径;无 → None
    """
    if not today_dir.exists():
        logger.warning("audio_scan: today_dir 不存在: %s", today_dir)
        return None
    for webm in sorted(today_dir.glob("*.webm")):
        sidecar = webm.with_suffix(".transcribed.txt")
        if sidecar.exists():
            continue  # 已转写
        logger.info("audio_scan: 找到未转写音频 %s", webm)
        return webm
    logger.info("audio_scan: 无未转写音频(%s)", today_dir)
    return None
