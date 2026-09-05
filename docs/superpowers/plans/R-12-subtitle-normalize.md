# R.12 — Subtitle `normalize` Module (Format Hints)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract subtitle normalization into `subtitle/normalize.py`. Consolidate format-specific parsing hints (SRT/VTT/ASS) into one place, so `SubtitleRefiner` no longer needs to know about formats — only normalized segments do.

**Architecture:**
- `subtitle/normalize.py`:
  - `class Segment(BaseModel)` — fields: `start: float`, `end: float`, `text: str`
  - `def parse_subtitle(path: Path, *, format_hint: str | None = None) -> list[Segment]`
  - `class FormatHint(str, Enum)`: `auto | srt | vtt | ass | json`
  - Heuristic format detection from extension / first bytes when `format_hint is None`
- `SubtitleRefiner.refine(segments: list[Segment]) -> list[Segment]` (replaces current `refine(subtitle_path: Path)` signature)
- `pipeline.process` calls `parse_subtitle` first, then `refiner.refine`

**Tech Stack:** pydantic v2, regex, stdlib

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §E Sub-1, Sub-2

## Global Constraints

- Public signatures of `SubtitleRefiner.refine` change; update all callers in `pipeline.process`
- `srt`/`vtt` parsing handled in this module (was duplicated in `refiner` + `browser_record`)
- `ass` parser may use regex only (no third-party lib); document limitations
- 35+ tests stay green

---

### Task 1: Write failing tests for `normalize`

**Files:**
- Create: `tests/test_subtitle_normalize.py`

**Interfaces:**
- `parse_subtitle(path: Path, *, format_hint: str | None = None) -> list[Segment]`
- `Segment(start: float, end: float, text: str)`

- [ ] **Step 1: Write tests**

```python
# tests/test_subtitle_normalize.py
from pathlib import Path

import pytest

from vla.subtitle.normalize import FormatHint, Segment, parse_subtitle


SRT_FIXTURE = """1
00:00:01,000 --> 00:00:04,000
Hello world.

2
00:00:05,000 --> 00:00:08,000
第二行
中文测试
"""


VTT_FIXTURE = """WEBVTT

00:00:01.000 --> 00:00:04.000
Hello world.

00:00:05.000 --> 00:00:08.000
第二行
"""


ASS_FIXTURE = """[Script Info]
ScriptType: v4.00+

[Events]
Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,Hello world.
Dialogue: 0,0:00:05.00,0:00:08.00,Default,,0,0,0,,第二行
"""


class TestSrtParse:
    def test_basic_srt(self, tmp_path: Path):
        p = tmp_path / "x.srt"
        p.write_text(SRT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p, format_hint="srt")
        assert len(segs) == 2
        assert segs[0].start == 1.0
        assert segs[0].end == 4.0
        assert segs[0].text == "Hello world."

    def test_multiline_text_concatenated(self, tmp_path: Path):
        p = tmp_path / "x.srt"
        p.write_text(SRT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p, format_hint="srt")
        assert segs[1].text == "第二行\n中文测试"


class TestVttParse:
    def test_basic_vtt(self, tmp_path: Path):
        p = tmp_path / "x.vtt"
        p.write_text(VTT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p, format_hint="vtt")
        assert len(segs) == 2
        assert segs[1].text == "第二行"


class TestAssParse:
    def test_basic_ass(self, tmp_path: Path):
        p = tmp_path / "x.ass"
        p.write_text(ASS_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p, format_hint="ass")
        assert len(segs) == 2
        assert segs[0].text == "Hello world."


class TestFormatDetection:
    def test_auto_detect_srt_by_extension(self, tmp_path: Path):
        p = tmp_path / "x.srt"
        p.write_text(SRT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p)
        assert len(segs) == 2

    def test_auto_detect_vtt_by_header(self, tmp_path: Path):
        p = tmp_path / "no_ext.txt"
        p.write_text(VTT_FIXTURE, encoding="utf-8")
        segs = parse_subtitle(p)
        assert segs[0].text == "Hello world."

    def test_unknown_format_raises(self, tmp_path: Path):
        p = tmp_path / "x.xyz"
        p.write_text("garbage", encoding="utf-8")
        with pytest.raises(ValueError, match="format"):
            parse_subtitle(p)


class TestSegmentModel:
    def test_segment_validation(self):
        s = Segment(start=1.0, end=2.0, text="hi")
        assert s.start == 1.0
        with pytest.raises(ValueError):
            Segment(start=3.0, end=2.0, text="hi")  # end < start
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_subtitle_normalize.py -v`
Expected: ModuleNotFoundError

