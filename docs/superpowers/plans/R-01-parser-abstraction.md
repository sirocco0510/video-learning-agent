# R.1 — LLM JSON Response Parser Abstraction

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hoist the brace-counting JSON parser from `SubtitleRefiner` into a shared `llm/response.py` module, then have both `QualityChecker` and `SubtitleRefiner` use it.

**Architecture:** Single function `parse_json_response(text, *, strip_think=True, try_code_blocks=True) -> dict` that strips `<think>…</think>` blocks, tries ```…``` code blocks, then falls back to brace-counting from any `{` position. Replaces both call sites' internal parsers.

**Tech Stack:** Python 3.12, stdlib only (`re`, `json`)

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §B #6

## Global Constraints

- Run `uv run pytest tests/test_response_parser.py -v` from project root
- TDD: each step writes test before implementation
- One commit per task with descriptive message
- All existing tests must continue to pass (35+ currently green)

---

### Task 1: Create `llm/response.py` with the parser

**Files:**
- Create: `src/vla/llm/response.py`
- Create: `tests/test_response_parser.py`

**Interfaces:**
- Produces: `parse_json_response(text: str, *, strip_think: bool = True, try_code_blocks: bool = True) -> dict` — raises `ValueError` if no JSON found
- Produces (test only): `_try_parse_balanced_object(text: str, start: int = 0) -> dict | None` — internal, brace-counts from `start`, returns None on parse failure

- [ ] **Step 1: Write the failing test**

```python
# tests/test_response_parser.py
import pytest
from vla.llm.response import parse_json_response, _try_parse_balanced_object


class TestStripThink:
    def test_strips_think_block_with_json_after(self):
        text = "<think>\n让我想想\n</think>\n{\"pass\": true, \"score\": 80}"
        assert parse_json_response(text) == {"pass": True, "score": 80}

    def test_strips_think_block_with_nested_braces_in_thought(self):
        text = '<think>\n例子 {"a": 1} 不重要\n</think>\n{"x": 2}'
        assert parse_json_response(text) == {"x": 2}

    def test_no_think_block_returns_json_directly(self):
        assert parse_json_response('{"k": "v"}') == {"k": "v"}

    def test_strip_think_false_keeps_think_block(self):
        text = '<think>{"x": 1}</think>{"y": 2}'
        # When strip_think=False, we still try code blocks + brace scan,
        # but {"y": 2} comes AFTER think so it should still be found
        result = parse_json_response(text, strip_think=False)
        assert result == {"y": 2}


class TestCodeBlocks:
    def test_json_code_block(self):
        text = '```json\n{"a": 1, "b": [2, 3]}\n```'
        assert parse_json_response(text) == {"a": 1, "b": [2, 3]}

    def test_plain_code_block_with_json(self):
        text = '```\n{"only": "json inside"}\n```'
        assert parse_json_response(text) == {"only": "json inside"}

    def test_try_code_blocks_false_falls_through(self):
        text = '```json\n{"x": 1}\n``` {"y": 2}'
        # With try_code_blocks=False, only the bare JSON gets parsed
        assert parse_json_response(text, try_code_blocks=False) == {"y": 2}


class TestBraceCounting:
    def test_finds_first_balanced_object(self):
        text = '前缀文字 {"key": "value"} 后缀'
        assert parse_json_response(text) == {"key": "value"}

    def test_handles_nested_objects(self):
        text = '{"outer": {"inner": {"deep": 1}}, "tail": true}'
        assert parse_json_response(text) == {"outer": {"inner": {"deep": 1}}, "tail": True}

    def test_handles_strings_with_braces(self):
        text = '{"text": "hello {world}"}'
        assert parse_json_response(text) == {"text": "hello {world}"}

    def test_handles_escaped_quotes(self):
        text = r'{"a": "say \"hi\""}'
        assert parse_json_response(text) == {"a": 'say "hi"'}


class TestMultipleJsonObjects:
    def test_picks_first_balanced_object(self):
        text = '{"first": 1} {"second": 2}'
        assert parse_json_response(text) == {"first": 1}

    def test_picks_outermost_when_nested(self):
        text = '{"a": {"b": 1}}'
        assert parse_json_response(text) == {"a": {"b": 1}}


class TestFailure:
    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="LLM 响应中没有找到 JSON"):
            parse_json_response("no json here at all")

    def test_unclosed_brace_raises(self):
        with pytest.raises(ValueError, match="LLM 响应中没有找到 JSON"):
            parse_json_response('{"unclosed":')


class TestTryParseBalancedObject:
    def test_returns_none_when_start_not_brace(self):
        assert _try_parse_balanced_object("not json", start=0) is None

    def test_returns_none_on_invalid_json(self):
        assert _try_parse_balanced_object('{"a": }', start=0) is None

    def test_returns_dict_on_valid(self):
        result = _try_parse_balanced_object('{"x": 1}', start=0)
        assert result == {"x": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_response_parser.py -v`
