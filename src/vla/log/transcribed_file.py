"""字幕原文落盘 + 读取(SSOT: spec §B #9,2026-09-03)。

消除 TranscriptionLog.save_transcribed 与 LLMSummarizer._parse_file 之间的
regex 耦合:写和读都走同一份 dataclass + 函数,任何字段命名变更只需改这里。

On-disk format (FR-7.7):
    # <title>
    来源:<source> | 质量:<score>/100 | 时长:<sec>s

    <text>
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_HEADER_TITLE_RE = re.compile(r"^#\s+(.+)$")
_META_TOKEN_RE = re.compile(r"(来源|质量|时长):\s*([^|\s]+(?:[^\n|]*))?")


@dataclass
class TranscribedItem:
    """一条字幕原文文件的内容 + 元元数据(2026-09-03 SSOT)。

    与 summary/llm_summarizer.py 里同名 dataclass 字段一致;后者委托本类。
    """

    title: str
    source: str
    quality_score: int
    duration_sec: int
    text: str
    path: Path
    mtime: float


def write(path: Path, item: TranscribedItem) -> Path:
    """按统一格式写入磁盘。返回写入的路径(便于 caller 链式调用)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {item.title}\n"
        f"来源:{item.source} | 质量:{item.quality_score}/100 | 时长:{item.duration_sec}s\n\n"
    )
    path.write_text(header + item.text, encoding="utf-8")
    return path


def read(path: Path) -> TranscribedItem:
    """读回 TranscribedItem。缺失字段用默认值(向后兼容旧文件)。"""
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    lines = content.split("\n", 1)
    title = path.stem
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
    meta_line = lines[1].split("\n", 1)[0] if len(lines) > 1 else ""
    source = "whisper"
    quality_score = 0
    duration_sec = 0
    for token in meta_line.split("|"):
        token = token.strip()
        if token.startswith("来源:"):
            source = token.removeprefix("来源:").strip()
        elif "质量:" in token:
            m = re.search(r"质量:(\d+)", token)
            if m:
                quality_score = int(m.group(1))
        elif "时长:" in token:
            m = re.search(r"时长:(\d+)", token)
            if m:
                duration_sec = int(m.group(1))
    if "\n\n" in content:
        text = content.split("\n\n", 1)[1].strip()
    else:
        text = content.strip()
    return TranscribedItem(
        title=title,
        source=source,
        quality_score=quality_score,
        duration_sec=duration_sec,
        text=text,
        path=path,
        mtime=path.stat().st_mtime,
    )