---

### Task 2: Implement `subtitle/normalize.py`

**Files:**
- Create: `src/vla/subtitle/normalize.py`

**Interfaces:**
- `class FormatHint(str, Enum)`: `auto | srt | vtt | ass | json`
- `class Segment(BaseModel)`: `start: float`, `end: float`, `text: str`
- `def parse_subtitle(path: Path, *, format_hint: str | None = None) -> list[Segment]`

- [ ] **Step 1: Implement the module**

Create `src/vla/subtitle/normalize.py`:

```python
"""字幕归一化层(SSOT: spec §E Sub-1/Sub-2,2026-09-03)。

- 把 SRT/VTT/ASS 解析集中在一处(消除 SubtitleRefiner / browser_record
  中的格式耦合)
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
    """SRT: index\nHH:MM:SS,mmm --> HH:MM:SS,mmm\ntext\n\n"""
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
    """VTT: 去掉 WEBVTT 头后跟 SRT 一样的 cue 语法。"""
    body = re.sub(r"^WEBVTT.*?\n\n", "", text, count=1, flags=re.DOTALL)
    return _parse_srt(body.replace(".", ","))


def _parse_ass(text: str) -> list[Segment]:
    """ASS: 只解析 Dialogue 行,text 是最后一列(逗号分隔 9 个字段)。"""
    out: list[Segment] = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line[len("Dialogue:"):].lstrip().split(",", 9)
        if len(parts) < 10:
            continue
        start_str, end_str, _rest, text_part = parts[0], parts[1], parts[2:9], parts[9]
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
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_subtitle_normalize.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/vla/subtitle/normalize.py tests/test_subtitle_normalize.py
git commit -m "feat(subtitle): normalize module with FormatHint + Segment"
```

---

### Task 3: Migrate `SubtitleRefiner` to consume `list[Segment]`

**Files:**
- Modify: `src/vla/subtitle/refiner.py`
- Modify: `src/vla/main_provider.py` (or wherever `refiner.refine()` is called)

- [ ] **Step 1: Update `refine()` signature**

Replace `refine(subtitle_path: Path) -> ...` with:

```python
def refine(self, segments: list[Segment]) -> list[Segment]:
    """对归一化的字幕段做整理(繁简 + 错字)。输入输出都是 Segment 列表。"""
    ...
```

Remove any internal SRT/VTT parsing code; the refiner is format-agnostic now.

- [ ] **Step 2: Update pipeline caller**

In the file that calls `refiner.refine(...)`, change:

```python
# before
refined = refiner.refine(subtitle_path)

# after
segments = parse_subtitle(subtitle_path)
refined = refiner.refine(segments)
```

- [ ] **Step 3: Run refiner + pipeline tests**

Run: `uv run pytest tests/test_subtitle_refiner.py tests/test_pipeline.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/vla/subtitle/refiner.py src/vla/main_provider.py
git commit -m "refactor(refiner): consume list[Segment] from normalize"
```

---

### Task 4: Verify

- [ ] **Step 1: Full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 2: `vla doctor`**

Run: `uv run vla doctor`
Expected: All checks pass

- [ ] **Step 3: Commit any stragglers**

```bash
git status  # clean
```
