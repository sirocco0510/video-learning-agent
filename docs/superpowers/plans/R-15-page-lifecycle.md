# R.15 — Page Lifecycle Ownership (Caller Responsible)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `finally: page.close()` safety net in `BrowserRecordStrategy`. Page lifecycle is owned by the caller (session / fixture / CLI loop). Closing the wrong page at the wrong time is the most common source of flaky E2E tests.

**Architecture:**
- `BrowserRecordStrategy.record()` no longer wraps its body in `try/finally: page.close()`
- Caller (CLI / `main_provider.process` / pytest fixture) opens + closes the page
- `BrowserRecordStrategy` accepts a `page: Page` parameter (already does) and uses it
- The strategy does NOT close the page on success or on exception

**Tech Stack:** stdlib only

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §E Sub-4

## Global Constraints

- Strategy does not own `page` lifetime; caller does
- `cli.py` / `main_provider.py` / conftest fixtures must explicitly close pages they open
- Pytest fixtures that currently relied on the implicit close need to close themselves (via `yield` + `try/finally`)

---

### Task 1: Audit current `finally: page.close()` usage

**Files:**
- Audit: `src/vla/subtitle/browser_record.py`, `src/vla/cli.py`, `src/vla/main_provider.py`, `tests/conftest.py`, all test files using playwright

- [ ] **Step 1: Find every `page.close()`**

Run: `grep -rn "page.close()\|finally:" src/vla/subtitle/browser_record.py src/vla/cli.py src/vla/main_provider.py tests/conftest.py`

- [ ] **Step 2: Find `try:` blocks in `browser_record.py`**

Run: `grep -n "try:" src/vla/subtitle/browser_record.py`

Expected: A `try:` block wrapping `record()` body, with `finally: page.close()` cleanup.

---

### Task 2: Write failing tests for caller-owned lifecycle

**Files:**
- Create: `tests/test_page_lifecycle.py`

**Interfaces:**
- `BrowserRecordStrategy.record(url, page, ...)` does NOT close `page` on success
- `BrowserRecordStrategy.record(url, page, ...)` does NOT close `page` on exception

- [ ] **Step 1: Write tests**

```python
# tests/test_page_lifecycle.py
from unittest.mock import MagicMock, patch

import pytest

from vla.config import VLAConfig
from vla.subtitle.browser_record import BrowserRecordStrategy


@pytest.fixture
def strategy(tmp_path) -> BrowserRecordStrategy:
    cfg = VLAConfig.from_yaml("config/vla.yaml")
    return BrowserRecordStrategy(
        download_dir=tmp_path,
        cfg=cfg,
    )


class TestCallerOwnsLifecycle:
    def test_record_does_not_close_page_on_success(self, strategy):
        page = MagicMock()
        # simulate download already on disk
        with patch.object(strategy, "_probe", return_value=True), \
             patch.object(strategy, "_download_via_page", return_value=strategy.download_dir / "x.mp4"), \
             patch.object(strategy, "_post_download", return_value=None):
            out = strategy.record("https://www.bilibili.com/video/BV1", page, "BV1", 100)
        assert out is not None
        page.close.assert_not_called()

    def test_record_does_not_close_page_on_exception(self, strategy):
        page = MagicMock()
        with patch.object(strategy, "_probe", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                strategy.record("https://x", page, "x", 1)
        page.close.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_page_lifecycle.py -v`
Expected: Fails because current code closes page in finally

---

### Task 3: Remove `finally: page.close()` from `BrowserRecordStrategy.record()`

**Files:**
- Modify: `src/vla/subtitle/browser_record.py` (the `record()` method)

- [ ] **Step 1: Find the try/finally block**

Look for the `record()` method (or equivalent entry point). It probably looks like:

```python
def record(self, url, page, ...):
    try:
        ...
        return result
    finally:
        page.close()
```

- [ ] **Step 2: Remove the `finally: page.close()`**

Change to:

```python
def record(self, url, page, ...):
    """Page 生命周期由 caller 负责(SSOT: spec §E Sub-4,2026-09-03)。
    本方法不再 close page,无论是成功还是抛异常。
    """
    ...
    return result
```

Do NOT add any `page.close()` calls inside the body (including on early-return paths).

- [ ] **Step 3: Run lifecycle tests**

Run: `uv run pytest tests/test_page_lifecycle.py -v`
Expected: Pass

- [ ] **Step 4: Run browser_record tests**

Run: `uv run pytest tests/test_browser_record.py -v`
Expected: All pass (existing tests already create + close pages explicitly)

- [ ] **Step 5: Commit**

```bash
git add src/vla/subtitle/browser_record.py tests/test_page_lifecycle.py
git commit -m "refactor(browser_record): caller owns page lifecycle"
```

---

### Task 4: Update callers to close pages explicitly

**Files:**
- Modify: `src/vla/cli.py`
- Modify: `src/vla/main_provider.py`
- Modify: `tests/conftest.py` (if it has a `page` fixture)

- [ ] **Step 1: Find every `with sync_playwright()` / `browser.new_page()` site**

Run: `grep -rn "browser.new_page\|sync_playwright" src/ tests/`

For each site, ensure there's an explicit `page.close()` in a `finally:` or use the playwright context manager (`with browser.new_page() as page:`).

- [ ] **Step 2: Update CLI / main_provider**

In `cli.py` and `main_provider.py`, wrap `page` usage:

```python
page = browser.new_page()
try:
    strategy.record(url, page, video_id, duration)
    ...
finally:
    page.close()
```

- [ ] **Step 3: Update pytest fixtures**

In `tests/conftest.py`, any `page` fixture should `yield page` and then close in the fixture teardown (if not already using playwright's built-in `page` fixture, which closes automatically).

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/vla/cli.py src/vla/main_provider.py tests/conftest.py
git commit -m "refactor(cli/main): close pages explicitly (caller owns lifecycle)"
```

---

### Task 5: Verify

- [ ] **Step 1: No `page.close()` in `browser_record.py`**

Run: `grep -n "page.close" src/vla/subtitle/browser_record.py`
Expected: no output

- [ ] **Step 2: Full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 3: `vla doctor`**

Run: `uv run vla doctor`
Expected: All checks pass

- [ ] **Step 4: Manual smoke test (optional)**

Run: `uv run vla process --url "https://www.bilibili.com/video/BV1yyMQ6kEh6/" --duration 10 --dry-run`
Expected: command runs without "page already closed" or "page is closed" errors

- [ ] **Step 5: Commit any stragglers**

```bash
git status  # clean
```
