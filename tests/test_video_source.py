"""VideoSourceFactory 测试(SSOT: requirements.md FR-1 / FR-8 + implementation-plan.md Phase 2)。

FR-1:yt-dlp simulate 检测 → 下载 or 录屏
FR-8:ffmpeg + avfoundation 录屏,系统音频,libx264 ultrafast CRF=28
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vla.log.transcription_log import TranscriptionLog
from vla.source.video_source import DownloadError, VideoSourceFactory


@pytest.fixture
def log(tmp_path: Path) -> TranscriptionLog:
    return TranscriptionLog(tmp_path / "logs")


@pytest.fixture
def factory(tmp_path: Path, log: TranscriptionLog) -> VideoSourceFactory:
    return VideoSourceFactory(tmp_path / "videos", log)


# ---------------- _is_downloadable ----------------


class TestIsDownloadable:
    def test_returns_true_on_zero_returncode(self, factory):
        """yt-dlp --simulate 成功 → True。"""
        fake = MagicMock(returncode=0, stderr=b"", stdout=b"")
        with patch("vla.source.video_source.subprocess.run", return_value=fake):
            assert factory._is_downloadable("https://example.com/v") is True

    def test_returns_false_on_nonzero_returncode(self, factory):
        """yt-dlp --simulate 失败 → False。"""
        fake = MagicMock(returncode=1, stderr=b"error", stdout=b"")
        with patch("vla.source.video_source.subprocess.run", return_value=fake):
            assert factory._is_downloadable("https://example.com/v") is False

    def test_returns_false_on_timeout(self, factory):
        """yt-dlp --simulate 超时 → False(不抛)。"""
        with patch(
            "vla.source.video_source.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=30),
        ):
            assert factory._is_downloadable("https://example.com/v") is False


# ---------------- _download ----------------


class TestDownload:
    def test_returns_path_when_succeeds(self, factory, tmp_path: Path):
        """yt-dlp -f worst 成功,文件存在 → 返回 Path。"""
        output_path = tmp_path / "videos" / "BV1xxx.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake video content")

        fake = MagicMock(returncode=0, stderr=b"", stdout=b"")
        with patch("vla.source.video_source.subprocess.run", return_value=fake):
            result = factory._download("https://example.com/v", "BV1xxx")

        assert result == output_path
        assert result.exists()

    def test_raises_download_error_on_failure(self, factory):
        """yt-dlp 失败 → 抛 DownloadError。"""
        fake = MagicMock(returncode=1, stderr=b"err", stdout=b"")
        with patch("vla.source.video_source.subprocess.run", return_value=fake):
            with pytest.raises(DownloadError):
                factory._download("https://example.com/v", "BV1xxx")


# ---------------- _record_screen ----------------


class TestRecordScreen:
    def test_starts_ffmpeg_async_and_returns_path(self, factory, tmp_path: Path):
        """打开浏览器 + 异步启动 ffmpeg → 返回 Path;Popen 被调用。"""
        output_path = tmp_path / "videos" / "BV1xxx.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # ffmpeg 会创建文件,提前占位让 exists() 为真
        output_path.write_bytes(b"")

        mock_proc = MagicMock()
        with (
            patch(
                "vla.source.video_source.subprocess.run"
            ) as mock_run,
            patch(
                "vla.source.video_source.subprocess.Popen",
                return_value=mock_proc,
            ) as mock_popen,
            patch("vla.source.video_source.time.sleep"),
        ):
            result = factory._record_screen(
                "https://www.bilibili.com/video/BV1xxx",
                "BV1xxx",
                600,
            )

        # open 命令被调用
        assert mock_run.called
        # ffmpeg 异步启动
        assert mock_popen.called
        # popen 用 Popen(非阻塞),不用 run
        popen_args = mock_popen.call_args.args[0]
        assert popen_args[0] == "ffmpeg"
        assert "-f" in popen_args
        assert "avfoundation" in popen_args
        # 返回 path
        assert result == output_path

    def test_kills_proc_on_exception(self, factory, tmp_path: Path):
        """Popen 之后任何异常 → 必须 proc.kill()(归主调度器在 Phase 8 用)。

        Phase 2 不直接验证,但留个反向用例占位:确认 sleep fail 不影响 Popen 已启动的清理语义。
        当前实现 sleep 在 Popen 之前,所以这条主要留给 Phase 8 caller 验证。
        """
        # 不强测:Phase 8 caller 会用 proc.wait() + proc.kill() 处理超时
        # 这里只确认 _record_screen 至少能成功 return path(Popen 不抛就算过)
        pytest.skip("kill 语义归 Phase 8 caller 验证;_record_screen 本身不持有 proc 生命周期")


# ---------------- get 编排 ----------------


class TestGet:
    def test_downloadable_routes_to_download(self, factory, tmp_path: Path):
        """可下载 → _download,VideoSource.mode='download'。"""
        output_path = tmp_path / "videos" / "BV1xxx.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"x")

        fake = MagicMock(returncode=0, stderr=b"", stdout=b"")
        with patch("vla.source.video_source.subprocess.run", return_value=fake):
            src = factory.get("https://example.com/v", "BV1xxx", 600)

        assert src.mode == "download"
        assert src.path == output_path
        assert src.duration_sec == 600.0

    def test_not_downloadable_routes_to_record(self, factory, tmp_path: Path):
        """不可下载 → _record_screen,VideoSource.mode='record'。"""
        output_path = tmp_path / "videos" / "BV1xxx.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"")

        mock_proc = MagicMock()
        # simulate 失败(recordcode != 0)→ 走录屏
        # 然后 _record_screen 还会调一次 subprocess.run(["open", url])→ 第二个 mock
        with (
            patch(
                "vla.source.video_source.subprocess.run",
                side_effect=[
                    MagicMock(returncode=1),  # yt-dlp simulate 失败
                    MagicMock(returncode=0),  # open 命令成功
                ],
            ),
            patch(
                "vla.source.video_source.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch("vla.source.video_source.time.sleep"),
        ):
            src = factory.get(
                "https://www.bilibili.com/video/BV1xxx", "BV1xxx", 600
            )

        assert src.mode == "record"
        assert src.path == output_path
