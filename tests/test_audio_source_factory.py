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

from vla.audio.source_factory import (
    AudioExtractionResult,
    AudioSourceFactory,
    probe_duration,
)


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
            # yt-dlp 命令:从 -o 模板取输出路径,创建空 wav
            if "-o" in cmd:
                out_idx = cmd.index("-o")
                out_template = cmd[out_idx + 1]
                out_path = Path(out_template.replace("%(ext)s", "wav"))
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"RIFF....")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            # ffprobe 命令:返回 JSON {format: {duration: 120.5}}
            if "ffprobe" in cmd[0] or "ffprobe" in str(cmd):
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=b'{"format": {"duration": 120.5}}', stderr=b""
                )
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


class TestProbeDuration:
    """模块级 probe_duration(2026-09-10 从 AudioSourceFactory 方法抽出)。

    抽成模块级的理由:learn 批量入口拿到 wav 后要回填真实时长到
    VideoTask.expected_duration(替换 3600 占位),那条路径不经过
    AudioSourceFactory —— 但需要同一份 ffprobe 逻辑与同一套失败语义。
    """

    def test_returns_int_seconds_from_ffprobe(self, tmp_path: Path) -> None:
        """ffprobe 返 JSON duration=120.5 → 截断成 120。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=b'{"format": {"duration": 120.5}}', stderr=b""
            )

        with patch("subprocess.run", side_effect=fake_run):
            assert probe_duration(wav) == 120

    def test_missing_ffprobe_returns_zero(self, tmp_path: Path) -> None:
        """ffprobe 不在 PATH → 0(不抛错,主流程不阻塞)。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")
        with patch("shutil.which", return_value=None):
            assert probe_duration(wav) == 0

    def test_invalid_json_returns_zero(self, tmp_path: Path) -> None:
        """ffprobe 输出不是 JSON → 0。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=b"not json", stderr=b"")

        with patch("subprocess.run", side_effect=fake_run):
            assert probe_duration(wav) == 0

    def test_missing_format_key_returns_zero(self, tmp_path: Path) -> None:
        """JSON 合法但缺 format.duration → 0。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=b'{"streams": []}', stderr=b"")

        with patch("subprocess.run", side_effect=fake_run):
            assert probe_duration(wav) == 0

    def test_timeout_returns_zero(self, tmp_path: Path) -> None:
        """ffprobe 超时 → 0。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["ffprobe"], 10)):
            assert probe_duration(wav) == 0

    def test_nonzero_exit_returns_zero(self, tmp_path: Path) -> None:
        """ffprobe 返回非 0 → 0。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["ffprobe"], stderr=b"boom"),
        ):
            assert probe_duration(wav) == 0

    def test_method_delegates_to_module_function(self, tmp_path: Path) -> None:
        """AudioSourceFactory._probe_duration 仍可用(委托,不再自带副本)。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=b'{"format": {"duration": 42.9}}', stderr=b""
            )

        with patch("subprocess.run", side_effect=fake_run):
            assert AudioSourceFactory(save_dir=tmp_path)._probe_duration(wav) == 42
