# R.5 — `utils/bvid.py` Shared BVID Utilities

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify the two BVID extractors (regex-based) currently scattered across `cli.py:229` and `bilibili_official.py:33`, plus add the URL key builder used by `state/history.py`.

**Architecture:** Two functions in `utils/bvid.py`:
- `extract_bvid(url: str) -> str | None` — silent on miss (returns None)
- `make_url_key(group_id: str, bvid: str, p: int | None = None) -> str` — returns `bilibili://group/<group_id>/<bvid>` or with `?p=<p>`

**Tech Stack:** Python 3.12 stdlib `re`

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §B #8

## Global Constraints

- `bilibili_official.extract_bv_id()` currently raises `ValueError` on miss — keep that semantic at the call site (callers do `bvid = extract_bvid(url) or raise ValueError(...)`)
- `state/history.URL_KEY_PREFIX = "bilibili://group/"` already exists; reuse rather than re-hardcode

---

### Task 1: Create `utils/bvid.py`

**Files:**
- Create: `src/vla/utils/__init__.py` (empty)
- Create: `src/vla/utils/bvid.py`
- Create: `tests/test_bvid.py`

**Interfaces:**
- Produces: `extract_bvid(url: str) -> str | None`
- Produces: `make_url_key(group_id: str, bvid: str, p: int | None = None) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bvid.py
from vla.utils.bvid import extract_bvid, make_url_key


class TestExtractBvid:
    def test_standard_url(self):
        assert extract_bvid("https://www.bilibili.com/video/BV1dzui69EYV") == "BV1dzui69EYV"

    def test_url_with_query_string(self):
        assert extract_bvid("https://www.bilibili.com/video/BV1vE411W7VE?p=1") == "BV1vE411W7VE"

    def test_short_url(self):
        assert extract_bvid("https://b23.tv/abc123") is None  # 短链没有 bvid 模式

    def test_no_bvid_returns_none(self):
        assert extract_bvid("https://example.com/") is None

    def test_empty_string_returns_none(self):
        assert extract_bvid("") is None

    def test_lowercase_bv_prefix(self):
        assert extract_bvid("https://www.bilibili.com/video/bv1xxx") == "bv1xxx"


class TestMakeUrlKey:
    def test_no_p(self):
        assert make_url_key("python", "BV1xxx") == "bilibili://group/python/BV1xxx"

    def test_with_p(self):
        assert make_url_key("python", "BV1xxx", p=2) == "bilibili://group/python/BV1xxx?p=2"

    def test_with_p_zero(self):
        assert make_url_key("g", "BV1", p=0) == "bilibili://group/g/BV1?p=0"

    def test_with_none_p_omits_query(self):
        assert make_url_key("g", "BV1", p=None) == "bilibili://group/g/BV1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bvid.py -v`
Expected: ModuleNotFoundError on `vla.utils.bvid`

- [ ] **Step 3: Write the implementation**

Create `src/vla/utils/__init__.py` (empty file).

Create `src/vla/utils/bvid.py`:

