"""macOS 系统通知(SSOT: requirements.md 第六章 6.1 ui/macos_notify.py)。

⚠️ 本文件是 **Phase 3 stub**,只暴露接口 + 保守默认值。
完整实现(osascript display dialog / display notification)在 Phase 6 落地。

Phase 3 扩展:ask_open_browser 返回 "opened" / "skip" / "timeout" 三态。
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

    def ask_open_browser(
        self,
        title: str,
        url: str,
        timeout_sec: int = 30,
    ) -> str:
        """A 级弹窗,询问用户是否启用浏览器插件(FR-2.9 一次启动)。

        Phase 3 stub 默认返回 "timeout" → 策略 ② 降级,标记 plugin_status=unavailable。
        Phase 6 用 osascript 实现异步 + timeout 杀进程。

        Returns:
            "opened":  用户点击"已开启"
            "skip":    用户点击"跳过该视频"
            "timeout": 弹窗超时未响应
        """
        return "timeout"
