"""macOS 系统通知(SSOT: requirements.md 第六章 + FR-2.5/2.6)。

策略:
- 全部走 osascript(B 级 `display notification` / A 级 `display dialog`)
- 移除 terminal-notifier 路径(3.0+ 移除 -sender + 权限问题多)
- A 级 dialog 支持 `giving up after N` 超时(30s 等插件开启 / 60s 等录屏完成确认)
- `enabled=False` 时静默:info/warning 不发,alert / ask_* 返回默认按钮

⚠️ 首次运行需授权:
  - osascript(系统级):系统设置 → 通知 → 允许终端 / osascript
  - dialog:系统设置 → 辅助功能
"""

from __future__ import annotations

import logging
import subprocess


logger = logging.getLogger(__name__)


# 两个 timeout 阶段的常量
BROWSER_PLUGIN_TIMEOUT_SEC = 30   # FR-2.5/2.6 等用户开启浏览器插件
RECORDING_DONE_TIMEOUT_SEC = 60   # 录屏完成后等用户确认下载


class MacOSNotifier:
    """B 级通知 + A 级弹窗。

    enabled=False 时:
    - B 级 info/warning 静默
    - A 级 alert / ask_open_browser / ask_recording_done 返回默认按钮
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    # ---------------- B 级非阻塞通知 ----------------

    def info(self, title: str, message: str) -> None:
        """B 级非阻塞通知:osascript display notification(banner 时长由 macOS 控制)。"""
        if not self.enabled:
            return
        script = (
            f'display notification "{_escape(message)}" '
            f'with title "{_escape(title)}"'
        )
        _run_osascript(script)

    def warning(self, title: str, message: str) -> None:
        """B 级非阻塞警告通知(同 info,语义区分)。"""
        self.info(title, message)

    # ---------------- A 级阻塞弹窗(无超时) ----------------

    def alert(
        self,
        title: str,
        message: str,
        buttons: tuple[str, ...] = ("OK",),
    ) -> str:
        """A 级阻塞弹窗:display dialog;返回用户点的按钮。

        osascript 输出:`button returned:<按钮名>`。
        """
        if not self.enabled:
            return buttons[0]
        btns_str = ", ".join(f'"{_escape(b)}"' for b in buttons)
        script = (
            f'display alert "{_escape(title)}" '
            f'message "{_escape(message)}" '
            f'buttons {{{btns_str}}} '
            f'default button "{_escape(buttons[0])}"'
        )
        result = _run_osascript(script, capture=True)
        if result is None:
            return buttons[0]
        for line in result.splitlines():
            if line.startswith("button returned:"):
                return line.split(":", 1)[1].strip()
        return buttons[0]

    # ---------------- A 级阻塞弹窗(带超时) ----------------

    def ask_open_browser(
        self,
        url: str,
        plugin_name: str,
        timeout_sec: int = BROWSER_PLUGIN_TIMEOUT_SEC,
    ) -> str:
        """弹窗询问用户是否已开启浏览器插件(FR-2.5/2.6/2.21)。

        Args:
            url: 视频 URL(展示给用户方便找到视频)
            plugin_name: 插件名(如 "Screen Recorder")
            timeout_sec: 超时秒数,默认 30s

        Returns:
            "enabled" — 用户点"已开启"(允许继续尝试)
            "skip"    — 用户点"跳过该视频"(标记不可用 + 降级)
            "timeout" — 用户未响应 / osascript 失败(标记不可用 + 降级)
        """
        if not self.enabled:
            return "timeout"  # 测试 / CI 走 timeout 降级路径
        title = f"🔌 {plugin_name} 插件字幕"
        message = (
            f"无官方字幕,启用 {plugin_name}(Cmd+Shift+R)录制浏览器 tab 音频 → Whisper 转写。\n\n"
            f"视频:{url}"
        )
        buttons = ("已开启", "跳过该视频")
        result = _display_dialog_with_timeout(
            title, message, buttons, default_button=buttons[0],
            timeout_sec=timeout_sec,
        )
        if result == "timeout":
            # FR-2.21:超时只 log,不重复 B 级通知(macOS dialog 自身已自动消失,
            # 再发"已降级"通知会和"录屏启动"/"录屏到时"挤在一起,信息噪音)
            logger.info(
                "插件启用 popup 超时(%ds),已自动降级到录屏兜底(ffmpeg)",
                timeout_sec,
            )
            return "timeout"
        if result == buttons[0]:
            return "enabled"
        if result == buttons[1]:
            return "skip"
        # 解析失败 / osascript 异常 → 按 timeout 走(同样只 log)
        logger.warning("插件 dialog 输出解析失败,按 timeout 处理: %r", result)
        return "timeout"

    def ask_recording_done(
        self,
        recording_path: str,
        timeout_sec: int = RECORDING_DONE_TIMEOUT_SEC,
    ) -> str:
        """录屏完成后提醒用户去编辑页点下载按钮(60s 超时)。

        Args:
            recording_path: 录屏文件路径(展示给用户)
            timeout_sec: 超时秒数,默认 60s

        Returns:
            "ok"     — 用户点"我知道了"(继续流程)
            "timeout" — 用户未响应 / osascript 失败
        """
        if not self.enabled:
            return "ok"  # 测试 / CI 走 ok 路径(不让流程卡住)
        title = "🎥 录屏已完成"
        message = (
            f"录屏文件已保存:\n{recording_path}\n\n"
            f"请在浏览器编辑页点 btn-download 下载转写结果。\n\n"
            f"60 秒后自动跳过。"
        )
        buttons = ("我知道了",)
        result = _display_dialog_with_timeout(
            title, message, buttons, default_button=buttons[0],
            timeout_sec=timeout_sec,
        )
        if result == "timeout":
            return "timeout"
        return "ok"


# ---------------- helpers ----------------


def _escape(s: str) -> str:
    """AppleScript 字符串转义:双引号 + 反斜杠。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run_osascript(script: str, capture: bool = False) -> str | None:
    """调 osascript -e script。失败 log warning,不抛。"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=120,  # 单次 osascript 调用的硬上限
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            logger.warning(
                "osascript 失败 rc=%s stderr=%s",
                result.returncode, stderr,
            )
            return None
        return result.stdout if capture else None
    except FileNotFoundError:
        logger.warning("osascript 未安装(macOS 限定)")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("osascript 进程超时")
        return None


def _display_dialog_with_timeout(
    title: str,
    message: str,
    buttons: tuple[str, ...],
    default_button: str,
    timeout_sec: int,
) -> str:
    """调 osascript display dialog 带 `giving up after` 超时。

    Returns:
        用户点的按钮名 / "timeout"(gave up:true 或 osascript 失败)
    """
    btns_str = ", ".join(f'"{_escape(b)}"' for b in buttons)
    script = (
        f'display dialog "{_escape(message)}" '
        f'with title "{_escape(title)}" '
        f'buttons {{{btns_str}}} '
        f'default button "{_escape(default_button)}" '
        f'giving up after {timeout_sec}'
    )
    result = _run_osascript(script, capture=True)
    if result is None:
        return "timeout"
    # 解析:`button returned:X\ngave up:false` 或 `button returned:X\ngave up:true`
    gave_up = False
    button: str | None = None
    for line in result.splitlines():
        if line.startswith("button returned:"):
            button = line.split(":", 1)[1].strip()
        elif line.startswith("gave up:"):
            gave_up = line.split(":", 1)[1].strip().lower() == "true"
    if gave_up:
        logger.info("⏱️ dialog 超时未响应(%ds):%s", timeout_sec, title)
        return "timeout"
    if button is None:
        logger.warning("osascript 输出无法解析 button:%r", result)
        return "timeout"
    return button