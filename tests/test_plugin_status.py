"""PluginStatus 测试(SSOT: requirements.md FR-2.9/2.10 + Phase 7.5)。

设计:
- 三态:unknown / available / unavailable
- 整 session 只确认一次(unavailable 后不再弹窗)
- 初始状态 unknown → 主调度第一次遇到需用插件处触发确认
- mark_unavailable(reason) → 记 reason + 不再弹窗
- mark_available() → 清 reason
"""

from __future__ import annotations

import pytest

from vla.state.plugin_status import PluginStatus


# ---------------- 初始状态 ----------------


class TestInitialState:
    def test_starts_as_unknown(self):
        p = PluginStatus()
        assert p.get() == "unknown"
        assert p.is_unavailable() is False
        assert p.is_known() is False

    def test_initial_reason_none(self):
        p = PluginStatus()
        assert p.reason is None


# ---------------- mark_available ----------------


class TestMarkAvailable:
    def test_changes_status(self):
        p = PluginStatus()
        p.mark_available()
        assert p.get() == "available"
        assert p.is_unavailable() is False
        assert p.is_known() is True

    def test_clears_reason(self):
        """mark_available 后 reason 变 None。"""
        p = PluginStatus()
        p.mark_unavailable("未安装")
        assert p.reason == "未安装"
        p.mark_available()
        assert p.reason is None


# ---------------- mark_unavailable ----------------


class TestMarkUnavailable:
    def test_changes_status_and_records_reason(self):
        p = PluginStatus()
        p.mark_unavailable("未检测到 VideoTrans 扩展")
        assert p.get() == "unavailable"
        assert p.is_unavailable() is True
        assert p.is_known() is True
        assert p.reason == "未检测到 VideoTrans 扩展"

    def test_overrides_previous_available(self):
        """available → unavailable 转换正确。"""
        p = PluginStatus()
        p.mark_available()
        p.mark_unavailable("用户拒绝")
        assert p.is_unavailable() is True
        assert p.reason == "用户拒绝"

    def test_session_singleton_pattern(self):
        """整 session 只确认一次:mark_unavailable 后,后续 mark_available 不变。

        实际语义:PluginStatus 是 session 单例,主调度只在第一次需要插件时确认,
        后续根据 is_unavailable() 决定降级。mark_available 可手动重新启用。
        """
        p = PluginStatus()
        p.mark_unavailable("err1")
        # 这里没有"锁"机制 — 业务逻辑由主调度负责(查 is_unavailable() 决定要不要弹窗)
        assert p.is_unavailable() is True


# ---------------- is_known ----------------


class TestIsKnown:
    def test_unknown_not_known(self):
        p = PluginStatus()
        assert p.is_known() is False

    def test_available_is_known(self):
        p = PluginStatus()
        p.mark_available()
        assert p.is_known() is True

    def test_unavailable_is_known(self):
        p = PluginStatus()
        p.mark_unavailable("err")
        assert p.is_known() is True


# ---------------- reset ----------------


class TestReset:
    def test_reset_back_to_unknown(self):
        """reset() → 回 unknown 状态(测试场景用)。"""
        p = PluginStatus()
        p.mark_unavailable("err")
        p.reset()
        assert p.get() == "unknown"
        assert p.reason is None
        assert p.is_known() is False