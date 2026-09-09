from pathlib import Path

import pytest

from vla.models import Asset, ProcessResult, QualityResult, SubtitleResult


def test_subtitle_result_audio_path_default_none():
    r = SubtitleResult(text="hi", source="api")
    assert r.audio_path is None


def test_subtitle_result_audio_path_set():
    p = Path("/tmp/foo.webm")
    r = SubtitleResult(text=None, source="whisper_scan", audio_path=p)
    assert r.audio_path == p


def test_asset_text_only():
    a = Asset(text="hello", source="api", audio_path=None)
    assert a.needs_transcribe is False
    assert a.deletable is False


def test_asset_audio_only_needs_transcribe():
    a = Asset(text=None, source="whisper_download", audio_path=Path("/tmp/a.wav"), deletable=True)
    assert a.needs_transcribe is True
    assert a.source == "whisper_download"


def test_asset_frozen():
    a = Asset(text=None, source="whisper_download", audio_path=Path("/tmp/a.wav"))
    with pytest.raises(Exception):  # FrozenInstanceError
        a.source = "browser"


def test_process_result_fields():
    qr = QualityResult(score=90, passed=True, issues=[], suggestion="", char_count=6)
    r = ProcessResult(text="hello", qr=qr, source="api", duration_sec=60)
    assert r.text == "hello"
    assert r.duration_sec == 60
    assert r.source == "api"
    assert r.qr is qr