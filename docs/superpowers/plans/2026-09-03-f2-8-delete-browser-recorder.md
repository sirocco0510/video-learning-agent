# F2-8 — Delete `BrowserRecorder` (FR-2.14 cleanup)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete `subtitle/browser_record.py` + `tests/test_browser_record.py` + `subtitle/probes/` (if sole consumer) + remove all references. Verify no regression in 491 baseline tests + new F2-* tests.

**Architecture:**
- Delete: `src/vla/subtitle/browser_record.py` (entire file, ~700 lines including `BrowserRecorder` + `_SafeClosePage` + `_pause_page_video`)
- Delete: `tests/test_browser_record.py` (entire file, 50 tests — replaced by F2-1/F2-2/F2-5 tests)
- Audit: `src/vla/subtitle/probes/` — verify if sole consumer was `BrowserRecorder`
- Modify: `src/vla/main.py` (remove `VideoSourceRecordConfig` references if any remain)
- Modify: `src/vla/config.py` (remove `record` block if any)
- Modify: any caller still references `BrowserRecorder`/`browser_record` — delete the import

**Tech Stack:** git rm + grep audit + pytest regression

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md` §1.1 + §6 Q2 (resolved: delete entirely)

**User rulings locked:** Q1=Polling / Q7=Silent / Q8=Warn / Q3+Q9=Keep generic PluginStatus (Q2 already resolved before this plan)

## Global Constraints

- `tests/` is the test root; fixtures in `tests/fixtures/`
- All F2-* plans before F2-8 must be merged(F2-1 ~ F2-7)
- `git rm` 用于删除 tracked files;`rm` 用于 untracked
- Grep audit 必须返回 0 匹配才验收
- 不能修改 `implementation-plan.md`(frozen 历史快照)
- 不改 FR-1 ~ FR-10 可观测行为(Q2 已决定:no wrapper)
- 失败处理:任何 caller 漏改 → 立即补 import 删除,跑测试直到绿

---

### Task 1: Audit all references to `BrowserRecorder` / `browser_record` / `BrowserRecordConfig`

**Files:**
- Audit only (no code change)

- [ ] **Step 1: Run grep audit commands**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent

# All Python references
echo "=== class BrowserRecorder ==="
grep -rn "class BrowserRecorder" src/ tests/

echo "=== from vla.subtitle.browser_record ==="
grep -rn "from vla.subtitle.browser_record\|vla.subtitle import browser_record" src/ tests/

echo "=== VideoSourceRecordConfig ==="
grep -rn "VideoSourceRecordConfig\|video_source.record" src/ tests/

echo "=== BrowserRecordConfig ==="
grep -rn "BrowserRecordConfig" src/ tests/

echo "=== _pause_page_video ==="
grep -rn "_pause_page_video" src/ tests/

echo "=== probes/ contents ==="
ls -la src/vla/subtitle/probes/

echo "=== browser_record imports in probes ==="
grep -rn "browser_record" src/vla/subtitle/probes/
```

Document all hits in a `TASKS_AUDIT.md` for reference during cleanup.

- [ ] **Step 2: Categorize each hit**

For each hit, decide:
- **DELETE**: import that's no longer needed → remove
- **MIGRATE**: code that should use F2-1/F2-2/F2-5 instead → update
- **KEEP**: legitimate reference (none expected after F2-7)

- [ ] **Step 3: Commit audit doc (optional)**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add docs/superpowers/plans/2026-09-03-f2-8-delete-browser-recorder.md
# audit doc appended inline if needed
git commit -m "docs(F2-8): BrowserRecorder audit" --allow-empty
```

---

### Task 2: Delete `browser_record.py` + `test_browser_record.py`

**Files:**
- Delete: `src/vla/subtitle/browser_record.py`
- Delete: `tests/test_browser_record.py`

- [ ] **Step 1: Pre-delete test count**

Run: `uv run pytest tests/test_browser_record.py -v 2>&1 | tail -3`
Document existing test count (expected ~50 tests).

- [ ] **Step 2: Delete both files**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git rm src/vla/subtitle/browser_record.py
git rm tests/test_browser_record.py
```

- [ ] **Step 3: Try import — expect failure**

Run: `uv run python -c "from vla.subtitle.browser_record import BrowserRecorder"`
Expected: `ModuleNotFoundError: No module named 'vla.subtitle.browser_record'`
(If a module imports browser_record, the import will fail loudly here — fix in Task 3.)

- [ ] **Step 4: Run full test suite to find broken imports**

Run: `uv run pytest tests/ -v --tb=line 2>&1 | tail -40`
Expected: some test files fail with `ImportError`. Document each broken import for Task 3 fix.

