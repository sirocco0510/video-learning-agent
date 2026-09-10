"""Notifier 工厂 — 跨平台适配(SSOT: 2026-09-10 Phase 10 轻量化)。

设计:
- macOS(darwin)→ MacOSNotifier(真通知 + 弹窗)
- 其他平台(Windows / Linux / CI)→ NullNotifier(静默 + 默认按钮)

策略 ②(浏览器插件 popup)已被 TabAudioRecorder 删除(2026-09-10 轻量化),
ask_open_browser / ask_recording_done 仍保留接口,但 NullNotifier 永远
返回 "skip" 或默认按钮,避免主流程卡在等用户响应。

为什么不用 NotifierLike Protocol + factory:跨平台导入 platform-specific 类
会导致 Windows 上 `import vla.ui.notifier` 直接失败(macos_notify.py 触发
pyobjc 导入)。**显式 sys.platform 分支 + 条件 import** 是 Windows 兼容的
正确做法。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional, Protocol


class NotifierLike(Protocol):
    """鸭子类型 — 任何满足下列方法签名的对象都接受。"""

    enabled: bool

    def info(self, title: str, message: str, click_path: Optional[Path] = None) -> None: ...
    def warning(self, title: str, message: str) -> None: ...
    def alert(self, title: str, message: str, buttons: tuple[str, ...] = ("OK",)) -> str: ...
    def dismiss_notifications(self, group: str = "vla-screenshot") -> None: ...
    def ask_open_browser(self, url: str, plugin_name: str, timeout_sec: int = 30) -> str: ...
    def ask_recording_done(self, recording_path: str, timeout_sec: int = 60) -> str: ...
    def alert_blocking(
        self,
        title: str,
        message: str,
        detail_button: Any = None,
        detail_action: Any = None,
        timeout_sec: int = 60,
    ) -> None: ...


class NullNotifier:
    """静默 notifier — Windows / Linux / CI 使用。所有方法 no-op 或返回默认。"""

    enabled = False

    def info(self, title: str, message: str, click_path: Optional[Path] = None) -> None:
        return

    def warning(self, title: str, message: str) -> None:
        return

    def alert(self, title: str, message: str, buttons: tuple[str, ...] = ("OK",)) -> str:
        return buttons[0]

    def dismiss_notifications(self, group: str = "vla-screenshot") -> None:
        return

    def ask_open_browser(self, url: str, plugin_name: str, timeout_sec: int = 30) -> str:
        # 2026-09-10 轻量化后无 TabAudioRecorder,Windows 用户不需要响应弹窗,
        # 直接返 "skip" 让 strategy._try_browser 标记 unavailable 跳过策略 ②。
        return "skip"

    def ask_recording_done(self, recording_path: str, timeout_sec: int = 60) -> str:
        return "ok"

    def alert_blocking(
        self,
        title: str,
        message: str,
        detail_button: Any = None,
        detail_action: Any = None,
        timeout_sec: int = 60,
    ) -> None:
        return


def create_notifier(*, enabled: bool | None = None) -> NotifierLike:
    """平台自适应 notifier 工厂。

    Args:
        enabled: None = 平台默认(darwin 启 / 其他禁);False = 强制静默;
                 True = 强制真实(测试用,Windows 上仍返 NullNotifier)

    Returns:
        MacOSNotifier(darwin + 非 False)或 NullNotifier(其他)。
    """
    if enabled is False:
        return NullNotifier()
    if sys.platform == "darwin":
        # 条件 import — macos_notify.py 在 import 时不会触发 pyobjc(只 osascript)
        from vla.ui.macos_notify import MacOSNotifier
        return MacOSNotifier(enabled=enabled if enabled is not None else True)
    return NullNotifier()


__all__ = ["NotifierLike", "NullNotifier", "create_notifier"]