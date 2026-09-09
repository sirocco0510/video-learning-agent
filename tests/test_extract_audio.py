import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from vla.transcribe.extract import extract_audio, extract_m3u8_audio


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


def test_extract_m3u8_audio_success(tmp_path, monkeypatch):
    """extract_m3u8_audio 调 ffmpeg with -vn -ac 1 -ar 16000 -f wav。"""
    out = tmp_path / "audio.wav"
    fake_proc = MagicMock(returncode=0, stderr="")
    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        out.write_bytes(b"RIFF")
        return fake_proc

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_m3u8_audio("https://video.bill-jc.com/foo.m3u8", out)
    # 验证 -vn 在 args 里, audio flags 对, wav 落盘
    assert "-vn" in captured_cmd
    assert "-ac" in captured_cmd and "1" in captured_cmd
    assert "-ar" in captured_cmd and "16000" in captured_cmd
    assert "-f" in captured_cmd and "wav" in captured_cmd
    assert "https://video.bill-jc.com/foo.m3u8" in captured_cmd
    assert str(out) in captured_cmd


def test_extract_m3u8_audio_fails_on_ffmpeg_nonzero(tmp_path, monkeypatch):
    """ffmpeg 返回非 0 → RuntimeError, 半截 wav 清掉。"""
    out = tmp_path / "audio.wav"
    out.write_bytes(b"RIFF")
    fake_proc = MagicMock(returncode=1, stderr="Connection refused")

    def fake_run(cmd, **kwargs):
        return fake_proc

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="extract_m3u8_audio failed"):
        extract_m3u8_audio("https://x.com/bad.m3u8", out)
    # 半截 wav 应被清掉
    assert not out.exists()
