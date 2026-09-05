# R.13 — `utils/json_walk.py` (Recursive String Walker)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a generic recursive walker for arbitrary nested JSON-like structures (dict / list / str / number / bool / None). Used by `SubtitleRefiner.refine_segments` to operate only on string leaves, regardless of how the structure is shaped.

**Architecture:**
- `src/vla/utils/json_walk.py`:
  - `def walk_strings(value: Any, visit: Callable[[str], str]) -> Any`
    - Recursively descends into dict and list; when it finds a `str`, calls `visit(s)` and substitutes the result
    - All other types pass through unchanged
  - `def walk_strings_inplace(value: Any, visit: Callable[[str], str]) -> None`
    - Same, but mutates dicts/lists in place (no return value)
- Use cases:
  - `SubtitleRefiner.refine_segments`: walk the (possibly nested) `segments` list of dicts and apply refinement to the `text` field
  - Anywhere we need to post-process LLM JSON output recursively

**Tech Stack:** stdlib only

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §E Sub-2

## Global Constraints

- Pure functions; no I/O, no logging, no LLM calls
- Walker is recursive; depth limit not enforced (Python recursion is fine for the structures we handle)
- Test depth ≥ 3 (nested dicts + lists + strings at leaves)

---

### Task 1: Write failing tests for `walk_strings`

**Files:**
- Create: `tests/test_json_walk.py`

**Interfaces:**
- `def walk_strings(value: Any, visit: Callable[[str], str]) -> Any`
- `def walk_strings_inplace(value: Any, visit: Callable[[str], str]) -> None`

- [ ] **Step 1: Write tests**

```python
# tests/test_json_walk.py
from typing import Any

import pytest

from vla.utils.json_walk import walk_strings, walk_strings_inplace


class TestWalkStringsBasic:
    def test_string_passthrough(self):
        assert walk_strings("hello", lambda s: s.upper()) == "HELLO"

    def test_int_passthrough(self):
        assert walk_strings(42, lambda s: s.upper()) == 42

    def test_float_passthrough(self):
        assert walk_strings(3.14, lambda s: s.upper()) == 3.14

    def test_bool_passthrough(self):
        assert walk_strings(True, lambda s: s.upper()) is True

    def test_none_passthrough(self):
        assert walk_strings(None, lambda s: s.upper()) is None


class TestWalkStringsCollections:
    def test_list_of_strings(self):
        assert walk_strings(["a", "b"], lambda s: s * 2) == ["aa", "bb"]

    def test_dict_of_strings(self):
        d = {"title": "T", "score": 90}
        out = walk_strings(d, lambda s: s.upper())
        assert out == {"title": "T", "score": 90}
        assert out["title"] == "T"
        assert out["score"] == 90  # int untouched

    def test_mixed_list(self):
        v = ["text", 42, None, True, "more"]
        out = walk_strings(v, lambda s: s + "!")
        assert out == ["text!", 42, None, True, "more!"]

    def test_nested_dict_in_list(self):
        v = [{"text": "a"}, {"text": "b", "score": 1}, "c"]
        out = walk_strings(v, lambda s: s + "?")
        assert out == [{"text": "a?"}, {"text": "b?", "score": 1}, "c?"]


class TestWalkStringsDeepNesting:
    def test_3_levels_dict(self):
        v = {"a": {"b": {"c": "leaf"}}}
        assert walk_strings(v, lambda s: s + "!") == {"a": {"b": {"c": "leaf!"}}}

    def test_dict_list_dict_string(self):
        v = {"items": [{"name": "x"}, {"name": "y"}]}
        out = walk_strings(v, lambda s: s.upper())
        assert out == {"items": [{"name": "X"}, {"name": "Y"}]}

    def test_empty_collections(self):
        assert walk_strings([], lambda s: s) == []
        assert walk_strings({}, lambda s: s) == {}


class TestVisitSideEffects:
    def test_visit_count(self):
        v = {"a": "x", "b": ["y", "z"], "c": 1}
        calls: list[str] = []
        walk_strings(v, calls.append)
        assert sorted(calls) == ["x", "y", "z"]


class TestWalkStringsInplace:
    def test_mutates_dict(self):
        d = {"text": "abc", "score": 1}
        walk_strings_inplace(d, lambda s: s.upper())
        assert d["text"] == "ABC"
        assert d["score"] == 1

    def test_mutates_list_in_dict(self):
        d = {"items": ["a", "b"]}
        walk_strings_inplace(d, lambda s: s + "!")
        assert d["items"] == ["a!", "b!"]

    def test_returns_none(self):
        d: Any = {"text": "abc"}
        ret = walk_strings_inplace(d, lambda s: s)
        assert ret is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_json_walk.py -v`
