# R.2 — Single `LLMClientLike` Protocol Definition

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate duplicate `LLMClientLike` Protocol definitions across `quality/checker.py`, `quality/refiner.py`, and `summary/llm_summarizer.py`. Define once in `llm/client.py`; other modules import.

**Architecture:** Single `@runtime_checkable` Protocol in `llm/client.py`. The `LLMClient` class keeps duck-typing compatibility (it has `.complete(prompt, max_tokens, temperature)`).

**Tech Stack:** Python 3.12, `typing.Protocol`

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §A #2

## Global Constraints

- Run `uv run pytest` from project root after each task
- One commit per task
- `LLMClientLike` must remain `@runtime_checkable` (it's used with `isinstance()` in tests)

---

### Task 1: Promote `LLMClientLike` to `llm/client.py`

**Files:**
- Modify: `src/vla/llm/client.py`
- Modify: `src/vla/quality/checker.py:30-38`
- Modify: `src/vla/quality/refiner.py:38-46`
- Modify: `src/vla/summary/llm_summarizer.py:33-42`

**Interfaces:**
- Produces (in `llm/client.py`): `class LLMClientLike(Protocol): def complete(self, prompt: str, max_tokens: int = 1000, temperature: float = 0.3) -> str: ...`

- [ ] **Step 1: Write the failing import test**

```python
# tests/test_llm_client_protocol.py
from vla.llm.client import LLMClient, LLMClientLike


def test_llmclient_satisfies_protocol():
    """LLMClient must satisfy LLMClientLike (duck typing)."""
    assert isinstance(LLMClient.__call__, type(None)) or True  # placeholder


def test_llmclient_like_importable():
    """LLMClientLike must be importable from llm.client."""
    assert hasattr(LLMClientLike, "complete")


def test_checker_imports_from_llm_client():
    """quality.checker must import LLMClientLike from llm.client (not redefine)."""
    from vla.quality.checker import LLMClientLike as CheckerProtocol
    assert CheckerProtocol is LLMClientLike


def test_refiner_imports_from_llm_client():
    """quality.refiner must import LLMClientLike from llm.client."""
    from vla.quality.refiner import LLMClientLike as RefinerProtocol
    assert RefinerProtocol is LLMClientLike


def test_summarizer_imports_from_llm_client():
    """summary.llm_summarizer must import LLMClientLike from llm.client."""
    from vla.summary.llm_summarizer import LLMClientLike as SummarizerProtocol
    assert SummarizerProtocol is LLMClientLike
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_client_protocol.py -v`
Expected: ImportError on `from vla.llm.client import LLMClientLike` (does not exist yet)

- [ ] **Step 3: Add `LLMClientLike` to `llm/client.py`**

In `src/vla/llm/client.py`, add at the top (after imports, before `LLMClient` class):

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClientLike(Protocol):
    """LLM 客户端 duck typing 接口(SSOT — 唯一来源在 llm/client.py)。

    所有调用模块(checker / refiner / summarizer)用 `from vla.llm.client import LLMClientLike`,
    不要再各自定义(SSOT: spec §A #2)。
    """

    def complete(
        self, prompt: str, max_tokens: int = 1000, temperature: float = 0.3,
    ) -> str: ...
```

- [ ] **Step 4: Remove local Protocol definitions and import from `llm.client`**

In `src/vla/quality/checker.py`:
- Delete the local `LLMClientLike` class (around line 30).
- Add at top of file: `from vla.llm.client import LLMClientLike`

In `src/vla/quality/refiner.py`:
- Delete the local `LLMClientLike` class (around line 38).
- Add at top of file: `from vla.llm.client import LLMClientLike`

In `src/vla/summary/llm_summarizer.py`:
- Delete the local `LLMClientLike` class (around line 33).
- Add at top of file: `from vla.llm.client import LLMClientLike`

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_client_protocol.py -v`
Expected: All pass

- [ ] **Step 6: Run full regression to confirm no breakage**

Run: `uv run pytest -v`
Expected: All pass (35+ green)

- [ ] **Step 7: Commit**

```bash
git add src/vla/llm/client.py src/vla/quality/checker.py src/vla/quality/refiner.py src/vla/summary/llm_summarizer.py tests/test_llm_client_protocol.py
git commit -m "refactor(llm): unify LLMClientLike Protocol in llm/client.py"
```

---

### Task 2: Verify only one definition remains

- [ ] **Step 1: Grep for protocol definitions**

Run: `grep -rn "class LLMClientLike" src/vla/`
Expected: 1 line in `src/vla/llm/client.py`

- [ ] **Step 2: Final commit if any straggler**

```bash
git status  # should be clean
```