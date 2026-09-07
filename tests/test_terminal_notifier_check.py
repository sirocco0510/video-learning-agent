"""`_check_terminal_notifier` 测试(SSOT: requirements.md FR-2.28.2h)。

`vla doctor` 集成检测 terminal-notifier(可点通知 CLI)的可用性。
- 在 PATH → (True, msg 含路径)
- 不在 → (False, msg 含 brew install 提示,WARN-only)
"""
from __future__ import annotations

import pytest


def test_terminal_notifier_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """terminal-notifier 在 PATH → (True, msg 含路径)。"""
    import vla.cli as mod

    monkeypatch.setattr(
        mod.shutil, "which",
        lambda x: "/opt/homebrew/bin/terminal-notifier" if x == "terminal-notifier" else None,
    )
    from vla.cli import _check_terminal_notifier
    ok, msg = _check_terminal_notifier()
    assert ok is True
    assert "/opt/homebrew/bin/terminal-notifier" in msg
    assert "Finder" in msg or "可点" in msg


def test_terminal_notifier_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """terminal-notifier 不在 PATH → (False, msg 含安装提示)。"""
    import vla.cli as mod

    monkeypatch.setattr(mod.shutil, "which", lambda x: None)
    from vla.cli import _check_terminal_notifier
    ok, msg = _check_terminal_notifier()
    assert ok is False
    assert "未安装" in msg
    assert "brew install" in msg
