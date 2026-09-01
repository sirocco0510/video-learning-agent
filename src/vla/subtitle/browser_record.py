"""BrowserRecorder(SSOT: requirements.md FR-2.16/2.17/2.18 + implementation-plan.md Phase 3.2)。

职责:
- 通过 Chrome 扩展 Screen Recorder 录屏(自带抽音,无需 ffmpeg)
- 监听下载事件拿到扩展输出的视频文件
- 委托给 AudioTranscriber 接口(Phase 4 实现 faster-whisper)转写
- 磁盘友好: 转写完删除源视频文件

设计:
- AudioTranscriber Protocol(duck typing)→ Phase 4 WhisperTranscriber 实现
- BrowserRecorder 注入 transcriber,自身不依赖 faster-whisper
- 录屏触发: 按 hotkey 启停(默认 Control+Shift+R,可配)
- 下载等待: 轮询,超时由 config.browser_plugin.record_download_timeout_sec 控制
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from vla.config import VLAConfig


logger = logging.getLogger(__name__)


@runtime_checkable
class AudioTranscriber(Protocol):
    """音频转写接口(duck typing)。

    Phase 4 由 WhisperTranscriber 实现(faster-whisper);现在 FakeTranscriber 用于测试。
    """

    def transcribe(self, audio_path: Path) -> str:
        """接收视频/音频文件路径,返回字幕文本。"""
        ...


class BrowserRecorder:
    """录屏 + 监听下载 + 委托转写。"""

    def __init__(
        self,
        config: VLAConfig,
        transcriber: AudioTranscriber,
        poll_interval_ms: int = 1000,
    ) -> None:
        self.config = config
        self.transcriber = transcriber
        self.hotkey = config.browser_plugin.record_hotkey
        self._timeout_sec = config.browser_plugin.record_download_timeout_sec
        self._poll_interval_ms = poll_interval_ms

    def record_and_transcribe(
        self,
        page: object,
        url: str,
        duration_sec: int,
        save_dir: Path,
    ) -> str:
        """录屏 + 转写,返回字幕文本。

        page: playwright sync Page(需 .keyboard.press / .wait_for_timeout / .context.on)
        url: 当前播放页 URL(供日志;非强制使用)
        duration_sec: 录屏时长
        save_dir: 视频文件落盘目录

        流程:
        1. 注册 download 监听
        2. 按 hotkey 启动录屏
        3. wait duration_sec
        4. 按 hotkey 停止录屏
        5. 轮询等下载(超时由 config.browser_plugin.record_download_timeout_sec 控制)
        6. 委托 transcriber 转写
        7. finally: 删除源视频文件 + 移除监听
        """
        save_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []

        def on_download(d) -> None:
            target = save_dir / d.suggested_filename
            try:
                d.save_as(target)
                downloaded.append(target)
                logger.info("录屏文件已落盘:%s", target)
            except Exception as e:
                logger.error("保存录屏文件失败:%s", e)

        ctx = page.context  # type: ignore[attr-defined]
        ctx.on("download", on_download)

        video_path: Path | None = None
        try:
            # 1. 启动录屏
            page.keyboard.press(self.hotkey)  # type: ignore[attr-defined]
            logger.info("录屏已启动 url=%s duration=%ds", url, duration_sec)

            # 2. 录制时长
            page.wait_for_timeout(duration_sec * 1000)  # type: ignore[attr-defined]

            # 3. 停止录屏
            page.keyboard.press(self.hotkey)  # type: ignore[attr-defined]
            logger.info("录屏已停止,等待扩展输出文件")

            # 4. 轮询下载
            timeout_ms = self._timeout_sec * 1000
            elapsed = 0
            while elapsed < timeout_ms:
                page.wait_for_timeout(self._poll_interval_ms)  # type: ignore[attr-defined]
                elapsed += self._poll_interval_ms
                if downloaded:
                    break

            if not downloaded:
                raise RuntimeError(
                    f"录屏 {self._timeout_sec}s 内未生成文件 url={url}"
                )

            video_path = downloaded[-1]

            # 5. 转写
            text = self.transcriber.transcribe(video_path)
            return text

        finally:
            # 6. 移除监听
            try:
                ctx.remove_listener("download", on_download)
            except Exception:
                pass
            # 7. 磁盘友好: 删除源视频
            if video_path is not None:
                try:
                    video_path.unlink()
                    logger.info("已清理录屏源文件:%s", video_path)
                except OSError as e:
                    logger.warning("清理录屏文件失败:%s %s", video_path, e)