```python
"""BVID + URL key utilities(SSOT: spec §B #8,2026-09-03)。

集中所有 B站 bvid 提取 / URL key 构造,消除 cli.py / bilibili_official.py /
state/history.py 三处分散实现。
"""

from __future__ import annotations

import re

from vla.state.history import URL_KEY_PREFIX


_BVID_PATTERN = re.compile(r"(BV[a-zA-Z0-9]+)")


def extract_bvid(url: str) -> str | None:
    """从 B站 URL 提取 bvid;不命中返回 None(不抛错)。

    适用于 `https://www.bilibili.com/video/BVxxx` / `BVxxx?p=1`。
    对 b23.tv 短链因不含 bvid 模式,返回 None(调用方需自行 resolve)。
    """
    match = _BVID_PATTERN.search(url)
    return match.group(1) if match else None


def make_url_key(group_id: str, bvid: str, p: int | None = None) -> str:
    """构造 HistoryManager 用的 URL key。

    格式:`{URL_KEY_PREFIX}{group_id}/{bvid}` + 可选 `?p=<p>`
    URL_KEY_PREFIX 在 state/history.py 定义,保持 SSOT。
    """
    key = f"{URL_KEY_PREFIX}{group_id}/{bvid}"
    if p is not None:
        key += f"?p={p}"
    return key
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_bvid.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/utils/__init__.py src/vla/utils/bvid.py tests/test_bvid.py
git commit -m "feat(utils): bvid extract_bvid + make_url_key"
```

---

### Task 2: Migrate `cli.py` to use shared `extract_bvid`

**Files:**
- Modify: `src/vla/cli.py:225-230`

**Interfaces:**
- Consumes: `from vla.utils.bvid import extract_bvid`

- [ ] **Step 1: Replace ad-hoc regex**

In `cli.py`, find the `process()` function. Replace:
```python
if not bvid:
    m = re.search(r"/(BV[A-Za-z0-9]+)", url)
    bvid = m.group(1) if m else f"local_{abs(hash(url))}"
```
with:
```python
if not bvid:
    extracted = extract_bvid(url)
    bvid = extracted or f"local_{abs(hash(url))}"
```

Add at top of `cli.py`: `from vla.utils.bvid import extract_bvid`

- [ ] **Step 2: Verify import is still needed**

Run: `grep -n "^import re\|^from re " src/vla/cli.py`
If `re` is only used by the line we just removed, also remove `import re` from the top imports.

- [ ] **Step 3: Run tests**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/vla/cli.py
git commit -m "refactor(cli): use shared extract_bvid"
```

---

### Task 3: Migrate `bilibili_official.py` to use shared `extract_bvid`

**Files:**
- Modify: `src/vla/subtitle/bilibili_official.py:32-37`

**Interfaces:**
- Consumes: `from vla.utils.bvid import extract_bvid`

- [ ] **Step 1: Replace the local `extract_bv_id`**

In `bilibili_official.py`, find `extract_bv_id` (around line 32). Replace:
```python
def extract_bv_id(self, url: str) -> str:
    match = _BVID_PATTERN.search(url)
    if not match:
        raise ValueError(f"无法从 URL 提取 bvid: {url}")
    return match.group(1)
```
with:
```python
def extract_bv_id(self, url: str) -> str:
    """委托给 utils.bvid.extract_bvid(抛 ValueError 保持原行为)。"""
    bvid = extract_bvid(url)
    if not bvid:
        raise ValueError(f"无法从 URL 提取 bvid: {url}")
    return bvid
```

Add at top: `from vla.utils.bvid import extract_bvid`
Delete the local `_BVID_PATTERN` constant (now unused).

- [ ] **Step 2: Run bilibili tests**

Run: `uv run pytest tests/test_bilibili_official.py tests/test_bilibili_adapter.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/vla/subtitle/bilibili_official.py
git commit -m "refactor(bilibili): use shared extract_bvid"
```

---

### Task 4: Migrate `state/history.py` to use shared `make_url_key`

**Files:**
- Modify: `src/vla/state/history.py` (where URL keys are constructed)

**Interfaces:**
- Consumes: `from vla.utils.bvid import make_url_key`

- [ ] **Step 1: Find URL key construction**

Run: `grep -n "URL_KEY_PREFIX\|bilibili://" src/vla/state/history.py`

- [ ] **Step 2: Replace with `make_url_key`**

Wherever you find `f"{URL_KEY_PREFIX}{group_id}/{bvid}"` or similar, replace with `make_url_key(group_id, bvid)`.

Keep the `URL_KEY_PREFIX` constant exported for backwards compat (it's still imported by `utils/bvid.py`).

- [ ] **Step 3: Run history tests**

Run: `uv run pytest tests/test_history.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/vla/state/history.py
git commit -m "refactor(history): use shared make_url_key"
```

---

### Task 5: Verify

- [ ] **Step 1: Full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 2: Grep verify no stale BVID regex**

Run: `grep -rn "BV\[a-zA-Z0-9\]\|BV\[A-Za-z0-9\]" src/vla/`
Expected: only `src/vla/utils/bvid.py`