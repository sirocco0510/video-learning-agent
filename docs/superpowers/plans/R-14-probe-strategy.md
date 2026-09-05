# R.14 — `ProbeStrategy` Protocol + Strategy Registry

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded probe chain (`head_request → cc_get → referer_check → cookie_warmup`) with a strategy list registered under a `ProbeStrategy` Protocol. Adding new probes (per platform) becomes one new class + one `register()` line.

**Architecture:**
- `src/vla/subtitle/probe_strategy.py`:
  - `class ProbeStrategy(Protocol)`: `name: str`, `def match(url: str) -> bool`, `def run(url: str, ctx: ProbeContext) -> ProbeResult`
  - `class ProbeContext`: shared resources (http session, page, config)
  - `class ProbeResult`: `ok: bool`, `note: str = ""`, `extra: dict = Field(default_factory=dict)`
  - `class ProbeRegistry`: `register(strategy)`, `get_all_for(url: str) -> list[ProbeStrategy]`
- Default registry populated with: `HeadRequestProbe`, `RefererCheckProbe`, `CookieWarmupProbe`
- `BrowserRecordStrategy` iterates `registry.get_all_for(url)` instead of inline try/except ladder

**Tech Stack:** stdlib typing.Protocol, dataclasses, pydantic v2 for result

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §E Sub-3

## Global Constraints

- `BrowserRecordStrategy.probe(url)` becomes a registry-driven loop
- Existing probe behavior preserved (same set of HTTP calls + ordering)
- New probe = one new class + register; no edits to `BrowserRecordStrategy`

---

### Task 1: Write failing tests for `ProbeStrategy`

**Files:**
- Create: `tests/test_probe_strategy.py`

**Interfaces:**
- `class ProbeStrategy(Protocol)`: `name`, `match(url)`, `run(url, ctx) -> ProbeResult`
- `class ProbeContext`: `session`, `page`, `cfg`
- `class ProbeResult`: `ok: bool`, `note: str`, `extra: dict`
- `class ProbeRegistry`: `register(strategy)`, `get_all_for(url) -> list[ProbeStrategy]`

- [ ] **Step 1: Write tests**

```python
# tests/test_probe_strategy.py
from typing import Protocol
from unittest.mock import MagicMock

import pytest

from vla.subtitle.probe_strategy import (
    ProbeContext,
    ProbeRegistry,
    ProbeResult,
    ProbeStrategy,
)


class FakeProbeOk:
    name = "fake_ok"

    def match(self, url: str) -> bool:
        return True

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(ok=True, note="ok")


class FakeProbeSkip:
    name = "fake_skip"

    def match(self, url: str) -> bool:
        return False

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult:
        raise AssertionError("should not be called")


class FakeProbeFail:
    name = "fake_fail"

    def match(self, url: str) -> bool:
        return True

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(ok=False, note="boom")


class TestRegistryBasics:
    def test_register_and_list_all_for_url(self):
        reg = ProbeRegistry()
        reg.register(FakeProbeOk())
        reg.register(FakeProbeSkip())
        assert [s.name for s in reg.get_all_for("https://x")] == ["fake_ok"]

    def test_empty_registry_returns_empty(self):
        reg = ProbeRegistry()
        assert reg.get_all_for("https://x") == []


class TestRegistryOrder:
    def test_registration_order_preserved(self):
        reg = ProbeRegistry()

        class A:
            name = "a"

            def match(self, url):
                return True

            def run(self, url, ctx):
                return ProbeResult(ok=True)

        class B:
            name = "b"

            def match(self, url):
                return True

            def run(self, url, ctx):
                return ProbeResult(ok=True)

        reg.register(A())
        reg.register(B())
        assert [s.name for s in reg.get_all_for("https://x")] == ["a", "b"]


class TestProtocolConformance:
    def test_protocol_used_as_type_hint(self):
        reg = ProbeRegistry()
        # Runtime check: registering a non-Protocol object should still work
        # (Protocol is structural, so we don't enforce at runtime in the registry)
        reg.register(FakeProbeOk())
        assert len(reg.get_all_for("https://x")) == 1


class TestRunChain:
    """Demonstrates how BrowserRecordStrategy will iterate the chain."""

    def test_chain_stops_on_first_ok(self):
        reg = ProbeRegistry()
        reg.register(FakeProbeFail())
        reg.register(FakeProbeOk())
        reg.register(FakeProbeFail())
        results = []
        for strat in reg.get_all_for("https://x"):
            r = strat.run("https://x", ProbeContext(session=MagicMock(), page=MagicMock(), cfg=MagicMock()))
            results.append(r)
            if r.ok:
                break
        assert len(results) == 2
        assert results[-1].ok is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_probe_strategy.py -v`