Expected: ModuleNotFoundError

---

### Task 2: Implement `utils/json_walk.py`

**Files:**
- Create: `src/vla/utils/__init__.py` (empty)
- Create: `src/vla/utils/json_walk.py`

**Interfaces:**
- `def walk_strings(value: Any, visit: Callable[[str], str]) -> Any`
- `def walk_strings_inplace(value: Any, visit: Callable[[str], str]) -> None`

- [ ] **Step 1: Implement the module**

Create `src/vla/utils/json_walk.py`:

```python
"""JSON 通用递归字符串 walker(SSOT: spec §E Sub-2,2026-09-03)。

用于:
- SubtitleRefiner 对任意结构的 segments 做"只对字符串叶子下手"的整理
- 任何对 LLM JSON 输出做后处理的场景

约束:
- 纯函数,无副作用(除 walk_strings_inplace 显式原地版本)
- 不假定 schema;只看类型分支(dict / list / str / 其他)
"""

from __future__ import annotations

from typing import Any, Callable


VisitFn = Callable[[str], str]


def walk_strings(value: Any, visit: VisitFn) -> Any:
    """递归把每个 str 叶子过一遍 visit,返回新结构(其他类型透传)。

    - dict: 逐项递归 value,保留 key
    - list / tuple: 逐项递归,list 返回 list,tuple 返回 tuple
    - str: visit(s)
    - 其他(int / float / bool / None): 原样返回
    """
    if isinstance(value, str):
        return visit(value)
    if isinstance(value, dict):
        return {k: walk_strings(v, visit) for k, v in value.items()}
    if isinstance(value, list):
        return [walk_strings(item, visit) for item in value]
    if isinstance(value, tuple):
        return tuple(walk_strings(item, visit) for item in value)
    return value


def walk_strings_inplace(value: Any, visit: VisitFn) -> None:
    """原地变异 dict / list;str 原地替换为 visit(s);其他类型不动。

    没有返回值(caller 已持有引用)。
    """
    if isinstance(value, str):
        return  # 标量原地变异无意义;此 walker 用在容器上
    if isinstance(value, dict):
        for k, v in list(value.items()):
            if isinstance(v, str):
                value[k] = visit(v)
            else:
                walk_strings_inplace(v, visit)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, str):
                value[i] = visit(item)
            else:
                walk_strings_inplace(item, visit)
        return
    # 其他类型不动
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_json_walk.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/vla/utils/__init__.py src/vla/utils/json_walk.py tests/test_json_walk.py
git commit -m "feat(utils): walk_strings + walk_strings_inplace"
```

---

### Task 3: Use `walk_strings` in `SubtitleRefiner`

**Files:**
- Modify: `src/vla/subtitle/refiner.py`

- [ ] **Step 1: Replace per-field text mutation**

Find the function in `refiner.py` that mutates segments one field at a time (probably iterates over known keys like `text` / `start` / `end`). Replace with:

```python
from vla.utils.json_walk import walk_strings_inplace

def refine_segments(self, segments: list[dict]) -> list[dict]:
    """对 segments 里所有字符串叶子做整理。Schema 无关。"""
    for seg in segments:
        walk_strings_inplace(seg, self._refine_text)
    return segments

def _refine_text(self, s: str) -> str:
    """单条字符串的整理(繁简 + 错字)。"""
    # 原来的繁简转换 + 错字字典逻辑保持不变
    ...
```

- [ ] **Step 2: Run refiner tests**

Run: `uv run pytest tests/test_subtitle_refiner.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/vla/subtitle/refiner.py
git commit -m "refactor(refiner): use walk_strings_inplace for segment text"
```

---

### Task 4: Verify

- [ ] **Step 1: Full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 2: `vla doctor`**

Run: `uv run vla doctor`
Expected: All checks pass

- [ ] **Step 3: Final grep no stale walker reimplementations**

Run: `grep -rn "def _walk\|_walk_strings\|for.*in.*segments.*_refine" src/vla/subtitle/`
Expected: only references to `walk_strings_inplace` from utils
