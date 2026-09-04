"""Tab Audio Recorder 触发器 (SSOT: spec §3.1, FR-2.21/2.24/2.24a/2.25).

设计要点:
- 无状态:调用方每进程创建一个 TabAudioRecorder(cfg) 实例,不做模块级单例(FR-2.21)。
- 所有方法 async,匹配 playwright 异步 API。
- probe_status 防御性:任何 chrome.management.getAll 异常 → 返回 "not_installed",
  永不向调用方抛错(主流程不中断,FR-2.21 降级语义)。
- 不需要 macOS TCC 屏幕录制权限(Tab Audio Recorder 用 chrome.tabCapture,
  不走 navigator.mediaDevices.getUserMedia)。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

from vla.config import VLAConfig


logger = logging.getLogger(__name__)


# ---- 异常类型 ----


class ExtensionNotFoundError(Exception):
    """Tab Audio Recorder 扩展未在 chrome.management.getAll() 中匹配到。

    _resolve_ext_id 在找不到匹配扩展时抛出,SubtitleStrategy 捕获后
    写 quality_skip.csv(FR-2.21 降级)。
    """


class RecorderTriggerError(Exception):
    """扩展触发录制失败:bg page evaluate 异常 / 跳转 editor.html 超时。

    主调度降级到 quality_skip(FR-2.21),不记 transcribe_fail
    (Whisper 还没启动)。
    """


class DownloadTimeoutError(Exception):
    """click_download 在 timeout_sec 内未收到 download 事件。

    文件留在 audio_failed/,供排查(FR-2.25 + FR-2.22 失败归档)。
    """


# ---- 主类 ----


# 用于从 editor.html URL 提取 audio_id 的正则(FR-2.24 实现细节)
_EDITOR_URL_PATTERN = re.compile(r"editor\.html\?id=(\d+)")


# 探测时执行的 JS:在 background page 里调用 chrome.management.getAll()
# 并把结果 resolve 出来。该 API 是浏览器级,不需要页面焦点。
_PROBE_GET_ALL_JS = """
async () => {
    return new Promise((resolve, reject) => {
        if (typeof chrome === 'undefined' || !chrome.management) {
            reject(new Error('chrome.management unavailable'));
            return;
        }
        chrome.management.getAll((extensions) => resolve(extensions));
    });
}
"""


class TabAudioRecorder:
    """Tab Audio Recorder 触发器 (SSOT: spec §3.1).

    Public methods:
        probe_status: 三态探测 enabled / disabled / not_installed
        start_recording: 触发扩展开始录制,返回 audio_id
        click_download: 在 editor.html 上点下载按钮,落盘到 save_dir

    Internal:
        _resolve_ext_id: 从 chrome.management.getAll() 动态匹配扩展 ID
                         (不硬编码 hanfcigjijjcbdbfoplddndcblmlfiio)
    """

    def __init__(
        self,
        match_keyword: str = "tab audio",
        save_dir: Path = Path("./logs/audio_raw"),
        match_timeout_sec: float = 5.0,
    ) -> None:
        """Args:
            match_keyword: 扩展名/描述的匹配关键词(默认 "tab audio"),
                           可从 cfg.extension.tab_audio_recorder.match_keyword 注入
            save_dir: 音频落盘目录(Path;mkdir 在调用方负责或 click_download 内兜底)
            match_timeout_sec: probe_status 超时阈值(秒);默认 5.0
        """
        self.match_keyword = match_keyword.lower()
        self.save_dir = Path(save_dir)
        self.match_timeout_sec = match_timeout_sec

    # ---- 探测 + 解析扩展 ID ----

    async def probe_status(
        self, browser: Any
    ) -> Literal["enabled", "disabled", "not_installed"]:
        """无状态探测 (FR-2.24a): 每次调用即时查 chrome.management.getAll()。

        Returns:
            "enabled" — 找到扩展且 enabled=True
            "disabled" — 找到扩展但 enabled=False
            "not_installed" — 未找到 / chrome.management 不可用 / 异常 / 超时

        失败(任何异常)→ 防御性返回 "not_installed",永不向调用方抛错。
        """
        try:
            extensions = await asyncio.wait_for(
                browser.evaluate(_PROBE_GET_ALL_JS),
                timeout=self.match_timeout_sec,
            )
        except Exception as e:
            logger.warning("⚠️ probe_status 异常,降级为 not_installed: %s", e)
            return "not_installed"

        if not isinstance(extensions, list):
            return "not_installed"

        for ext in extensions:
            if not isinstance(ext, dict):
                continue
            name = (ext.get("name") or "").lower()
            desc = (ext.get("description") or "").lower()
            if self.match_keyword in name or self.match_keyword in desc:
                return "enabled" if ext.get("enabled") else "disabled"

        return "not_installed"

    async def _resolve_ext_id(self, browser: Any) -> str:
        """从 chrome.management.getAll() 匹配 name/description.contains(match_keyword)。

        找不到匹配的扩展 → raise ExtensionNotFoundError(让上层走 quality_skip)。

        Returns:
            扩展 ID 字符串(Chrome 扩展唯一 ID,32 字符)。
        """
        try:
            extensions = await asyncio.wait_for(
                browser.evaluate(_PROBE_GET_ALL_JS),
                timeout=self.match_timeout_sec,
            )
        except Exception as e:
            raise ExtensionNotFoundError(
                f"chrome.management.getAll 失败: {e}"
            ) from e

        if not isinstance(extensions, list):
            raise ExtensionNotFoundError("chrome.management.getAll 返回非列表")

        for ext in extensions:
            if not isinstance(ext, dict):
                continue
            name = (ext.get("name") or "").lower()
            desc = (ext.get("description") or "").lower()
            if self.match_keyword in name or self.match_keyword in desc:
                ext_id = ext.get("id")
                if isinstance(ext_id, str) and ext_id:
                    return ext_id

        raise ExtensionNotFoundError(
            f"未找到匹配 match_keyword='{self.match_keyword}' 的扩展"
        )
