# R.7 — Delete `FailureAlert` (Dead Code)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `FailureAlert` and all references. User decision 2026-09-03: completely delete, including the matching test class and protocol methods.

**Architecture:** This is pure deletion. No replacement. `implementation-plan.md` chapter is preserved as historical snapshot per user decision (frozen).

**Tech Stack:** N/A

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §A #3, #4

## Global Constraints

- `implementation-plan.md` is FROZEN — do not edit any chapter
- `MacOSNotifier.alert()` stays unchanged (only `alert_blocking` was the missing method)
- `tests/test_e2e.py` keeps other tests; only `TestFailureAlertE2E` is deleted

---

### Task 1: Verify no production callers

**Files:**
- Audit: `src/vla/`, `tests/`

- [ ] **Step 1: Confirm FailureAlert is unreachable from main.py / cli.py**

Run: `grep -rn "FailureAlert\|failure_alert" src/vla/main.py src/vla/cli.py src/vla/main_provider.py`
Expected: no output

If there ARE callers in main/cli/main_provider, STOP and ask user — failure mode is different from "dead code".

- [ ] **Step 2: Find every reference to the module**

Run: `grep -rln "FailureAlert\|failure_alert" src/ tests/`
Expected output (with line counts):
- `src/vla/log/failure_alert.py`
- `tests/test_e2e.py`

---

### Task 2: Delete `src/vla/log/failure_alert.py`

**Files:**
- Delete: `src/vla/log/failure_alert.py`

- [ ] **Step 1: Remove the file**

Run: `rm src/vla/log/failure_alert.py`

- [ ] **Step 2: Verify Python still imports the log package**

Run: `uv run python -c "from vla.log.transcription_log import TranscriptionLog; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add -u src/vla/log/failure_alert.py
git commit -m "refactor(log): delete unused FailureAlert module"
```

---

### Task 3: Delete `TestFailureAlertE2E` from `tests/test_e2e.py`

**Files:**
- Modify: `tests/test_e2e.py`

- [ ] **Step 1: Find the test class boundaries**

Run: `grep -n "class TestFailureAlertE2E\|^class " tests/test_e2e.py`
Find the line range of the `TestFailureAlertE2E` class.

- [ ] **Step 2: Remove the test class**

Delete the `TestFailureAlertE2E` class block (from `class TestFailureAlertE2E:` line through the next blank line before the next class or end-of-file).

- [ ] **Step 3: Remove any stub notifier `alert_blocking` if it exists in test file**

Run: `grep -n "alert_blocking" tests/test_e2e.py`
If any test stub defined `alert_blocking` and that stub is now unused, also remove it. If it's used by remaining tests, keep it.

- [ ] **Step 4: Run e2e tests**

Run: `uv run pytest tests/test_e2e.py -v`
Expected: remaining tests pass; `TestFailureAlertE2E` no longer appears

- [ ] **Step 5: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test(e2e): delete TestFailureAlertE2E (FailureAlert removed)"
```

---

### Task 4: Final verification

- [ ] **Step 1: No remaining references**

Run: `grep -rln "FailureAlert\|failure_alert" src/ tests/`
Expected: no output

- [ ] **Step 2: Full regression**

Run: `uv run pytest -v`
Expected: All pass (35+ green)

- [ ] **Step 3: `vla doctor` smoke test**

Run: `uv run vla doctor`
Expected: All checks pass

- [ ] **Step 4: Final commit if any straggler**

```bash
git status  # should be clean
```