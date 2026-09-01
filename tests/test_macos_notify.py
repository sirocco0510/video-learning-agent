"""MacOSNotifier 测试。

Phase 3 stub:仅暴露接口 + 保守默认值;ask_open_browser 默认 'timeout'(降级到 ③)。
Phase 6 按 requirements.md 6.1 ui/macos_notify.py 完整实现 osascript / display notification。

ask_open_browser 返回值约定(Phase 3 扩展):
  "opened"   用户点击"已开启"
  "skip"     用户点击"跳过该视频"
  "timeout"  超时未响应
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


def test_ask_open_browser_default_is_timeout():
    """stub 阶段默认 timeout → 降级到 ③(最安全默认值)。"""
    n = MacOSNotifier()
    assert n.ask_open_browser("启用插件", "https://example.com", timeout_sec=30) == "timeout"
