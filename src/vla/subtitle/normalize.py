"""字幕归一化层(SSOT: spec §E Sub-1/Sub-2,2026-09-03)。

- 把 SRT/VTT/ASS 解析集中在一处(消除 SubtitleRefiner 与旧 Screen Recorder
  模块的格式耦合)
- 输出统一的 Segment(start, end, text) 列表
- SubtitleRefiner 现在只接 list[Segment],不再管格式
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, model_validator


class FormatHint(str, Enum):
    AUTO = "auto"
    SRT = "srt"
    VTT = "vtt"
    ASS = "ass"
    JSON = "json"


class Segment(BaseModel):
    """一条字幕片段。start/end 单位:秒(浮点)。"""

    start: float
    end: float
    text: str

    @model_validator(mode="after")
    def _check_order(self) -> "Segment":
        if self.end < self.start:
            raise ValueError(f"end ({self.end}) < start ({self.start})")
        return self


_SRT_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_VTT_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.(\d{1,3})")
_ASS_TS = re.compile(r"(\d{1,}):(\d{2}):(\d{2})\.(\d{1,2})")


def _ts_to_seconds(match: re.Match, *, ms_group: int) -> float:
    h, m, s, frac = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac) / (10 ** len(frac))


def _detect_format(path: Path, content_head: str) -> FormatHint:
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "srt":
        return FormatHint.SRT
    if suffix == "vtt":
        return FormatHint.VTT
    if suffix == "ass" or suffix == "ssa":
        return FormatHint.ASS
    if content_head.startswith("WEBVTT"):
        return FormatHint.VTT
    if "[Script Info]" in content_head or "[Events]" in content_head:
        return FormatHint.ASS
    if _SRT_TS.search(content_head):
        return FormatHint.SRT
    raise ValueError(f"cannot detect subtitle format from {path}")


def _parse_srt(text: str) -> list[Segment]:
    """SRT: index\\nHH:MM:SS,mmm --> HH:MM:SS,mmm\\ntext\\n\\n"""
    out: list[Segment] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = block.split("\n")
        if len(lines) < 2:
            continue
        ts_line = next((ln for ln in lines if "-->" in ln), None)
        if not ts_line:
            continue
        ts_match = re.search(
            r"(\d{2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{1,3})",
            ts_line,
        )
        if not ts_match:
            continue
        start = _ts_to_seconds(_SRT_TS.search(ts_match.group(1)), ms_group=4)  # type: ignore[arg-type]
        end = _ts_to_seconds(_SRT_TS.search(ts_match.group(2)), ms_group=4)  # type: ignore[arg-type]
        ts_idx = lines.index(ts_line)
        body = "\n".join(lines[ts_idx + 1 :]).strip()
        if body:
            out.append(Segment(start=start, end=end, text=body))
    return out


def _parse_vtt(text: str) -> list[Segment]:
    """VTT: 去掉 WEBVTT 头后跟 SRT 一样的 cue 语法。

    _parse_srt 的时间戳正则已经接受 `.` 和 `,`,所以这里只剥头部、不替换正文里的 `.`,
    否则会把 "Hello world." 这种正常文本里的句号误改成逗号。
    """
    body = re.sub(r"^WEBVTT.*?\n\n", "", text, count=1, flags=re.DOTALL)
    return _parse_srt(body)


def _parse_ass(text: str) -> list[Segment]:
    """ASS: 只解析 Dialogue 行,text 是最后一列(逗号分隔 10 个字段)。

    ASS Dialogue 字段顺序:
        Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
    所以 timestamp 在 parts[1]/parts[2],text 在 parts[9];parts[0] 是 Layer。

    局限(纯 regex,无第三方库):
    - 不处理 override 块(只读 [Events])
    - 不解析 \\N / \\n 之外的样式 override(已剥离 {…})
    - 时间戳最多 2 位百分秒
    """
    out: list[Segment] = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line[len("Dialogue:"):].lstrip().split(",", 9)
        if len(parts) < 10:
            continue
        # parts[0] = Layer, parts[1] = Start, parts[2] = End, parts[9] = Text
        start_str, end_str = parts[1], parts[2]
        _rest = parts[3:9]
        text_part = parts[9]
        m1 = _ASS_TS.match(start_str)
        m2 = _ASS_TS.match(end_str)
        if not (m1 and m2):
            continue
        start = _ts_to_seconds(m1, ms_group=4)  # type: ignore[arg-type]
        end = _ts_to_seconds(m2, ms_group=4)  # type: ignore[arg-type]
        text_part = re.sub(r"\{[^}]*\}", "", text_part).replace("\\N", "\n").strip()
        if text_part:
            out.append(Segment(start=start, end=end, text=text_part))
    return out


def parse_subtitle(
    path: Path, *, format_hint: str | FormatHint | None = None
) -> list[Segment]:
    """读取字幕文件,返回归一化的 Segment 列表。

    Args:
        path: 字幕文件路径
        format_hint: 强制指定格式("srt"/"vtt"/"ass"/"json"/"auto");
            None 或 "auto" 时按扩展名 + 内容启发式识别。
    """
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    head = content[:512]
    if format_hint in (None, FormatHint.AUTO, "auto"):
        fmt = _detect_format(path, head)
    else:
        fmt = FormatHint(format_hint)
    if fmt == FormatHint.SRT:
        return _parse_srt(content)
    if fmt == FormatHint.VTT:
        return _parse_vtt(content)
    if fmt == FormatHint.ASS:
        return _parse_ass(content)
    raise ValueError(f"unsupported format: {fmt}")