- [ ] **Step 5: Commit deletion**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git commit -m "refactor(F2-8): delete browser_record.py + test_browser_record.py"
```

---

### Task 3: Fix all remaining `BrowserRecorder` / `browser_record` imports

**Files:**
- Modify: any file importing from `browser_record` (identified in Task 2 Step 4)

- [ ] **Step 1: For each broken import, delete the import line**

Common pattern to remove:

```python
# DELETE these lines from callers
from vla.subtitle.browser_record import BrowserRecorder, _SafeClosePage, _pause_page_video
```

If the caller actually used `BrowserRecorder` for something not yet replaced by F2-1/F2-2/F2-5, STOP and report — do not silently break.

- [ ] **Step 2: For `_pause_page_video` callers (per R-9 of refactor-consolidation)**

R-9 already wrapped `_safe_close_page` (callers own page lifecycle). Verify:

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
grep -rn "_pause_page_video\|pause_page_video" src/
```

If only `page_control.pause_page_video` remains (R-9 result), no action. If `_pause_page_video` references exist, replace with `from vla.subtitle.page_control import pause_page_video`.

- [ ] **Step 3: For `VideoSourceRecordConfig` references**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
grep -rn "VideoSourceRecordConfig\|video_source.record\|cfg.video_source.record" src/
```

If R-8 of refactor-consolidation removed this (it should), no action. If hits remain, remove from config.py + main.py.

- [ ] **Step 4: Re-run full test suite**

Run: `uv run pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: All F2-1 ~ F2-7 tests PASS + 491 baseline tests PASS + new pipeline tests PASS

Iterate until green.

- [ ] **Step 5: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add -A
git commit -m "refactor(F2-8): remove all BrowserRecorder/browser_record imports"
```

---

### Task 4: Audit `subtitle/probes/` and delete if sole consumer was BrowserRecorder

**Files:**
- Audit + possible delete: `src/vla/subtitle/probes/`

- [ ] **Step 1: List probes/ contents**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
ls -la src/vla/subtitle/probes/
```

- [ ] **Step 2: Check if probes/ depends on browser_record**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
grep -rn "browser_record\|BrowserRecorder" src/vla/subtitle/probes/
```

- [ ] **Step 3: Decide**

- If `probes/` imports `browser_record` (sole consumer) → delete entire directory
- If `probes/` is used by `browser_driver` (F2-14 ProbeStrategy refactor) → keep directory, just verify no broken imports

Based on spec §6 Q2 (resolved: F2-8 deletes `probes/` if sole consumer was `BrowserRecorder`):

```bash
# Check consumers of probes/
grep -rn "from vla.subtitle.probes\|vla.subtitle import probes" src/ tests/
```

- If only `browser_record` referenced `probes/` → `git rm -r src/vla/subtitle/probes/`
- If `browser_driver.py` references `probes/` (from R-14 ProbeStrategy refactor) → keep

- [ ] **Step 4: If deleting probes/**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git rm -r src/vla/subtitle/probes/
git commit -m "refactor(F2-8): delete subtitle/probes/ (BrowserRecorder sole user)"
```

- [ ] **Step 5: If keeping probes/, verify it still works**

Run: `uv run pytest tests/test_probe_strategy.py -v`
Expected: PASS (ProbeStrategy from R-14 still functional)

---

### Task 5: Final regression — 491 baseline + F2-* tests + doctor

**Files:**
- Test only

- [ ] **Step 1: Run full test suite**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run pytest tests/ -v --tb=short 2>&1 | tail -50
```

Document:
- Total tests run
- Pass count (target: 491 baseline + new F2-* tests, all PASS)
- Failures (must be 0)

- [ ] **Step 2: Run `vla doctor`**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run vla doctor
```

Expected: green (no errors). Q8: `--check-screenshot` may print TCC warning if permission denied, but doctor still exit 0.

- [ ] **Step 3: Grep audit (acceptance criteria from spec §7)**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent

echo "BrowserRecorder class remaining:"
grep -rn "class BrowserRecorder" src/vla/  # expect 0

echo "video.record / ffmpeg.*screen remaining:"
grep -rn "video\.record\|ffmpeg.*screen" src/vla/  # expect 0

echo "browser_record.py file:"
ls src/vla/subtitle/browser_record.py 2>&1  # expect No such file

echo "test_browser_record.py file:"
ls tests/test_browser_record.py 2>&1  # expect No such file
```

- [ ] **Step 4: Commit (only if all green)**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git commit --allow-empty -m "chore(F2-8): F2-8 BrowserRecorder deletion regression pass"
```

---

## Acceptance Criteria (from spec §7 Audit gates)

- [ ] `ls src/vla/subtitle/browser_record.py` → **file not found**
- [ ] `ls tests/test_browser_record.py` → **file not found**
- [ ] `grep -rn "class BrowserRecorder" src/vla/` → **0 matches**
- [ ] `grep -rn "video.record\|ffmpeg.*screen" src/vla/` → **0 matches**
- [ ] `grep -rn "BrowserRecorder\|browser_record" src/vla/ tests/` → **0 matches** (after Task 3)
- [ ] All 491 baseline tests still PASS + new F2-* tests PASS
- [ ] `vla doctor` green (Q8: TCC deny → warn, exit 0)

## Dependency Note

F2-8 depends on **all previous F2-* plans merged** (F2-1 ~ F2-7). Specifically F2-7 must merge first because it stops importing `BrowserRecorder` in strategy.py. After F2-8, BrowserRecorder is gone forever — no back-compat shim.