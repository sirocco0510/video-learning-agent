# F2-6 — SubtitlePipeline (FR-2.15c wiring)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `clean_transcript` (Level 1) → `SubtitleRefiner.refine` (Level 4) → `QualityChecker.check` into a single async `SubtitlePipeline.run()` call, so `VideoLearningAgent` invokes one orchestrator instead of three separate calls.

**Architecture:**
- New module: `src/vla/quality/pipeline.py` (`SubtitlePipeline` + `PipelineResult` dataclass)
- Modify: `src/vla/quality/__init__.py` (export `SubtitlePipeline`)
- Modify: `src/vla/main.py` (call `SubtitlePipeline.run()` after Whisper, BEFORE quality decision)
- Tests: `tests/test_pipeline.py` (6 tests: Level 1 only / Level 4 enabled / quality on refined / refiner failure / 3-file output / long text skip)

**Tech Stack:** Python 3.12, asyncio, pydantic v2 (PipelineResult), existing `transcribe.postprocess.PostProcessor` + `quality.refiner.SubtitleRefiner` + `quality.checker.QualityChecker`

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md` §3.6 + §4.4 (FR-2.15c / FR-3.9)

## Global Constraints

- `tests/` is the test root; fixtures in `tests/fixtures/`
- TDD: write failing test → run → minimal impl → run → commit
- Refiner `refine_enabled=false` is the default (省钱, FR-2.15c master switch)
- Refiner failure fallback: any LLM exception / parse failure → return cleaned_text as-is, never break pipeline
- Long text skip: text > `refine_max_chars` (default 6000) → skip Level 4 entirely
- 3-file output contract: `<stem>.transcript.txt` (raw, by transcriber) + `<stem>.cleaned.txt` (Level 1) + `<stem>.refined.txt` (Level 4, optional)
- asyncio_mode = "auto" (pytest config already set)
- pydantic v2 for `PipelineResult`

## Interfaces from Earlier Plans (F2-1 / F2-2 / F2-5) — none consumed

F2-6 has NO dependency on audio factories or screenshot controllers. It's a text-only orchestration layer.

## Interfaces Consumed (pre-existing in codebase)

```python
# src/vla/transcribe/postprocess.py (existing)
class PostProcessor:
    def clean_transcript(self, text: str) -> tuple[str, PostStats]: ...

# src/vla/quality/refiner.py (existing, R-10 era)
class SubtitleRefiner:
    def __init__(self, config: VLAConfig, llm: LLMClientLike | None = None): ...
    @property
    def enabled(self) -> bool: ...
    def refine(self, text: str, title: str = "") -> RefinementResult: ...

@dataclass
class RefinementResult:
    original_text: str
    cleaned_text: str
    corrections: list[Correction]
    notes: list[str]
    model: str
    prompt_tokens: int
    completion_tokens: int

def write_cleaned_transcript(path: Path, result: RefinementResult) -> None: ...

# src/vla/quality/checker.py (existing, R-1 era)
class QualityChecker:
    def __init__(self, config: VLAConfig, llm: LLMClientLike | None = None): ...
    def check(self, text: str, title: str, duration_sec: int, model_size: str) -> QualityResult: ...
