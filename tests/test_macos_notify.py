"""MacOSNotifier 测试。

Phase 3 stub:仅暴露接口 + 保守默认值。
Phase 6 按 requirements.md 6.1 ui/macos_notify.py 完整实现 osascript / display notification。

Phase 3.7 删除 ask_open_browser(策略 ② 弹窗流程已被三级降级取代)。
"""

from vla.ui.macos_notify import MacOSNotifier


def test_info_noop():
    n = MacOSNotifier()
    n.info("title", "msg")  # 不抛


def test_warning_noop():
    n = MacOSNotifier()
    n.warning("title", "msg")


def test_alert_returns_first_button_by_default():
    n = MacOSNotifier()
    assert n.alert("title", "msg") == "OK"


def test_alert_returns_first_button_of_custom_buttons():
    n = MacOSNotifier()
    assert n.alert("title", "msg", buttons=("Yes", "No")) == "Yes"