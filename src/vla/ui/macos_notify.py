"""macOS 系统通知(SSOT: requirements.md 第六章 6.1 ui/macos_notify.py)。

⚠️ 本文件是 **Phase 3 stub**,只暴露接口 + 保守默认值。
完整实现(osascript display dialog / display notification)在 Phase 6 落地。
"""


class MacOSNotifier:
    """Phase 3 stub:接口 + 默认行为。Phase 6 填 osascript / display notification。"""

    def info(self, title: str, message: str) -> None:
        """B 级非阻塞通知。Phase 6 用 display notification。"""
        return None

    def warning(self, title: str, message: str) -> None:
        """B 级非阻塞警告通知。Phase 6 用 display notification。"""
        return None

    def alert(
        self,
        title: str,
        message: str,
        buttons: tuple[str, ...] = ("OK",),
    ) -> str:
        """A 级阻塞弹窗;Phase 6 用 osascript display dialog;返回用户点的按钮。"""
        return buttons[0]