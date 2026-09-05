"""MacOSNotifier 测试(SSOT: requirements.md 第六章 + FR-2.5/2.6)。

实现走 osascript(display notification / display dialog);
测试时用 enabled=False 跳过实际弹窗,只验证 API 契约 + escape + 异常处理。
"""

import subprocess

from vla.ui.macos_notify import (
    BROWSER_PLUGIN_TIMEOUT_SEC,
    MacOSNotifier,
    RECORDING_DONE_TIMEOUT_SEC,
    _escape,
)


# ---------------- enabled=False(测试静默) ----------------


def test_info_noop_when_disabled():
    """enabled=False → info 不发任何东西,也不抛。"""
    n = MacOSNotifier(enabled=False)
    n.info("title", "msg")  # 不抛


def test_warning_noop_when_disabled():
    n = MacOSNotifier(enabled=False)
    n.warning("title", "msg")


def test_alert_returns_first_button_when_disabled():
    """enabled=False → alert 直接返回 buttons[0],不阻塞。"""
    n = MacOSNotifier(enabled=False)
    assert n.alert("title", "msg") == "OK"
    assert n.alert("title", "msg", buttons=("Yes", "No")) == "Yes"


def test_ask_open_browser_returns_timeout_when_disabled():
    """enabled=False → ask_open_browser 直接返回 'timeout'(让流程降级)。"""
    n = MacOSNotifier(enabled=False)
    assert n.ask_open_browser("https://x", "VideoTrans") == "timeout"


def test_ask_recording_done_returns_ok_when_disabled():
    """enabled=False → ask_recording_done 直接返回 'ok'(不阻塞流程)。"""
    n = MacOSNotifier(enabled=False)
    assert n.ask_recording_done("/tmp/v.mp4") == "ok"


# ---------------- escape ----------------


def test_escape_quotes():
    assert _escape('a"b') == 'a\\"b'


def test_escape_backslashes():
    assert _escape("a\\b") == "a\\\\b"


def test_escape_plain_text():
    assert _escape("hello") == "hello"


def test_escape_quotes_and_backslashes():
    assert _escape('a\\b"c') == 'a\\\\b\\"c'


# ---------------- B 级 info/warning → osascript display notification ----------------


def test_info_uses_osascript_display_notification(monkeypatch):
    """info() 调 osascript display notification。"""
    import vla.ui.macos_notify as mod

    calls: list[tuple] = []

    def fake_run(cmd, **kw):
        calls.append(("cmd", list(cmd)))
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    n.info("启动", "请按 hotkey")

    assert calls[0][1][0] == "osascript"
    script = calls[0][1][2]
    assert "display notification" in script
    assert "启动" in script
    assert "请按 hotkey" in script


def test_warning_uses_osascript(monkeypatch):
    import vla.ui.macos_notify as mod

    calls: list[tuple] = []

    def fake_run(cmd, **kw):
        calls.append(("cmd", list(cmd)))
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    n.warning("录屏到时", "请在编辑页点 btn-download")

    assert calls[0][1][0] == "osascript"
    assert "录屏到时" in calls[0][1][2]


def test_info_does_not_crash_when_osascript_missing(monkeypatch):
    """osascript 不在 PATH → log warning 不抛(Caller 不依赖通知可用)。"""
    import vla.ui.macos_notify as mod

    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError),
    )
    n = MacOSNotifier(enabled=True)
    n.info("title", "msg")  # 不抛


def test_warning_does_not_crash_when_osascript_fails(monkeypatch):
    import vla.ui.macos_notify as mod

    def fake_run(*a, **kw):
        class R:
            returncode = 1
            stderr = "Not authorized"
            stdout = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    n.warning("title", "msg")  # 不抛


# ---------------- A 级 alert → display dialog ----------------


def test_alert_returns_default_button_when_osascript_fails(monkeypatch):
    """osascript 失败 → alert 返回 buttons[0](保证调用方拿到合法值)。"""
    import vla.ui.macos_notify as mod

    def fake_run(*a, **kw):
        class R:
            returncode = 1
            stderr = "permission denied"
            stdout = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    assert n.alert("title", "msg", buttons=("Yes", "No")) == "Yes"


def test_alert_parses_button_returned(monkeypatch):
    """osascript 成功 → alert 从 'button returned:X' 解析按钮名。"""
    import vla.ui.macos_notify as mod

    def fake_run(*a, **kw):
        class R:
            returncode = 0
            stderr = ""
            stdout = "button returned:Cancel\n"
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    assert n.alert("title", "msg", buttons=("OK", "Cancel")) == "Cancel"


# ---------------- A 级 ask_open_browser → display dialog with timeout ----------------


def test_ask_open_browser_returns_enabled_when_user_clicks_enabled(monkeypatch):
    """用户点"已开启" → 返回 "enabled"。"""
    import vla.ui.macos_notify as mod

    def fake_run(cmd, **kw):
        # osascript 模拟点击 enabled
        class R:
            returncode = 0
            stderr = ""
            stdout = "button returned:已开启\ngave up:false\n"
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    result = n.ask_open_browser("https://x", "VideoTrans", timeout_sec=30)
    assert result == "enabled"


