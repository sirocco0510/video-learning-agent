"""视频源工厂(SSOT: requirements.md FR-1 / FR-8 + implementation-plan.md Phase 2)。

FR-1:yt-dlp simulate 检测 → 下载 or 录屏
FR-8:ffmpeg + avfoundation 录屏,系统音频,libx264 ultrafast CRF=28

⚠️ Phase 2 边界:
- ffmpeg 进程的清理(proc.wait() / proc.kill())归主调度器(Phase 8)负责
- _record_screen 只负责启动 Popen 并返回目标路径
"""

import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import VLAConfig
from ..log.transcription_log import TranscriptionLog
from ..models import VideoSource

if TYPE_CHECKING:
    pass


class DownloadError(Exception):
    """yt-dlp 下载失败。"""


class VideoSourceFactory:
    def __init__(
        self,
        tmp_dir: Path,
        log: TranscriptionLog,
        config: VLAConfig | None = None,
    ) -> None:
        self.tmp_dir = tmp_dir
        self.log = log
        # 隐式从项目默认 config/vla.yaml 加载;Phase 8 主调度会显式传 config
        self.config = config or VLAConfig.from_yaml("./config/vla.yaml")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # ---- 检测 ----

    def _is_downloadable(self, url: str) -> bool:
        """调 yt-dlp --simulate;返回码 0 即视为可下载;超时 → False。"""
        try:
            r = subprocess.run(
                ["yt-dlp", "--simulate", "--quiet", url],
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0

    # ---- 下载 ----

    def _download(self, url: str, video_id: str) -> Path:
        """yt-dlp -f <format> -o <tmp>/<id>.mp4 <url>;失败抛 DownloadError。"""
        output_path = self.tmp_dir / f"{video_id}.mp4"
        r = subprocess.run(
            [
                "yt-dlp",
                "-f", self.config.video_source.download.format,
                "-o", str(output_path),
                url,
            ],
            capture_output=True,
            timeout=600,
        )
        if r.returncode != 0 or not output_path.exists():
            raise DownloadError(
                f"yt-dlp failed for {url} (returncode={r.returncode})"
            )
        return output_path

    # ---- 录屏 ----

    def _record_screen(self, url: str, video_id: str, duration_sec: int) -> Path:
        """打开浏览器 → 等加载 → 异步启动 ffmpeg avfoundation → 返回路径。

        进程清理(proc.kill() / proc.wait())归主调度器(Phase 8)。
        """
        output_path = self.tmp_dir / f"{video_id}.mp4"

        # 1. 打开浏览器(非阻塞)
        subprocess.run(["open", url], check=False)

        # 2. 等浏览器加载
        time.sleep(5)

        # 3. 异步启动 ffmpeg
        record_cfg = self.config.video_source.record
        # audio_input 兼容 "screen:audio" 旧格式 与 "audio" 新格式;只取最后一段
        audio_idx = record_cfg.audio_input.split(":")[-1]
        cmd = [
            "ffmpeg", "-y",
            "-f", "avfoundation",
            "-framerate", str(record_cfg.fps),
            "-i", f"{record_cfg.screen_index}:{audio_idx}",
            "-t", str(duration_sec),
            "-c:v", "libx264",
            "-preset", record_cfg.preset,
            "-crf", str(record_cfg.crf),
            "-c:a", "aac",
            "-b:a", "128k",
            str(output_path),
        ]
        subprocess.Popen(cmd)
        return output_path

    # ---- 编排 ----

    def get(self, url: str, video_id: str, expected_duration: int) -> VideoSource:
        """调度:_is_downloadable → _download or _record_screen。"""
        if self._is_downloadable(url):
            path = self._download(url, video_id)
            return VideoSource(
                path=path, mode="download", duration_sec=float(expected_duration)
            )
        path = self._record_screen(url, video_id, expected_duration)
        return VideoSource(
            path=path, mode="record", duration_sec=float(expected_duration)
        )
