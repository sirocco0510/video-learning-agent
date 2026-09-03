"""VideoSourceFactory 测试(SSOT: requirements.md FR-1 + R-08 决策)。

FR-1:yt-dlp simulate 检测 → 下载
R-08(2026-09-03):删除 _record_screen,get() 失败抛 DownloadError
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from vla.source.video_source import DownloadError, VideoSourceFactory


@pytest.fixture
def factory(tmp_path: Path) -> VideoSourceFactory:
    from vla.log.transcription_log import TranscriptionLog
    log = TranscriptionLog(tmp_path)
    from vla.config import VLAConfig
    cfg = VLAConfig.from_yaml("config/vla.yaml")
    return VideoSourceFactory(tmp_dir=tmp_path, log=log, config=cfg)


class TestDownloadSuccess:
    def test_returns_download_video_source(self, factory: VideoSourceFactory, tmp_path: Path):
        fake_path = tmp_path / "video.mp4"
        with patch.object(factory, "_is_downloadable", return_value=True), \
             patch.object(factory, "_download", return_value=fake_path):
            source = factory.get("https://www.bilibili.com/video/BV1xxx", "BV1xxx", 100)
        assert source.mode == "download"
        assert source.path == fake_path
        assert source.duration_sec == 100.0


class TestDownloadFailure:
    def test_raises_download_error_on_simulate_fail(self, factory: VideoSourceFactory):
        with patch.object(factory, "_is_downloadable", return_value=False):
            with pytest.raises(DownloadError, match="无法下载"):
                factory.get("https://www.bilibili.com/video/BV1xxx", "BV1xxx", 100)

    def test_raises_download_error_on_subprocess_fail(self, factory: VideoSourceFactory):
        with patch.object(factory, "_is_downloadable", return_value=True), \
             patch.object(factory, "_download", side_effect=DownloadError("yt-dlp failed")):
            with pytest.raises(DownloadError, match="yt-dlp failed"):
                factory.get("https://www.bilibili.com/video/BV1xxx", "BV1xxx", 100)


class TestNoScreenRecording:
    def test_no_record_screen_method(self, factory: VideoSourceFactory):
        """Verify _record_screen method has been removed."""
        assert not hasattr(factory, "_record_screen"), "_record_screen should be deleted"