def test_ask_open_browser_returns_skip_when_user_clicks_skip(monkeypatch):
    """用户点"跳过该视频" → 返回 "skip"。"""
    import vla.ui.macos_notify as mod

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stderr = ""
            stdout = "button returned:跳过该视频\ngave up:false\n"
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    result = n.ask_open_browser("https://x", "VideoTrans")
    assert result == "skip"


def test_ask_open_browser_returns_timeout_when_gave_up(monkeypatch):
    """osascript gave up:true → 返回 "timeout"。"""
    import vla.ui.macos_notify as mod

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stderr = ""
            stdout = "button returned:已开启\ngave up:true\n"
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    result = n.ask_open_browser("https://x", "VideoTrans", timeout_sec=30)
    assert result == "timeout"


def test_ask_open_browser_timeout_does_not_send_warning_notification(monkeypatch):
    """弹窗超时(gave up:true)时**不**发 B 级通知 — 只 log,
    避免和'录屏启动'/'录屏到时'挤在一起产生噪音。"""
    import vla.ui.macos_notify as mod

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stderr = ""
            stdout = "button returned:已开启\ngave up:true\n"
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    warning_called: list[tuple[str, str]] = []
    n.warning = lambda title, message: warning_called.append((title, message))  # type: ignore[method-assign]

    result = n.ask_open_browser("https://x", "Screen Recorder", timeout_sec=30)

    assert result == "timeout"
    # 关键断言:warning 不被调用(避免和'录屏启动'/'录屏到时'信息噪音)
    assert warning_called == []


def test_ask_open_browser_returns_timeout_when_osascript_fails(monkeypatch):
    """osascript 失败 → 返回 "timeout"(走降级路径)。"""
    import vla.ui.macos_notify as mod

    def fake_run(*a, **kw):
        class R:
            returncode = 1
            stderr = "permission denied"
            stdout = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    result = n.ask_open_browser("https://x", "VideoTrans")
    assert result == "timeout"


def test_ask_open_browser_uses_giving_up_after(monkeypatch):
    """ask_open_browser 调 osascript 时带 `giving up after` 子句。"""
    import vla.ui.macos_notify as mod

    calls: list[list] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        class R:
            returncode = 0
            stderr = ""
            stdout = "button returned:已开启\ngave up:false\n"
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    n.ask_open_browser("https://x", "VideoTrans", timeout_sec=45)

    script = calls[0][2]
    assert "giving up after 45" in script
    assert "display dialog" in script


def test_ask_open_browser_default_timeout_is_30s():
    """默认 timeout = BROWSER_PLUGIN_TIMEOUT_SEC(30s)。"""
    assert BROWSER_PLUGIN_TIMEOUT_SEC == 30


def test_ask_open_browser_does_not_call_when_disabled(monkeypatch):
    """enabled=False → 不调 osascript,直接返回 timeout。"""
    import vla.ui.macos_notify as mod

    calls: list = []

    def fake_run(*a, **kw):
        calls.append(1)
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=False)
    result = n.ask_open_browser("https://x", "VideoTrans")
    assert result == "timeout"
    assert calls == []  # osascript 没被调


# ---------------- A 级 ask_recording_done ----------------


def test_ask_recording_done_returns_ok_on_click(monkeypatch):
    """用户点"我知道了" → 返回 "ok"。"""
    import vla.ui.macos_notify as mod

    def fake_run(*a, **kw):
        class R:
            returncode = 0
            stderr = ""
            stdout = "button returned:我知道了\ngave up:false\n"
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    result = n.ask_recording_done("/tmp/v.mp4")
    assert result == "ok"


def test_ask_recording_done_returns_timeout_on_gave_up(monkeypatch):
    """超时未响应 → 返回 "timeout"。"""
    import vla.ui.macos_notify as mod

    def fake_run(*a, **kw):
        class R:
            returncode = 0
            stderr = ""
            stdout = "button returned:我知道了\ngave up:true\n"
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    result = n.ask_recording_done("/tmp/v.mp4", timeout_sec=60)
    assert result == "timeout"


def test_ask_recording_done_default_timeout_is_60s():
    """默认 timeout = RECORDING_DONE_TIMEOUT_SEC(60s)。"""
    assert RECORDING_DONE_TIMEOUT_SEC == 60


def test_ask_recording_done_uses_giving_up_after(monkeypatch):
    """ask_recording_done 调 osascript 时带 `giving up after 60`。"""
    import vla.ui.macos_notify as mod

    calls: list[list] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        class R:
            returncode = 0
            stderr = ""
            stdout = "button returned:我知道了\ngave up:false\n"
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    n.ask_recording_done("/tmp/v.mp4")

    script = calls[0][2]
    assert "giving up after 60" in script


def test_ask_recording_done_does_not_call_when_disabled(monkeypatch):
    import vla.ui.macos_notify as mod

    calls: list = []

    def fake_run(*a, **kw):
        calls.append(1)
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=False)
    result = n.ask_recording_done("/tmp/v.mp4")
    assert result == "ok"
    assert calls == []


# ---------------- _display_dialog_with_timeout 直接测 ----------------


def test_display_dialog_returns_timeout_on_subprocess_timeout(monkeypatch):
    """osascript 进程本身超时(不是 dialog 超时)→ 返回 timeout。"""
    import vla.ui.macos_notify as mod

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(["osascript"], 120)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    n = MacOSNotifier(enabled=True)
    result = n.ask_open_browser("https://x", "VideoTrans")
    assert result == "timeout"