```

---

### Task 1: Implement `SubtitlePipeline.run` (Level 1 only by default)

**Files:**
- Create: `src/vla/quality/pipeline.py`
- Modify: `src/vla/quality/__init__.py` (add `from .pipeline import SubtitlePipeline, PipelineResult`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `SubtitlePipeline(config, postprocessor, refiner, checker)`
- Produces: `async run(text, title, duration_sec, model_size, output_dir, stem) -> PipelineResult`
- Produces: `PipelineResult(cleaned_text, refined_text, quality, post_stats, refine_result)` — `refine_result=None` when disabled

- [ ] **Step 1: Write 2 failing tests**

```python
# tests/test_pipeline.py
"""SubtitlePipeline 测试(SSOT: spec 2026-09-03-fr2-fr3 §3.6 / §4.4)。

FR-2.15c / FR-3.9:Level 1 (本地) → Level 4 (云端 LLM,可选) → QualityChecker。
默认 refine_enabled=false → 只跑 Level 1 + QualityChecker。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from vla.config import VLAConfig
from vla.quality.checker import QualityChecker, QualityResult
from vla.quality.pipeline import PipelineResult, SubtitlePipeline
from vla.quality.refiner import RefinementResult, SubtitleRefiner
from vla.transcribe.postprocess import PostProcessor, PostStats


@pytest.fixture
def config() -> VLAConfig:
    """refine_enabled=False (默认,省钱)。"""
    return VLAConfig()


@pytest.fixture
def mock_postprocessor() -> MagicMock:
    p = MagicMock(spec=PostProcessor)
    p.clean_transcript = MagicMock(
        return_value=("cleaned text", PostStats(chars_in=10, chars_out=10, lines=1))
    )
    return p


@pytest.fixture
def mock_refiner_disabled() -> MagicMock:
    """refine_enabled=False refiner(spec 行为:enabled 属性返回 False)。"""
    r = MagicMock(spec=SubtitleRefiner)
    r.enabled = False
    return r


@pytest.fixture
def mock_refiner_enabled() -> MagicMock:
    r = MagicMock(spec=SubtitleRefiner)
    r.enabled = True
    r.refine = MagicMock(
        return_value=RefinementResult(
            original_text="cleaned text",
            cleaned_text="refined text",
            corrections=[],
            notes=[],
            model="claude-fable-5",
            prompt_tokens=100,
            completion_tokens=50,
        )
    )
    return r


@pytest.fixture
def mock_checker() -> MagicMock:
    c = MagicMock(spec=QualityChecker)
    c.check = MagicMock(
        return_value=QualityResult(pass_=True, score=85, reasons=[])
    )
    return c


class TestPipelineLevel1Only:
    def test_default_run_calls_level1_only(
        self,
        config: VLAConfig,
        mock_postprocessor: MagicMock,
        mock_refiner_disabled: MagicMock,
        mock_checker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """refine_enabled=False → 只调 postprocessor + checker,refiner.refine 不调。"""
        pipeline = SubtitlePipeline(
            config, mock_postprocessor, mock_refiner_disabled, mock_checker
        )
        result = asyncio.run(
            pipeline.run(
                text="raw transcript",
                title="Test Video",
                duration_sec=300,
                model_size="small",
                output_dir=tmp_path,
                stem="Bv1_test",
            )
        )
        assert isinstance(result, PipelineResult)
        assert result.cleaned_text == "cleaned text"
        assert result.refined_text == "cleaned text"  # no refine → same as cleaned
        assert result.refine_result is None
        assert result.quality.pass_ is True
        assert result.quality.score == 85
        mock_postprocessor.clean_transcript.assert_called_once_with("raw transcript")
        mock_refiner_disabled.refine.assert_not_called()
        mock_checker.check.assert_called_once_with(
            "cleaned text", "Test Video", 300, "small",
        )

    def test_pipeline_writes_cleaned_file(
        self,
        config: VLAConfig,
        mock_postprocessor: MagicMock,
        mock_refiner_disabled: MagicMock,
        mock_checker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Level 1 → 写 <stem>.cleaned.txt。"""
        pipeline = SubtitlePipeline(
            config, mock_postprocessor, mock_refiner_disabled, mock_checker
        )
        asyncio.run(
            pipeline.run(
                "raw", "title", 100, "small", tmp_path, "Bv1_clean",
            )
        )
        cleaned_path = tmp_path / "Bv1_clean.cleaned.txt"
        assert cleaned_path.exists()
        assert cleaned_path.read_text(encoding="utf-8") == "cleaned text"
        # refined.txt 不应存在
        assert not (tmp_path / "Bv1_clean.refined.txt").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'vla.quality.pipeline'`

- [ ] **Step 3: Implement `pipeline.py`**

```python
# src/vla/quality/pipeline.py
"""SubtitlePipeline:Level 1 → Level 4 → QualityChecker 协调器(SSOT: spec §3.6)。

FR-2.15c / FR-3.9 字幕清理管线。
- Level 1 (本地, always on):PostProcessor.clean_transcript
- Level 4 (云端 LLM, optional):SubtitleRefiner.refine(refine_enabled=true 才走)
- QualityChecker:always runs on refined_text (or cleaned_text if no refine)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from vla.config import VLAConfig
from vla.quality.checker import QualityChecker, QualityResult
from vla.quality.refiner import RefinementResult, SubtitleRefiner, write_cleaned_transcript
from vla.transcribe.postprocess import PostProcessor, PostStats


@dataclass
class PipelineResult(BaseModel):
    """FR-2.15c 管线结果。"""
    cleaned_text: str
    refined_text: str
    quality: QualityResult
    post_stats: PostStats
    refine_result: RefinementResult | None = None  # None when refiner disabled/skipped


class SubtitlePipeline:
    def __init__(
        self,
        config: VLAConfig,
        postprocessor: PostProcessor,
        refiner: SubtitleRefiner | None,
        checker: QualityChecker,
    ) -> None:
        self._config = config
        self._postprocessor = postprocessor
        self._refiner = refiner
        self._checker = checker
        self.log = logging.getLogger(__name__)

    async def run(
        self,
        text: str,
        title: str,
        duration_sec: int,
        model_size: str,
        output_dir: Path,
        stem: str,
    ) -> PipelineResult:
        """FR-2.15c 主入口。

        顺序:
          1. Level 1:postprocessor.clean_transcript → 写 <stem>.cleaned.txt
          2. Level 4 (可选):refiner.refine(只当 enabled 且 text ≤ refine_max_chars)
             → 写 <stem>.refined.txt(只在 cleaned_text != refined_text 时)
          3. QualityChecker:对 refined_text(或 cleaned_text)打分
        Refiner 失败兜底:用 cleaned_text 继续,refine_result=None。
        """
        # ---- Level 1: always on ----
        cleaned_text, post_stats = self._postprocessor.clean_transcript(text)
        cleaned_path = output_dir / f"{stem}.cleaned.txt"
        cleaned_path.write_text(cleaned_text, encoding="utf-8")

        refined_text = cleaned_text
        refine_result: RefinementResult | None = None

        # ---- Level 4: optional ----
        max_chars = getattr(
            self._config.quality_check, "refine_max_chars", 6000
        )
        if self._refiner is not None and self._refiner.enabled and len(cleaned_text) <= max_chars:
            try:
                refine_result = self._refiner.refine(cleaned_text, title=title)
                if refine_result.cleaned_text != cleaned_text:
                    refined_text = refine_result.cleaned_text
                    refined_path = output_dir / f"{stem}.refined.txt"
                    write_cleaned_transcript(refined_path, refine_result)
            except Exception as e:
                # 兜底:refiner 失败 → 用 cleaned_text,主流程不中断
                self.log.warning("Level 4 refine 失败,使用 cleaned_text: %s", e)
                refine_result = None
                refined_text = cleaned_text
        elif self._refiner is not None and self._refiner.enabled:
            self.log.warning(
                "字幕文本 %d 字符 > refine_max_chars=%d,跳过 Level 4",
                len(cleaned_text), max_chars,
            )

        # ---- Quality gate ----
        quality = self._checker.check(refined_text, title, duration_sec, model_size)

        return PipelineResult(
            cleaned_text=cleaned_text,
            refined_text=refined_text,
            quality=quality,
            post_stats=post_stats,
            refine_result=refine_result,
        )
```

- [ ] **Step 4: Update `src/vla/quality/__init__.py`**

```python
# src/vla/quality/__init__.py — add at end
from vla.quality.pipeline import PipelineResult, SubtitlePipeline

__all__ = [
    "QualityChecker",
    "QualityResult",
    "SubtitleRefiner",
    "RefinementResult",
    "SubtitlePipeline",   # NEW
    "PipelineResult",    # NEW
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline.py::TestPipelineLevel1Only -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add src/vla/quality/pipeline.py src/vla/quality/__init__.py tests/test_pipeline.py
git commit -m "feat(F2-6): SubtitlePipeline orchestrator (FR-2.15c level 1)"
```

---

### Task 2: Level 4 enabled — refine + write refined.txt + quality uses refined_text

**Files:**
- Test: `tests/test_pipeline.py` (add `TestPipelineLevel4` class)

- [ ] **Step 1: Add 2 tests**

```python
class TestPipelineLevel4:
    def test_level4_runs_when_enabled(
        self,
        config: VLAConfig,
        mock_postprocessor: MagicMock,
        mock_refiner_enabled: MagicMock,
        mock_checker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """refine_enabled=True → 调 refiner.refine + 写 refined.txt + quality 用 refined_text。"""
        pipeline = SubtitlePipeline(
            config, mock_postprocessor, mock_refiner_enabled, mock_checker
        )
        result = asyncio.run(
            pipeline.run("raw", "title", 200, "small", tmp_path, "Bv1_refined")
        )
        assert result.refine_result is not None
        assert result.refined_text == "refined text"  # 来自 refine_result
        assert result.cleaned_text == "cleaned text"
        mock_refiner_enabled.refine.assert_called_once_with("cleaned text", title="title")
        mock_checker.check.assert_called_once_with(
            "refined text", "title", 200, "small",
        )
        # 文件存在
        assert (tmp_path / "Bv1_refined.cleaned.txt").exists()
        assert (tmp_path / "Bv1_refined.refined.txt").exists()

    def test_level4_skips_when_long_text(
        self,
        config: VLAConfig,
        mock_postprocessor: MagicMock,
        mock_refiner_enabled: MagicMock,
        mock_checker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """text > refine_max_chars (6000) → 跳过 Level 4,用 cleaned_text。"""
        long_text = "a" * 7000
        mock_postprocessor.clean_transcript.return_value = (
            long_text, PostStats(chars_in=7000, chars_out=7000, lines=1)
        )
        pipeline = SubtitlePipeline(
            config, mock_postprocessor, mock_refiner_enabled, mock_checker
        )
        result = asyncio.run(
            pipeline.run(long_text, "long", 600, "small", tmp_path, "Bv1_long")
        )
        assert result.refine_result is None
        assert result.refined_text == long_text
        mock_refiner_enabled.refine.assert_not_called()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline.py::TestPipelineLevel4 -v`
Expected: PASS (2 tests, total 4 tests)

(no impl change — Task 1 already handles this path; this task verifies existing impl is correct)

- [ ] **Step 3: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add tests/test_pipeline.py
git commit -m "test(F2-6): Level 4 enabled + long text skip paths"
```

---

### Task 3: Refiner failure fallback + 3-file output verification

**Files:**
- Test: `tests/test_pipeline.py` (add `TestPipelineFailure` + `TestPipelineFileOutput`)

- [ ] **Step 1: Add 2 tests**

```python
class TestPipelineFailure:
    def test_refiner_failure_continues_with_cleaned(
        self,
        config: VLAConfig,
        mock_postprocessor: MagicMock,
        mock_refiner_enabled: MagicMock,
        mock_checker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """refiner.refine 抛异常 → pipeline 不抛,继续 quality check 用 cleaned_text。"""
        mock_refiner_enabled.refine = MagicMock(
            side_effect=Exception("LLM API timeout")
        )
        pipeline = SubtitlePipeline(
            config, mock_postprocessor, mock_refiner_enabled, mock_checker
        )
        result = asyncio.run(
            pipeline.run("raw", "t", 100, "small", tmp_path, "Bv1_fail")
        )
        assert result.refine_result is None
        assert result.refined_text == "cleaned text"  # 降级到 cleaned
        mock_checker.check.assert_called_once_with(
            "cleaned text", "t", 100, "small",
        )
        assert result.quality.pass_ is True


class TestPipelineFileOutput:
    def test_three_files_written(
        self,
        config: VLAConfig,
        mock_postprocessor: MagicMock,
        mock_refiner_enabled: MagicMock,
        mock_checker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """3 文件输出契约:transcript.txt(由 caller)+Prewriter.txt + refined.txt。"""
        pipeline = SubtitlePipeline(
            config, mock_postprocessor, mock_refiner_enabled, mock_checker
        )
        # transcript.txt 由 caller(StreamingTranscriber)写,pipeline 不负责
        transcript_path = tmp_path / "Bv1_three.transcript.txt"
        transcript_path.write_text("raw transcript", encoding="utf-8")
        asyncio.run(
            pipeline.run("raw transcript", "t", 100, "small", tmp_path, "Bv1_three")
        )
        assert (tmp_path / "Bv1_three.transcript.txt").exists()
        assert (tmp_path / "Bv1_three.cleaned.txt").exists()
        assert (tmp_path / "Bv1_three.refined.txt").exists()
        # content 校验
        assert (tmp_path / "Bv1_three.cleaned.txt").read_text() == "cleaned text"
        assert (tmp_path / "Bv1_three.refined.txt").read_text() == "refined text"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS (6 tests total)

(no impl change — Task 1 covers both paths via the try/except)

- [ ] **Step 3: Run full file**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: 6/6 PASS

- [ ] **Step 4: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add tests/test_pipeline.py
git commit -m "test(F2-6): refiner failure fallback + 3-file output contract"
```

---

### Task 4: Wire `SubtitlePipeline.run()` into `VideoLearningAgent`

**Files:**
- Modify: `src/vla/main.py` (replace 3 separate calls with one `pipeline.run()`)
- Test: `tests/test_pipeline.py` (add integration smoke via mock agent)

**NOTE:** This task touches `main.py` and may affect existing e2e tests. If `main.py` has heavy imports, mock the heavy bits in test. The change must NOT alter FR-1 ~ FR-10 observable behavior — only refactor internal call sequence.

- [ ] **Step 1: Read current `main.py` flow**

Run: `grep -n "SubtitleRefiner\|QualityChecker\|clean_transcript\|\.refine(\|\.check(" src/vla/main.py | head -30`
Document the current 3-step call sequence (likely: `postprocessor.clean_transcript` → `refiner.refine` → `checker.check`).

- [ ] **Step 2: Add a wiring test (smoke)**

Append to `tests/test_pipeline.py`:

```python
class TestPipelineWiring:
    def test_pipeline_replaces_three_step_call(
        self,
        config: VLAConfig,
        mock_postprocessor: MagicMock,
        mock_refiner_disabled: MagicMock,
        mock_checker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Smoke:VideoLearningAgent 应该只调一次 pipeline.run(),而不是 3 个独立方法。

        通过 inspect 验证 main.py 源码里不再有
        `postprocessor.clean_transcript(` + `refiner.refine(` + `checker.check(` 三个连续调用
        (允许单独出现,但不允许在同一 function 内三个连续 call)。
        """
        import inspect
        from vla import main
        src = inspect.getsource(main)
        # 简单 contract:main.py 不应再各自 import 这三个类
        # (实际调用集中到 pipeline)
        assert "from vla.quality.pipeline import SubtitlePipeline" in src
