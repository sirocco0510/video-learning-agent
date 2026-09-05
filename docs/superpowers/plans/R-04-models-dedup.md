# R.4 — `models.py` Duplicate Class Definitions Removal

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the first set of `VideoTask` / `SubtitleResult` / `QualityResult` definitions in `models.py` (lines 18-52). The second set (lines 59-99) is the canonical one — has all model fields properly. The first `QualityResult` is empty (only docstring), making it a no-op definition that shadows nothing but still creates dead code.

**Architecture:** Single canonical model file with 6 BaseModel classes: `VideoTask`, `SubtitleResult`, `QualityResult`, `VideoSource`, `Correction`, `RefinementResult`.

**Tech Stack:** pydantic v2

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §A #1

## Global Constraints

- All existing tests must continue to pass after each step
- The canonical model set is defined at lines 59-149 (after first cleanup)
- Imports: keep `Path`, `BaseModel`, `Field`, `HttpUrl` once at top

---

### Task 1: Audit current `models.py`

**Files:**
- Read-only audit: `src/vla/models.py`

- [ ] **Step 1: Confirm current duplicate state**

Run: `grep -n "class VideoTask\|class SubtitleResult\|class QualityResult" src/vla/models.py`
Expected output:
```
18:class VideoTask(BaseModel):
48:class QualityResult(BaseModel):
59:class VideoTask(BaseModel):
75:class SubtitleResult(BaseModel):
89:class QualityResult(BaseModel):
```

(Three classes with two definitions each — the first `QualityResult` has no fields.)

- [ ] **Step 2: Identify consumers callers**

Run: `grep -rln "from vla.models import" src/vla/ tests/`
Expected: many files importing VideoTask, SubtitleResult, QualityResult, VideoSource, Correction, RefinementResult

---

### Task 2: Remove first set of class definitions

**Files:**
- Modify: `src/vla/models.py:11-53`

**Interfaces:** (no change to public API)

- [ ] **Step 1: Verify the second `QualityResult` has all required fields**

Open `src/vla/models.py` and confirm lines 89-99 (the second `QualityResult`) have fields:
- `passed: bool`
- `score: int`
- `issues: list[str]`
- `suggestion: str`
- `char_count: int`

If yes, proceed. If any field is missing in the second definition, STOP and ask the user — that means the first definition was the canonical one with fields.

- [ ] **Step 2: Delete the first definitions**

In `src/vla/models.py`, delete lines 11-53:
- Line 11: `from __future__ import annotations`
- Line 13: `from pathlib import Path`
- Lines 18-52: first `VideoTask`, `SubtitleResult`, `QualityResult`

The result should leave the file starting with `from pathlib import Path` (the second import at line 54) and the second set of class definitions.

- [ ] **Step 3: Verify no syntax error**

Run: `uv run python -c "from vla.models import VideoTask, SubtitleResult, QualityResult, VideoSource, Correction, RefinementResult; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Verify the field on `QualityResult`**

Run: `uv run python -c "from vla.models import QualityResult; print(QualityResult.model_fields.keys())"`
Expected: dict_keys(['passed', 'score', 'issues', 'suggestion', 'char_count'])

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest -v`
Expected: All pass (35+ green)

- [ ] **Step 6: Commit**

```bash
git add src/vla/models.py
git commit -m "fix(models): remove duplicate class definitions (Python shadow bug)"
```

---

### Task 3: Verify no orphan imports

- [ ] **Step 1: Confirm no second import remains**

Run: `grep -n "from pathlib\|^from pydantic" src/vla/models.py`
Expected: exactly one `from pathlib import Path` and one `from pydantic import BaseModel, Field, HttpUrl`

- [ ] **Step 2: Lint check**

Run: `uv run python -m py_compile src/vla/models.py`
Expected: silent (no output = no syntax error)