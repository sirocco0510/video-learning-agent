# R.9 — Delete `_pause_page_video` Compat Wrapper

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `_pause_page_video` backward-compat wrapper in `subtitle/browser_record.py`. All call sites use `page_control.pause_page_video` directly.

**Architecture:** Pure deletion. 3 callers already use `pause_page_video` from `page_control.py`; the wrapper was kept for backward compat only.

**Tech Stack:** N/A

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §B #10

## Global Constraints

- `subtitle/page_control.py::pause_page_video` stays unchanged
- All 3 call sites already import correctly; verify after deletion

---

### Task 1: Audit current state

**Files:**
- Audit: `src/vla/subtitle/`

- [ ] **Step 1: Find wrapper definition**

Run: `grep -rn "_pause_page_video" src/vla/`
Expected output:
- `src/vla/subtitle/browser_record.py` — wrapper definition + caller(s)

- [ ] **Step 2: Find all `pause_page_video` callers**

Run: `grep -rn "pause_page_video" src/vla/`
Expected: 3 callers across `strategy.py`, `bilibili_adapter.py`, `browser_record.py` (some directly, some via wrapper)

---

### Task 2: Delete the wrapper

**Files:**
- Modify: `src/vla/subtitle/browser_record.py`

**Interfaces:** (no change)

- [ ] **Step 1: Locate the wrapper**

In `browser_record.py`, find the wrapper definition (around line 339 per agent report). It looks like:

```python
def _pause_page_video(page):
    """Backward compat wrapper. Use page_control.pause_page_video directly."""
    from vla.subtitle.page_control import pause_page_video
    return pause_page_video(page)
```

- [ ] **Step 2: Delete the wrapper**

Delete the entire wrapper function (definition + docstring + body).

- [ ] **Step 3: Replace any internal callers within `browser_record.py`**

If `browser_record.py` itself calls `_pause_page_video(page)` internally, change those calls to:

```python
from vla.subtitle.page_control import pause_page_video
pause_page_video(page)
```

(Use the existing import if already present; otherwise add.)

- [ ] **Step 4: Run browser_record tests**

Run: `uv run pytest tests/test_browser_record.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/vla/subtitle/browser_record.py
git commit -m "refactor(browser_record): delete _pause_page_video compat wrapper"
```

---

### Task 3: Verify

- [ ] **Step 1: Final grep**

Run: `grep -rn "_pause_page_video" src/vla/ tests/`
Expected: no output

- [ ] **Step 2: Full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 3: `vla doctor`**

Run: `uv run vla doctor`
Expected: All checks pass