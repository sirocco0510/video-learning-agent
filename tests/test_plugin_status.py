"""PluginStatus 测试。

Phase 3 stub:仅暴露 get/mark_available/mark_unavailable 三个方法 + 内部状态。
Phase 7.5 按 requirements.md FR-2.10 完整实现(模块级单例 + session 重启重置)。
"""

from vla.state.plugin_status import PluginStatus


def test_initial_state_unknown():
    """新建实例默认 unknown。"""
    ps = PluginStatus()
    assert ps.get() == "unknown"


def test_mark_available():
    """mark_available → get 返回 available。"""
    ps = PluginStatus()
    ps.mark_available()
    assert ps.get() == "available"


def test_mark_unavailable_with_reason():
    """mark_unavailable(reason) → get 返回 unavailable。"""
    ps = PluginStatus()
    ps.mark_unavailable(reason="dialog_timeout")
    assert ps.get() == "unavailable"


def test_mark_unavailable_overrides_available():
    """从 available → unavailable(FR-2.11 插件字幕质量不过关 → 标记 unavailable)。"""
    ps = PluginStatus()
    ps.mark_available()
    ps.mark_unavailable(reason="quality_fail")
    assert ps.get() == "unavailable"


def test_state_attribute_holds_reason():
    """unavailable 状态带 reason。"""
    ps = PluginStatus()
    ps.mark_unavailable(reason="user_skip")
    assert ps.state == "unavailable"
    assert ps.reason == "user_skip"
