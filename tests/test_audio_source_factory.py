"""AudioSourceFactory 测试(SSOT: spec 2026-09-03-fr2-fr3 §3.2)。

FR-2.14 path ①: yt-dlp -x --audio-format wav 下载可下载 URL 的音频。
- is_downloadable → yt-dlp --simulate
- extract → yt-dlp -x --audio-format wav → <save_dir>/<stem>.wav
- 失败 → 抛 subprocess.CalledProcessError
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vla.audio.source_factory import AudioExtractionResult, AudioSourceFactory


class TestIsDownloadable:
    def test_returns_true_when_simulate_succeeds(self, tmp_path: Path) -> None:
        """yt-dlp --simulate 返回 0 → True。"""
        factory = AudioSourceFactory(save_dir=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert factory.is_downloadable("https://example.com/watch?v=abc") is True
            mock_run.assert_called_once()
            args = mock_run.call_args.args[0]
            assert "yt-dlp" in args
            assert "--simulate" in args

    def test_returns_false_when_simulate_fails(self, tmp_path: Path) -> None:
        """yt-dlp --simulate 返回非 0 → False(不抛)。"""
        factory = AudioSourceFactory(save_dir=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert factory.is_downloadable("https://example.com/private") is False


class TestExtract:
    def test_creates_wav_file_in_save_dir(self, tmp_path: Path) -> None:
        """extract 后 <save_dir>/<stem>.wav 存在,返回 AudioExtractionResult。"""
        factory = AudioSourceFactory(save_dir=tmp_path)

        def fake_run(cmd, **kwargs):
            # 找到 yt-dlp 命令的 -o 参数对应的输出路径,创建空文件
            out_idx = cmd.index("-o")
            out_template = cmd[out_idx + 1]
            out_path = Path(out_template.replace("%(ext)s", "wav"))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"RIFF....")
            # ffprobe 用于 duration
            if "ffprobe" in cmd[0] or "ffprobe" in str(cmd):
                r = subprocess.CompletedProcess(cmd, 0, stdout=b"120.5", stderr=b"")
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with patch("subprocess.run", side_effect=fake_run):
            result = factory.extract("https://www.bilibili.com/video/BV1abc", "BV1abc")

        assert isinstance(result, AudioExtractionResult)
        assert result.source == "yt-dlp"
        assert result.audio_path == tmp_path / "BV1abc.wav"
        assert result.audio_path.exists()
        assert result.duration_sec == 120

    def test_raises_called_process_error_on_failure(self, tmp_path: Path) -> None:
        """yt-dlp -x 返回非 0 → 抛 subprocess.CalledProcessError(不静默吞)。"""
        factory = AudioSourceFactory(save_dir=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, ["yt-dlp"], stderr=b"404")
            with pytest.raises(subprocess.CalledProcessError):
                factory.extract("https://www.bilibili.com/video/BV_missing", "BV_missing")
