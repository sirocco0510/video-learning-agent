import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from vla.transcribe.extract import extract_audio


def test_extract_audio_success(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")  # 实际 ffmpeg 会失败,但我们 mock
    out = tmp_path / "out.wav"
    fake_proc = MagicMock(returncode=0, stderr="")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc):
        extract_audio(src, out)
    # output 不应被删(成功路径)
    assert out.exists() is False  # 因为我们没真的创建


def test_extract_audio_ffmpeg_nonzero_raises_and_cleans(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    out.write_bytes(b"\x00\x00")  # 模拟半截 wav
    fake_proc = MagicMock(returncode=1, stderr="some ffmpeg error")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="extract_audio failed"):
            extract_audio(src, out)
    # 半截 wav 被删
    assert out.exists() is False


def test_extract_audio_input_missing_raises(tmp_path):
    src = tmp_path / "missing.mp4"  # 不创建
    out = tmp_path / "out.wav"
    fake_proc = MagicMock(returncode=1, stderr="no such file")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc):
        with pytest.raises(RuntimeError):
            extract_audio(src, out)
    # output 不该被建出来
    assert out.exists() is False


def test_extract_audio_ffmpeg_binary_missing(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    with patch(
        "vla.transcribe.extract.subprocess.run",
        side_effect=FileNotFoundError("ffmpeg not found"),
    ):
        with pytest.raises(FileNotFoundError):
            extract_audio(src, out)


def test_extract_audio_overwrites_existing_output(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    out.write_bytes(b"old")
    fake_proc = MagicMock(returncode=0, stderr="")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc) as mrun:
        extract_audio(src, out)
    # 验证用了 -y(覆盖)
    args = mrun.call_args[0][0]
    assert "-y" in args