Expected: ImportError or ModuleNotFoundError (`vla.llm.response` does not exist)

- [ ] **Step 3: Write the implementation**

Create `src/vla/llm/response.py`:

```python
"""LLM JSON response parser(SSOT: spec §B #6,2026-09-03)。

封装 brace-counting + think-block stripping + code-block scanning,作为所有
LLM 调用方(quality_checker / refiner)的统一解析入口。

为什么不用更简单的 regex:
- thinking model(MiniMax M2 / DeepSeek R1)会在输出前先输出 <think>...</think>,
  内含示例 JSON 会干扰普通 regex
- LLM 输出可能含未转义的引号 / nested 嵌套,brace-counting 配合 string-boundary
  处理比 regex 更稳
"""

from __future__ import annotations

import json
import re
from typing import Any


def parse_json_response(
    text: str,
    *,
    strip_think: bool = True,
    try_code_blocks: bool = True,
) -> dict[str, Any]:
    """从 LLM 响应中提取 JSON dict。

    策略顺序:
    1. 剥 `<think>...</think>` 块(strip_think=True 时)
    2. 扫 ```...``` 代码块,brace-counting 解析(try_code_blocks=True 时)
    3. 扫所有 `{` 起点,brace-counting 找 outermost {...}

    Raises:
        ValueError: 没找到任何合法 JSON 对象
    """
    if strip_think:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    if try_code_blocks:
        for m in re.finditer(r"```(?:json)?\\s*\\n?(.*?)\\n?```", text, re.DOTALL):
            inner = m.group(1).strip()
            data = _try_parse_balanced_object(inner)
            if data is not None:
                return data

    for match in re.finditer(r"\{", text):
        data = _try_parse_balanced_object(text, start=match.start())
        if data is not None:
            return data

    raise ValueError(f"LLM 响应中没有找到 JSON: {text[:200]}")


def _try_parse_balanced_object(
    text: str, start: int = 0,
) -> dict[str, Any] | None:
    """从 text[start] 开始 brace-counting(尊重字符串边界),parse outermost {...}。

    Returns:
        解析成功 → dict
        解析失败 → None(调用方继续尝试下一个起点)
    """
    if not text or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_response_parser.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/llm/response.py tests/test_response_parser.py
git commit -m "feat(llm): shared parse_json_response with brace-counting"
```

---

### Task 2: Migrate `SubtitleRefiner` to the new parser

**Files:**
- Modify: `src/vla/quality/refiner.py:247-312`

**Interfaces:**
- Consumes: `from vla.llm.response import parse_json_response` (new in Task 1)

- [ ] **Step 1: Delete the local `_parse_json` and `_try_parse_balanced_object`**

In `src/vla/quality/refiner.py`, delete lines 244-312 (the static method `_parse_json` and the module-level helper `_try_parse_balanced_object`). Keep `from __future__ import annotations`, `import ` lines, and the class body intact.

- [ ] **Step 2: Update the call site in `refine()`**

In `refiner.py:194`, replace:
```python
data = self._parse_json(response)
```
with:
```python
from vla.llm.response import parse_json_response
data = parse_json_response(response)
```

- [ ] **Step 3: Run refiner tests to verify no regression**

Run: `uv run pytest tests/test_refiner.py -v`
Expected: All tests still pass (the existing tests already cover think blocks and nested braces)

- [ ] **Step 4: Commit**

```bash
git add src/vla/quality/refiner.py
git commit -m "refactor(refiner): use shared llm.response parser"
```

---

### Task 3: Migrate `QualityChecker` to the new parser

**Files:**
- Modify: `src/vla/quality/checker.py:187-210`

**Interfaces:**
- Consumes: `from vla.llm.response import parse_json_response`

- [ ] **Step 1: Delete the local `_parse_json` static method**

In `src/vla/quality/checker.py`, delete the `_parse_json` method body (lines 187-210). The signature was `@staticmethod def _parse_json(text: str) -> dict[str, Any]`.

- [ ] **Step 2: Update the call site in `check()`**

In `checker.py:148`, replace:
```python
data = self._parse_json(response)
```
with:
```python
from vla.llm.response import parse_json_response
data = parse_json_response(response)
```

- [ ] **Step 3: Run all quality tests to verify no regression**

Run: `uv run pytest tests/test_checker.py tests/test_refiner.py tests/test_response_parser.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/vla/quality/checker.py
git commit -m "refactor(checker): use shared llm.response parser"
```

---

### Task 4: Verify full regression

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests pass (35+ green)

- [ ] **Step 2: Verify no stale parser code remains**

Run: `grep -rn "_parse_json\|_try_parse_balanced_object" src/vla/quality/`
Expected: no output (or only the new import in refiner.py:194)

- [ ] **Step 3: Commit any straggler changes (should be empty)**

```bash
git status  # should be clean
```