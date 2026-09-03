"""视频源工厂(SSOT: requirements.md FR-1 + R-08 决策)。

FR-1:yt-dlp simulate 检测 → 下载
R-08(2026-09-03 spec §A #5):屏幕录制路径已删除,get() 失败抛 DownloadError
"""

import subprocess
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

    # ---- 编排 ----

    def get(self, url: str, video_id: str, expected_duration: int) -> VideoSource:
        """下载视频源。失败抛 DownloadError(上层 main_provider 不再 fallback 到 ffmpeg)。

        决策(2026-09-03 spec §A #5):
        - 屏幕录制路径已删除(FR-8)
        - 网络/yt-dlp 失败 = 报错退出,不静默走 ffmpeg 录屏
        """
        if not self._is_downloadable(url):
            raise DownloadError(f"无法下载(yt-dlp simulate failed): {url}")
        path = self._download(url, video_id)
        return VideoSource(
            path=path, mode="download", duration_sec=float(expected_duration)
        )
