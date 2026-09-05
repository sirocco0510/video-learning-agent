# R.3 — Shared LLM Prompt Utilities

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hoist prompt construction helpers from `quality/checker.py` and `quality/refiner.py` into a shared `llm/prompts.py` module.

**Architecture:** Two helpers — `build_chat_prompt(system, user)` joins system + user prompts; `enforce_json_response(system, extra="只输出 JSON")` appends a JSON-output constraint to the system prompt.

**Tech Stack:** Python 3.12, stdlib only

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §B #7

## Global Constraints

- `LLMClientLike.complete(prompt: str, ...)` takes a single string (system+user joined)
- All current behavior must remain — JSON-only enforcement on refine + checker
- Tests stay green

---

### Task 1: Create `llm/prompts.py`

**Files:**
- Create: `src/vla/llm/prompts.py`
- Create: `tests/test_prompts.py`

**Interfaces:**
- Produces: `build_chat_prompt(system: str, user: str) -> str`
- Produces: `enforce_json_response(system: str, *, extra: str = "只输出 JSON") -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts.py
from vla.llm.prompts import build_chat_prompt, enforce_json_response


class TestBuildChatPrompt:
    def test_joins_system_and_user_with_blank_line(self):
        result = build_chat_prompt("SYS", "USER")
        assert result == "SYS\n\nUSER"

    def test_empty_system(self):
        assert build_chat_prompt("", "USER") == "\nUSER"

    def test_empty_user(self):
        assert build_chat_prompt("SYS", "") == "SYS\n\n"

    def test_multiline_preserved(self):
        sys = "line1\nline2"
        user = "lineA\nlineB"
        assert build_chat_prompt(sys, user) == "line1\nline2\n\nlineA\nlineB"


class TestEnforceJsonResponse:
    def test_default_extra_appended(self):
        result = enforce_json_response("base system")
        assert "只输出 JSON" in result
        assert result.startswith("base system")

    def test_custom_extra(self):
        result = enforce_json_response("base", extra="Respond with JSON only")
        assert "Respond with JSON only" in result

    def test_appended_at_end_with_blank_line_separator(self):
        result = enforce_json_response("base")
        # 双换行分隔,LLM 容易把约束当独立指令
        assert result.endswith("只输出 JSON")

    def test_no_duplicate_appending(self):
        """如果已经包含 JSON 指令,不要重复加(避免 prompt 变长)。"""
        first = enforce_json_response("base")
        second = enforce_json_response(first)
        assert second.count("只输出 JSON") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prompts.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: Write the implementation**

Create `src/vla/llm/prompts.py`:

```python
"""LLM prompt 工具(SSOT: spec §B #7,2026-09-03)。

集中 system + user 拼接 + JSON-only 约束追加,避免每个 LLM 调用模块
重复硬编码。
"""

from __future__ import annotations


def build_chat_prompt(system: str, user: str) -> str:
    """拼接 system + user prompt。

    LLMClientLike.complete() 只接受单字符串,所以调用方需要预先 join。
    """
    return f"{system}\n\n{user}"


def enforce_json_response(system: str, *, extra: str = "只输出 JSON") -> str:
    """在 system prompt 末尾追加"只输出 JSON"约束。

    - 默认追加"只输出 JSON"
    - 已包含则跳过(避免 prompt 膨胀)
    """
    if extra in system:
        return system
    return f"{system}\n\n{extra}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_prompts.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/llm/prompts.py tests/test_prompts.py
git commit -m "feat(llm): shared build_chat_prompt + enforce_json_response"
```

---

### Task 2: Refine to use `enforce_json_response`

**Files:**
- Modify: `src/vla/quality/refiner.py` (the `_SYSTEM_PROMPT` constant)

**Interfaces:**
- Consumes: `from vla.llm.prompts import build_chat_prompt, enforce_json_response`

- [ ] **Step 1: Write failing test confirming system prompt enforces JSON**

In `tests/test_refiner.py`, the existing TestRefineFlow already covers this implicitly via the `_parse_json` test. No new test needed — proceed.

- [ ] **Step 2: Update `_SYSTEM_PROMPT`**

The existing `_SYSTEM_PROMPT` already says `【输出格式 — 严格 JSON,只输出 JSON,不要其他文字】`. No change needed. The current prompt text is fine — `enforce_json_response` is available but not strictly needed if the system already states it.

However, we still want to use `build_chat_prompt` for the join. Update the call site in `refiner.py`:

Replace:
```python
full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_prompt}"
```
with:
```python
from vla.llm.prompts import build_chat_prompt
full_prompt = build_chat_prompt(_SYSTEM_PROMPT, user_prompt)
```

- [ ] **Step 3: Run refiner tests**

Run: `uv run pytest tests/test_refiner.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/vla/quality/refiner.py
git commit -m "refactor(refiner): use shared build_chat_prompt"
```

---

### Task 3: QualityChecker uses `build_chat_prompt`

**Files:**
- Modify: `src/vla/quality/checker.py` (the `PROMPT` constant usage)

**Interfaces:**
- Consumes: `from vla.llm.prompts import build_chat_prompt`

- [ ] **Step 1: Find and update prompt join**

In `checker.py`, search for the place where `PROMPT` is interpolated into the actual call. Replace any `f"{PROMPT}..."` or string concatenation with `build_chat_prompt(PROMPT, user_prompt)`.

- [ ] **Step 2: Run all quality + refiner tests**

Run: `uv run pytest tests/test_checker.py tests/test_refiner.py tests/test_prompts.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/vla/quality/checker.py
git commit -m "refactor(checker): use shared build_chat_prompt"
```

---

### Task 4: Summarizer uses `build_chat_prompt`

**Files:**
- Modify: `src/vla/summary/llm_summarizer.py`

**Interfaces:**
- Consumes: `from vla.llm.prompts import build_chat_prompt`

- [ ] **Step 1: Find prompt construction**

Search for `SUMMARIZE_BATCH_PROMPT` usage. Replace concatenation with `build_chat_prompt`.

- [ ] **Step 2: Run summarizer tests**

Run: `uv run pytest tests/test_llm_summarizer.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/vla/summary/llm_summarizer.py
git commit -m "refactor(summarizer): use shared build_chat_prompt"
```

---

### Task 5: Verify

- [ ] **Step 1: Full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 2: Grep verify shared usage**

Run: `grep -rn "from vla.llm.prompts" src/vla/`
Expected: checker, refiner, summarizer all show the import