Expected: ModuleNotFoundError

---

### Task 2: Implement `probe_strategy.py`

**Files:**
- Create: `src/vla/subtitle/probe_strategy.py`

- [ ] **Step 1: Implement the module**

Create `src/vla/subtitle/probe_strategy.py`:

```python
"""探针策略抽象(SSOT: spec §E Sub-3,2026-09-03)。

把 BrowserRecordStrategy 里的硬编码探针链(head_request → cc_get →
referer_check → cookie_warmup)换成 Protocol + Registry。

新增平台时:写一个 ProbeStrategy 实现 + register(),不动 BrowserRecordStrategy。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ProbeContext:
    """共享给所有探针的资源。session/page 至少有一个非 None。"""

    session: Any  # requests.Session 或 None
    page: Any  # playwright.Page 或 None
    cfg: Any  # VLAConfig 或其子集


@dataclass
class ProbeResult:
    ok: bool
    note: str = ""
    extra: dict = field(default_factory=dict)


@runtime_checkable
class ProbeStrategy(Protocol):
    """一个探针 = 决定是否对当前 url 生效 + 跑一次副作用检测。"""

    name: str

    def match(self, url: str) -> bool: ...

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult: ...


class ProbeRegistry:
    """按注册顺序迭代;BrowserRecordStrategy 用这个代替硬编码 if/elif 链。"""

    def __init__(self) -> None:
        self._strategies: list[ProbeStrategy] = []

    def register(self, strategy: ProbeStrategy) -> None:
        self._strategies.append(strategy)

    def get_all_for(self, url: str) -> list[ProbeStrategy]:
        return [s for s in self._strategies if s.match(url)]
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_probe_strategy.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/vla/subtitle/probe_strategy.py tests/test_probe_strategy.py
git commit -m "feat(probe): ProbeStrategy Protocol + ProbeRegistry"
```

---

### Task 3: Add concrete probes (`HeadRequestProbe`, `RefererCheckProbe`, `CookieWarmupProbe`)

**Files:**
- Create: `src/vla/subtitle/probes/__init__.py` (re-exports)
- Create: `src/vla/subtitle/probes/head_request.py`
- Create: `src/vla/subtitle/probes/referer_check.py`
- Create: `src/vla/subtitle/probes/cookie_warmup.py`

**Interfaces:** Each is a `ProbeStrategy` with sensible `match()` and `run()`.

- [ ] **Step 1: Implement `HeadRequestProbe`**

```python
# src/vla/subtitle/probes/head_request.py
"""HEAD 请求探针:对所有 HTTP(S) url 生效。"""

from __future__ import annotations

from vla.subtitle.probe_strategy import ProbeContext, ProbeResult, ProbeStrategy


class HeadRequestProbe:
    name = "head_request"

    def match(self, url: str) -> bool:
        return url.startswith(("http://", "https://"))

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult:
        if ctx.session is None:
            return ProbeResult(ok=False, note="no session")
        try:
            r = ctx.session.head(url, allow_redirects=True, timeout=5)
        except Exception as e:
            return ProbeResult(ok=False, note=f"exception: {e!r}")
        if r.status_code < 400:
            return ProbeResult(ok=True, note=f"HTTP {r.status_code}")
        return ProbeResult(ok=False, note=f"HTTP {r.status_code}")
```

- [ ] **Step 2: Implement `RefererCheckProbe`**

```python
# src/vla/subtitle/probes/referer_check.py
"""Referer 探针:对已知平台(B 站 / YouTube 等)生效,看返回内容是否包含视频标题关键词。"""

from __future__ import annotations

import re

from vla.subtitle.probe_strategy import ProbeContext, ProbeResult


_PLATFORM_KEYWORDS: dict[str, list[str]] = {
    "bilibili.com": ["bilibili", "视频", "投稿"],
    "youtube.com": ["youtube", "watch"],
    "youtu.be": ["youtube", "watch"],
}


class RefererCheckProbe:
    name = "referer_check"

    def match(self, url: str) -> bool:
        return any(host in url for host in _PLATFORM_KEYWORDS)

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult:
        if ctx.session is None:
            return ProbeResult(ok=False, note="no session")
        host = next((h for h in _PLATFORM_KEYWORDS if h in url), "")
        keywords = _PLATFORM_KEYWORDS.get(host, [])
        try:
            r = ctx.session.get(url, timeout=5, headers={"Referer": f"https://{host}/"})
        except Exception as e:
            return ProbeResult(ok=False, note=f"exception: {e!r}")
        body = r.text[:8192]
        if any(kw in body for kw in keywords):
            return ProbeResult(ok=True, note=f"matched {host} keywords")
        return ProbeResult(ok=False, note=f"no {host} keywords in body")
```

