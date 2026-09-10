import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from vla.main_provider import RealTextProvider
from vla.models import Asset, ProcessResult, QualityResult, VideoTask


def _make_provider():
    p = RealTextProvider.__new__(RealTextProvider)
    p.transcriber = MagicMock()
    p.checker = MagicMock()
    p.refiner = None
    p.log = MagicMock()
    p.plugin_status = MagicMock()
    p.cfg = MagicMock(quality_check=MagicMock(refine_enabled=False))
    return p


def _task():
    return VideoTask(id="BV1xx", title="t", url="https://x", expected_duration=60)


def _qr_pass(score: int = 90) -> QualityResult:
    return QualityResult(score=score, passed=True, issues=[], suggestion="ok", char_count=10)


def _qr_fail(score: int = 30, reason: str = "低质") -> QualityResult:
    return QualityResult(score=score, passed=False, issues=[reason], suggestion="retry", char_count=10)


@pytest.mark.asyncio
async def test_process_asset_text_only_no_transcribe():
    """API/Browser 命中 → 直接用 text,不调 transcribe。"""
    p = _make_provider()
    asset = Asset(text="hello", source="api", audio_path=None)
    p.checker.check = MagicMock(return_value=_qr_pass(95))
    result = await p.process_asset(asset, _task())
    assert result is not None
    assert result.text == "hello"
    assert result.source == "api"
    p.transcriber.transcribe.assert_not_called()


@pytest.mark.asyncio
async def test_process_asset_wav_transcribe_passes(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="transcribed text")
    p.checker.check = MagicMock(return_value=_qr_pass(90))
    result = await p.process_asset(asset, _task())
    assert result is not None
    assert result.text == "transcribed text"
    p.transcriber.transcribe.assert_called_once_with(wav)
    # 质量 pass → unlink audio
    assert not wav.exists()


@pytest.mark.asyncio
async def test_process_asset_wav_quality_fail_still_deletes_audio(tmp_path):
    """2026-09-10 FR-3.7 修正:转写成功即删音频 —— 质量失败也不留。

    旧策略「质量失败 → 保留 .wav 供重转写」已作废。新理由:重跑 refine /
    summary 只需**文本**(transcript.txt 已落盘,Refiner 吃的是文本),
    只有重**转写**才需要 wav,而 bill-jc 走 m3u8 直抽 + 30 分钟上限,
    重抽成本有界。
    """
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="bad text")
    p.checker.check = MagicMock(return_value=_qr_fail(30, "低质"))
    result = await p.process_asset(asset, _task())
    assert result is None
    p.log.log_quality_fail.assert_called_once()
    # 转写已成功 → 音频已删,质量失败也留不下
    assert not wav.exists()


@pytest.mark.asyncio
async def test_process_asset_deletes_audio_before_quality_gate(tmp_path):
    """删除时机在质量门控**之前** —— 门控判 fail 时 wav 已经不在了。"""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="text")
    seen: list[bool] = []
    p.checker.check = MagicMock(side_effect=lambda **kw: (
        seen.append(wav.exists()), _qr_fail(30, "低质")
    )[1])

    await p.process_asset(asset, _task())

    assert seen == [False], "质量门控被调用时 wav 应已删除"


@pytest.mark.asyncio
async def test_process_asset_not_deletable_keeps_audio(tmp_path):
    """deletable=False 的 asset(字幕命中,无 wav)不受影响。"""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=False)
    p.transcriber.transcribe = MagicMock(return_value="text")
    p.checker.check = MagicMock(return_value=_qr_pass(90))

    await p.process_asset(asset, _task())

    assert wav.exists(), "deletable=False 时不该删"


@pytest.mark.asyncio
async def test_process_asset_transcribe_fails(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(side_effect=RuntimeError("whisper crashed"))
    result = await p.process_asset(asset, _task())
    assert result is None
    p.log.log_transcribe_fail.assert_called_once()
    # 转写失败 → audio 也不删
    assert wav.exists()


@pytest.mark.asyncio
async def test_process_asset_scan_touches_sidecar(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_scan", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="scanned text")
    p.checker.check = MagicMock(return_value=_qr_pass(90))
    await p.process_asset(asset, _task())
    # sidecar 应被 touch
    sidecar = wav.with_suffix(".transcribed.txt")
    assert sidecar.exists()


@pytest.mark.asyncio
async def test_process_asset_browser_quality_fail_marks_plugin_unavailable(tmp_path):
    p = _make_provider()
    asset = Asset(text="browser text", source="browser", audio_path=None)
    p.checker.check = MagicMock(return_value=_qr_fail(30, "bad"))
    result = await p.process_asset(asset, _task())
    assert result is None
    p.plugin_status.mark_unavailable.assert_called_once()


@pytest.mark.asyncio
async def test_process_asset_refine_runs_when_enabled(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    p.cfg.quality_check.refine_enabled = True
    p.refiner = MagicMock()
    from vla.models import RefinementResult, Correction
    p.refiner.refine = MagicMock(return_value=RefinementResult(
        original_text="original",
        cleaned_text="refined text",
        corrections=[Correction(original="x", fixed="y", reason="r")],
        model="claude-sonnet-4-5",
    ))
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="original")
    p.checker.check = MagicMock(return_value=_qr_pass(90))
    result = await p.process_asset(asset, _task())
    assert result.text == "refined text"
    p.refiner.refine.assert_called_once()


@pytest.mark.asyncio
async def test_process_asset_refine_fails_uses_original(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    p.cfg.quality_check.refine_enabled = True
    p.refiner = MagicMock()
    p.refiner.refine = MagicMock(side_effect=RuntimeError("refine crashed"))
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="original")
    p.checker.check = MagicMock(return_value=_qr_pass(90))
    result = await p.process_asset(asset, _task())
    assert result.text == "original"  # refine 失败 → 用原文