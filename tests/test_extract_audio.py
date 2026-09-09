import base64
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vla.transcribe.extract import extract_audio, extract_browser_audio, extract_m3u8_audio


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


# -----------------------------------------------------------------------------
# extract_browser_audio (Phase 9.6.4) — 浏览器内 MediaRecorder 抽音兜底
# -----------------------------------------------------------------------------


def _make_fake_browser(*, ready_state_ok: bool = True, capture_error: str | None = None):
    """构造 fake playwright browser/page，模拟 navigate → record → 返回 b64 payload。

    ready_state_ok=False:wait_for_selector/wait_for_function 抛 TimeoutError。
    capture_error="no_video":page.evaluate 抛 "No <video> element"。
    capture_error="no_audio":page.evaluate 抛 "No audio track"。
    """
    fake_page = MagicMock()
    fake_page.goto = AsyncMock()
    fake_page.wait_for_selector = AsyncMock()
    fake_page.wait_for_function = AsyncMock()
    fake_page.close = AsyncMock()

    if not ready_state_ok:
        fake_page.wait_for_selector = AsyncMock(
            side_effect=Exception("wait_for_selector timeout"),
        )
    elif capture_error == "no_video":
        fake_page.evaluate = AsyncMock(side_effect=Exception("No <video> element"))
    elif capture_error == "no_audio":
        fake_page.evaluate = AsyncMock(side_effect=Exception("No audio track"))
    else:
        # happy path: 返回 b64 payload(0x00 几个字节的 webm 假装)
        fake_bytes = b"\x1a\x45\xdf\xa3"  # EBML header
        payload = {"b64": base64.b64encode(fake_bytes).decode("ascii"), "duration": 12.0}
        fake_page.evaluate = AsyncMock(return_value=payload)

    fake_context = MagicMock()
    fake_context.new_page = AsyncMock(return_value=fake_page)

    fake_browser = MagicMock()
    fake_browser.contexts = [fake_context]
    return fake_browser, fake_page


@pytest.mark.asyncio
async def test_extract_browser_audio_invokes_playwright_and_ffmpeg(tmp_path, monkeypatch):
    """Happy path:fake Playwright 走通 navigate → record → 返回 b64,patch ffmpeg 验证
    wav 落盘 + webm 临时文件被 unlink + page 被 close。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    # Patch async_playwright context manager
    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    captured_ffmpeg_cmds: list = []

    def fake_ffmpeg_run(cmd, **kwargs):
        captured_ffmpeg_cmds.append(cmd)
        # 模拟 ffmpeg 成功:创建 wav 文件
        wav_arg = cmd[cmd.index("-f") + 2]  # "-f", "wav", <path>
        Path(wav_arg).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_ffmpeg_run)
    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio(
            "https://b-learning.bill-jc.com/play?kngId=x", out_wav,
        )

    # 验证 playwright 流程
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp.assert_called_once_with(
        "http://localhost:9222"
    )
    fake_page.goto.assert_called_once()
    fake_page.wait_for_selector.assert_called_once()
    fake_page.wait_for_function.assert_called_once()
    fake_page.evaluate.assert_called_once()
    fake_page.close.assert_called_once()

    # 验证 ffmpeg 被调(必须有 webm 输入 + wav 输出)
    assert len(captured_ffmpeg_cmds) == 1
    cmd = captured_ffmpeg_cmds[0]
    assert cmd[0] == "ffmpeg"
    assert "-vn" in cmd
    assert "-ac" in cmd and "1" in cmd
    assert "-ar" in cmd and "16000" in cmd
    assert "-f" in cmd and "wav" in cmd
    assert str(out_wav) in cmd

    # wav 落盘
    assert out_wav.exists()


@pytest.mark.asyncio
async def test_extract_browser_audio_returns_runtimeerror_when_no_video_element(tmp_path, monkeypatch):
    """page.wait_for_selector 超时 → RuntimeError("browser audio capture failed")。"""
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser(ready_state_ok=False)

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MagicMock(returncode=0, stderr=""))

    with patch("vla.transcribe.extract.async_playwright", ap):
        with pytest.raises(RuntimeError, match="browser audio capture failed"):
            await extract_browser_audio("https://x.com/play", out_wav)
    # page 仍要关(finally)
    fake_page.close.assert_called_once()


@pytest.mark.asyncio
async def test_extract_browser_audio_returns_runtimeerror_when_capture_stream_empty(tmp_path, monkeypatch):
    """audioTracks=[] → RuntimeError 包含 'No audio track'。"""
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser(capture_error="no_audio")

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MagicMock(returncode=0, stderr=""))

    with patch("vla.transcribe.extract.async_playwright", ap):
        with pytest.raises(RuntimeError, match="No audio track"):
            await extract_browser_audio("https://x.com/play", out_wav)


@pytest.mark.asyncio
async def test_extract_browser_audio_passes_playback_rate_to_js(tmp_path, monkeypatch):
    """验证 playback_rate=4 默认值 + 自定义值都正确传给 page.evaluate。

    3h 视频 @ 4x → 45min wall-clock。MediaRecorder 拿原始采样率音频,
    faster-whisper 转写对采样率不敏感、对播放速率不敏感。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    def fake_ffmpeg_run(cmd, **kwargs):
        wav_arg = cmd[cmd.index("-f") + 2]
        Path(wav_arg).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_ffmpeg_run)

    # 默认值
    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio("https://x.com/play", out_wav)
    args = fake_page.evaluate.call_args
    payload = args[0][1]  # (_BROWSER_CAPTURE_JS, payload)
    assert payload["playbackRate"] == 4.0
    assert payload["maxDurationSec"] == 3600

    # 自定义值
    fake_page.evaluate.reset_mock()
    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio(
            "https://x.com/play", out_wav, max_duration_sec=7200, playback_rate=2.0,
        )
    payload = fake_page.evaluate.call_args[0][1]
    assert payload["playbackRate"] == 2.0
    assert payload["maxDurationSec"] == 7200
