"""ProbeStrategy Protocol + ProbeRegistry 测试(SSOT: docs/superpowers/plans/R-14-probe-strategy.md)。

覆盖:
- TestRegistryBasics(2):register+list / empty registry
- TestRegistryOrder(1):注册顺序保留
- TestProtocolConformance(1):Protocol 结构性 typing
- TestRunChain(1):chain stops on first ok
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from vla.subtitle.probe_strategy import (
    ProbeContext,
    ProbeRegistry,
    ProbeResult,
    ProbeStrategy,
)


# ---------------- Fake probes ----------------


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


# ---------------- Test classes ----------------


class TestRegistryBasics:
    def test_register_and_list_all_for_url(self):
        reg = ProbeRegistry()
        reg.register(FakeProbeOk())
        reg.register(FakeProbeSkip())
        # match=True 的留下,match=False 过滤掉
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
        # 注册顺序 = 链顺序(浏览器录屏策略的默认遍历序)
        assert [s.name for s in reg.get_all_for("https://x")] == ["a", "b"]


class TestProtocolConformance:
    def test_protocol_used_as_type_hint(self):
        """Protocol 是结构化 typing:duck typing 通过 isinstance 检查即可。

        FakeProbeOk 定义了 name / match / run,所以运行时也认它是 ProbeStrategy。
        """
        reg = ProbeRegistry()
        reg.register(FakeProbeOk())
        assert len(reg.get_all_for("https://x")) == 1

    def test_isinstance_check_for_protocol(self):
        """runtime_checkable:可以 isinstance(obj, ProbeStrategy) 判结构合规。"""
        ok = FakeProbeOk()
        assert isinstance(ok, ProbeStrategy)


class TestRunChain:
    """模拟 BrowserRecordStrategy 怎么用 registry 跑链。"""

    def test_chain_stops_on_first_ok(self):
        reg = ProbeRegistry()
        reg.register(FakeProbeFail())
        reg.register(FakeProbeOk())
        reg.register(FakeProbeFail())
        results = []
        ctx = ProbeContext(session=MagicMock(), page=MagicMock(), cfg=MagicMock())
        for strat in reg.get_all_for("https://x"):
            r = strat.run("https://x", ctx)
            results.append(r)
            if r.ok:
                break
        # 链在第二个 ok 处停下 — 只跑 2 个(fail → ok),不再跑第三个 fail
        assert len(results) == 2
        assert results[-1].ok is True


# ---------------- ProbeContext / ProbeResult shape ----------------


class TestProbeShapes:
    def test_probe_result_defaults(self):
        r = ProbeResult(ok=True)
        assert r.ok is True
        assert r.note == ""
        assert r.extra == {}

    def test_probe_context_fields(self):
        ctx = ProbeContext(session="s", page="p", cfg="c")
        assert ctx.session == "s"
        assert ctx.page == "p"
        assert ctx.cfg == "c"
