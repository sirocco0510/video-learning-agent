# R.6 — `log/transcribed_file.py` Module

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the transcribed-file write/read pair into a standalone module so `TranscriptionLog.save_transcribed` and `LLMSummarizer._parse_file` no longer rely on regex-coupled formats.

**Architecture:** `log/transcribed_file.py` provides:
- `TranscribedItem` dataclass (canonical fields)
- `write(path: Path, item: TranscribedItem) -> Path`
- `read(path: Path) -> TranscribedItem`

The on-disk format stays compatible with what `_parse_file` currently expects:
```
# <title>
来源:<source> | 质量:<score>/100 | 时长:<sec>s

<text>
```

**Tech Stack:** Python 3.12, `dataclasses`, stdlib

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §B #9

## Global Constraints

- On-disk format MUST stay compatible with current regex assumptions
- `LLMSummarizer.TranscribedItem` may either be re-exported from the new module or kept in summarizer (delegating to new module) — pick delegation to avoid circular import

---

### Task 1: Create `log/transcribed_file.py`

**Files:**
- Create: `src/vla/log/transcribed_file.py`
- Create: `tests/test_transcribed_file.py`

**Interfaces:**
- Produces: `class TranscribedItem` with fields: `title, source, quality_score, duration_sec, text, path, mtime`
- Produces: `def write(path: Path, item: TranscribedItem) -> Path`
- Produces: `def read(path: Path) -> TranscribedItem`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transcribed_file.py
from pathlib import Path
from vla.log.transcribed_file import TranscribedItem, write, read


class TestRoundTrip:
    def test_basic_roundtrip(self, tmp_path: Path):
        item = TranscribedItem(
            title="Python 装饰器",
            source="whisper",
            quality_score=85,
            duration_sec=1800,
            text="这是正文内容。",
            path=tmp_path / "x.txt",
            mtime=0.0,
        )
        path = write(tmp_path / "x.txt", item)
        loaded = read(path)
        assert loaded.title == "Python 装饰器"
        assert loaded.source == "whisper"
        assert loaded.quality_score == 85
        assert loaded.duration_sec == 1800
        assert loaded.text == "这是正文内容。"

    def test_unicode_in_text(self, tmp_path: Path):
        item = TranscribedItem(
            title="T", source="api", quality_score=90,
            duration_sec=120, text="深度学习\n\n神经网络原理", path=tmp_path / "t.txt", mtime=0.0,
        )
        path = write(tmp_path / "t.txt", item)
        assert "深度学习" in read(path).text
        assert "\n\n" in read(path).text  # paragraph separator preserved

    def test_zero_duration(self, tmp_path: Path):
        item = TranscribedItem(
            title="T", source="browser", quality_score=50,
            duration_sec=0, text="hi", path=tmp_path / "t.txt", mtime=0.0,
        )
        path = write(tmp_path / "t.txt", item)
        assert read(path).duration_sec == 0


class TestReadEdgeCases:
    def test_minimal_file(self, tmp_path: Path):
        """只有标题 + 正文也能读(元数据缺失时用默认值)。"""
        path = tmp_path / "min.txt"
        path.write_text("# Minimal\n\nBody text", encoding="utf-8")
        loaded = read(path)
        assert loaded.title == "Minimal"
        assert loaded.text == "Body text"
        assert loaded.source == "whisper"  # default
        assert loaded.quality_score == 0  # default
        assert loaded.duration_sec == 0  # default

    def test_no_blank_separator_falls_back_to_strip(self, tmp_path: Path):
        path = tmp_path / "weird.txt"
        path.write_text("# T\nsource:whisper\nall in one", encoding="utf-8")
        loaded = read(path)
        # 没有 \n\n 分隔,read 把整段除标题外当 text
        assert loaded.title == "T"


class TestWriteCreatesDirectory:
    def test_creates_parent_dir(self, tmp_path: Path):
        path = tmp_path / "nested" / "x.txt"
        item = TranscribedItem(
            title="T", source="api", quality_score=80,
            duration_sec=10, text="hi", path=path, mtime=0.0,
        )
        result = write(path, item)
        assert result.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transcribed_file.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: Write the implementation**

Create `src/vla/log/transcribed_file.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transcribed_file.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/log/transcribed_file.py tests/test_transcribed_file.py
git commit -m "feat(log): transcribed_file write/read with TranscribedItem"
```

---

### Task 2: Wire `TranscriptionLog.save_transcribed` to `transcribed_file.write`

**Files:**
- Modify: `src/vla/log/transcription_log.py:116-143`

**Interfaces:**
- Consumes: `from vla.log.transcribed_file import TranscribedItem, write as write_transcribed`

- [ ] **Step 1: Update save_transcribed**

In `transcription_log.py`, replace `save_transcribed` body to delegate:

```python
def save_transcribed(
    self,
    video_id: str,
    title: str,
    text: str,
    quality: QualityResult,
    source: str,
    duration_sec: int,
) -> Path:
    """委托给 transcribed_file.write,保留 FR-7.7 落盘行为。"""
    from vla.log.transcribed_file import TranscribedItem, write as write_transcribed
    safe = _safe_title(title)
    path = self.transcribed_dir / f"{video_id}_{safe}.txt"
    item = TranscribedItem(
        title=title,
        source=source,
        quality_score=quality.score,
        duration_sec=duration_sec,
        text=text,
        path=path,
        mtime=0.0,
    )
    return write_transcribed(path, item)
```

- [ ] **Step 2: Run all transcription_log tests**

Run: `uv run pytest tests/test_transcription_log.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/vla/log/transcription_log.py
git commit -m "refactor(log): save_transcribed delegates to transcribed_file"
```

---

### Task 3: Wire `LLMSummarizer._parse_file` to `transcribed_file.read`

**Files:**
- Modify: `src/vla/summary/llm_summarizer.py:114-152`

**Interfaces:**
- Consumes: `from vla.log.transcribed_file import TranscribedItem, read as read_transcribed`

- [ ] **Step 1: Update `_parse_file`**

Replace the method body:

```python
def _parse_file(self, path: Path) -> TranscribedItem:
    """委托 transcribed_file.read — 单一来源。"""
    from vla.log.transcribed_file import read as read_transcribed
    return read_transcribed(path)
```

- [ ] **Step 2: Update local `TranscribedItem` definition (if any)**

The summarizer file may also have its own `TranscribedItem` dataclass. If yes:
- Replace with `from vla.log.transcribed_file import TranscribedItem`

If no, just add the import.

- [ ] **Step 3: Run summarizer tests**

Run: `uv run pytest tests/test_llm_summarizer.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/vla/summary/llm_summarizer.py
git commit -m "refactor(summarizer): _parse_file delegates to transcribed_file"
```

---

### Task 4: Verify

- [ ] **Step 1: Full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 2: Grep no stale regex**

Run: `grep -rn "_HEADER_TITLE_RE\|_META_TOKEN_RE" src/vla/`
Expected: only `src/vla/log/transcribed_file.py`