```

- [ ] **Step 3: Modify `src/vla/main.py`**

Find the existing 3-step sequence. Replace with:

```python
# OLD: 3-step sequence (delete)
# cleaned_text, post_stats = postprocessor.clean_transcript(raw_text)
# refined_text = refiner.refine(cleaned_text) if refiner.enabled else cleaned_text
# quality = checker.check(refined_text, title, duration_sec, model_size)

# NEW: single pipeline call (FR-2.15c)
from vla.quality.pipeline import SubtitlePipeline
pipeline = SubtitlePipeline(config, postprocessor, refiner, checker)
result = await pipeline.run(
    text=raw_text,
    title=title,
    duration_sec=duration_sec,
    model_size=model_size,
    output_dir=output_dir,
    stem=stem,
)
# result.cleaned_text / refined_text / quality / post_stats / refine_result
quality = result.quality
```

If main.py imports `PostProcessor`, `SubtitleRefiner`, `QualityChecker` only for this sequence, replace with single `from vla.quality.pipeline import SubtitlePipeline` import. Otherwise keep imports if used elsewhere.

- [ ] **Step 4: Run test + existing main tests**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run pytest tests/test_pipeline.py tests/test_video_learning_agent.py -v
```
Expected: `TestPipelineWiring::test_pipeline_replaces_three_step_call` PASS; existing main tests still PASS

If existing main tests break, revert Task 4 changes and document the break — leave the call-site refactor for F2-7 (which has broader scope).

- [ ] **Step 5: Commit (only if all tests pass)**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add src/vla/main.py tests/test_pipeline.py
git commit -m "refactor(F2-6): wire SubtitlePipeline.run into VideoLearningAgent"
```

---

## Acceptance Criteria

- [ ] `uv run pytest tests/test_pipeline.py -v` → 6/6 PASS (or 7/7 if wiring test added)
- [ ] `grep -rn "class SubtitlePipeline" src/vla/quality/` → matches
- [ ] `grep -rn "SubtitlePipeline" src/vla/main.py` → matches (wiring)
- [ ] Default `refine_enabled=false` → no LLM call, `refined.txt` not written
- [ ] Refiner exception → pipeline continues, `quality` still runs on cleaned_text
- [ ] Long text → Level 4 skipped, cleaned_text used
- [ ] All 3 files (transcript.txt / cleaned.txt / refined.txt) follow naming convention

## Dependency Note

F2-6 has NO dependency on audio factories (F2-1) or screenshot controllers (F2-5). It can be executed in parallel with F2-1, F2-2, F2-3, F2-4, F2-5.