- [ ] **Step 3: Implement `CookieWarmupProbe`**

```python
# src/vla/subtitle/probes/cookie_warmup.py
"""Cookie 预热探针:用浏览器打开首页取一次 cookie,后续请求复用。"""

from __future__ import annotations

from vla.subtitle.probe_strategy import ProbeContext, ProbeResult


class CookieWarmupProbe:
    name = "cookie_warmup"

    def __init__(self, home_urls: dict[str, str] | None = None) -> None:
        self.home_urls = home_urls or {
            "bilibili.com": "https://www.bilibili.com",
            "youtube.com": "https://www.youtube.com",
        }

    def match(self, url: str) -> bool:
        return any(host in url for host in self.home_urls)

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult:
        if ctx.session is None:
            return ProbeResult(ok=False, note="no session")
        host = next((h for h in self.home_urls if h in url), None)
        if not host:
            return ProbeResult(ok=False, note="no home for url")
        try:
            ctx.session.get(self.home_urls[host], timeout=5)
        except Exception as e:
            return ProbeResult(ok=False, note=f"warmup failed: {e!r}")
        return ProbeResult(ok=True, note=f"warmed {host}")
```

- [ ] **Step 4: Add `__init__.py` re-exports**

```python
# src/vla/subtitle/probes/__init__.py
from vla.subtitle.probes.cookie_warmup import CookieWarmupProbe
from vla.subtitle.probes.head_request import HeadRequestProbe
from vla.subtitle.probes.referer_check import RefererCheckProbe

__all__ = [
    "CookieWarmupProbe",
    "HeadRequestProbe",
    "RefererCheckProbe",
]
```

- [ ] **Step 5: Commit**

```bash
git add src/vla/subtitle/probes/ tests/test_probe_strategy.py
git commit -m "feat(probe): default probes (head/referer/cookie)"
```

---

### Task 4: Wire `BrowserRecordStrategy` to registry

**Files:**
- Modify: `src/vla/subtitle/browser_record.py`
- Modify: `src/vla/main_provider.py` (or wherever `BrowserRecordStrategy` is constructed)

- [ ] **Step 1: Replace inline probe ladder with registry iteration**

Find the `probe(url)` method (or equivalent) in `BrowserRecordStrategy`. Replace its body:

```python
def probe(self, url: str) -> bool:
    """跑 registry 里所有匹配的探针,直到任意一个 ok 或全部失败。"""
    from vla.subtitle.probe_strategy import ProbeContext
    ctx = ProbeContext(session=self.session, page=self.page, cfg=self.cfg)
    for strat in self.probe_registry.get_all_for(url):
        result = strat.run(url, ctx)
        self.probe_log.append({"strategy": strat.name, "ok": result.ok, "note": result.note})
        if result.ok:
            return True
    return False
```

- [ ] **Step 2: Construct with default registry**

Where `BrowserRecordStrategy` is built (likely in `main_provider.py`):

```python
from vla.subtitle.probe_strategy import ProbeRegistry
from vla.subtitle.probes import CookieWarmupProbe, HeadRequestProbe, RefererCheckProbe

def default_probe_registry() -> ProbeRegistry:
    reg = ProbeRegistry()
    reg.register(HeadRequestProbe())
    reg.register(RefererCheckProbe())
    reg.register(CookieWarmupProbe())
    return reg
```

Pass `probe_registry=default_probe_registry()` to the `BrowserRecordStrategy` constructor.

- [ ] **Step 3: Run browser_record tests**

Run: `uv run pytest tests/test_browser_record.py -v`
Expected: All pass (probe behavior preserved)

- [ ] **Step 4: Commit**

```bash
git add src/vla/subtitle/browser_record.py src/vla/main_provider.py
git commit -m "refactor(browser_record): probe chain via ProbeRegistry"
```

---

### Task 5: Verify

- [ ] **Step 1: Full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 2: `vla doctor`**

Run: `uv run vla doctor`
Expected: All checks pass

- [ ] **Step 3: Audit no inline probe code**

Run: `grep -rn "def head_request\|def _head_request\|def _probe\|session.head" src/vla/subtitle/browser_record.py`
Expected: no inline probe logic in `browser_record.py`
