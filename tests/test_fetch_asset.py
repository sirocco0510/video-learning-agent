import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from vla.main_provider import RealTextProvider
from vla.models import Asset, QualityResult, SubtitleResult, VideoSource, VideoTask


def _make_provider(strategy=None, source_factory=None, save_dir=None, today_dir=None):
    p = RealTextProvider.__new__(RealTextProvider)
    p.strategy = strategy or AsyncMock()
    p.transcriber = AsyncMock()
    p.source_factory = source_factory or MagicMock()
    p.checker = MagicMock()
    p.refiner = None
    p.log = MagicMock()
    p.plugin_status = MagicMock()
    p._save_dir = save_dir or Path("/tmp/save")
    p._today_dir = today_dir or Path("/tmp/today")
    p._save_dir.mkdir(parents=True, exist_ok=True)
    p._today_dir.mkdir(parents=True, exist_ok=True)
    return p


def _task(url="https://www.bilibili.com/video/BV1xx"):
    return VideoTask(id="BV1xx", title="t", url=url, expected_duration=60)


@pytest.mark.asyncio
async def test_fetch_asset_api_text_hit():
    p = _make_provider()
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(text="hello", source="api"))
    asset = await p.fetch_asset(_task())
    assert asset.text == "hello"
    assert asset.source == "api"
    assert asset.audio_path is None
    assert asset.deletable is False
    assert asset.needs_transcribe is False


@pytest.mark.asyncio
async def test_fetch_asset_browser_text_hit():
    p = _make_provider()
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(text="hi", source="browser"))
    asset = await p.fetch_asset(_task())
    assert asset.source == "browser"
    assert asset.deletable is False


@pytest.mark.asyncio
async def test_fetch_asset_internal_spider_m3u8_to_wav(tmp_path):
    p = _make_provider(save_dir=tmp_path / "save", today_dir=tmp_path / "today")
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(
        text=None, source="internal_spider",
        metadata={"video_url": "https://video.bill-jc.com/conversion/group1/v1/test.m3u8"},
    ))
    fake_wav = tmp_path / "save" / "audio_raw" / "BV1xx.wav"
    fake_wav.parent.mkdir(parents=True, exist_ok=True)
    with patch("vla.main_provider.extract_m3u8_audio") as mex:
        # 模拟 extract_m3u8_audio 写出 wav
        def fake_extract(url, dst):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"\x00")
        mex.side_effect = fake_extract
        asset = await p.fetch_asset(_task())
    assert asset.source == "whisper_internal_download"
    assert asset.audio_path == fake_wav
    assert asset.deletable is True
    assert asset.needs_transcribe is True


@pytest.mark.asyncio
async def test_fetch_asset_internal_spider_no_video_url_returns_none():
    """internal_spider 返回但无 video_url → 返回 None(spec §4.2 策略明确)。"""
    p = _make_provider()
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(
        text=None, source="internal_spider", metadata={},  # 无 video_url
    ))
    asset = await p.fetch_asset(_task(url="https://b-learning.bill-jc.com/kng/#/video/play?kngId=xxx"))
    assert asset is None


@pytest.mark.asyncio
async def test_fetch_asset_video_source_factory_mp4_to_wav(tmp_path):
    p = _make_provider(save_dir=tmp_path / "save", today_dir=tmp_path / "today")
    p.strategy.get_subtitle = AsyncMock(return_value=None)  # 字幕未命中
    # source_factory 返回 video_path
    mp4 = tmp_path / "BV1xx.mp4"
    mp4.write_bytes(b"\x00")
    p.source_factory.get = MagicMock(return_value=VideoSource(path=mp4, mode="yt_dlp", duration_sec=60))
    with patch("vla.main_provider.extract_audio") as mex:
        def fake_extract(src, dst):
            dst.write_bytes(b"\x00")
        mex.side_effect = fake_extract
        asset = await p.fetch_asset(_task())
    assert asset.source == "whisper_download"
    assert asset.audio_path == mp4.with_suffix(".wav")
    assert asset.deletable is True
    # MP4 应被 unlink(FR-3.3)
    assert not mp4.exists()


@pytest.mark.asyncio
async def test_fetch_asset_scan_today_dir_last(tmp_path):
    """scan 路径:last 兜底;strategy/api/internal/factory 全失败后才用。"""
    p = _make_provider(save_dir=tmp_path / "save", today_dir=tmp_path / "today")
    p.strategy.get_subtitle = AsyncMock(return_value=None)
    p.source_factory.get = MagicMock(return_value=None)
    webm = tmp_path / "today" / "recording.webm"
    webm.write_bytes(b"\x00")
    with patch("vla.subtitle.audio_scan.scan_untranscribed_audio", return_value=webm) as mscan, \
         patch("vla.main_provider.extract_audio") as mex:
        def fake_extract(src, dst):
            dst.write_bytes(b"\x00")
        mex.side_effect = fake_extract
        asset = await p.fetch_asset(_task())
    assert asset.source == "whisper_scan"
    assert asset.audio_path == webm.with_suffix(".wav")
    assert asset.deletable is True  # wav 是我方 temp
    # webm 留原位(用户产物)
    assert webm.exists()
    # scan 在 strategy/source_factory 之后才被调(顺序断言)
    assert mscan.called


@pytest.mark.asyncio
async def test_fetch_asset_all_paths_exhausted_returns_none(tmp_path):
    p = _make_provider(save_dir=tmp_path / "save", today_dir=tmp_path / "today")
    p.strategy.get_subtitle = AsyncMock(return_value=None)
    p.source_factory.get = MagicMock(return_value=None)
    with patch("vla.subtitle.audio_scan.scan_untranscribed_audio", return_value=None):
        asset = await p.fetch_asset(_task())
    assert asset is None


@pytest.mark.asyncio
async def test_fetch_asset_scan_not_called_when_strategy_text_hits():
    """scan 是 last fallback;strategy 命中 text 时不应扫目录。"""
    p = _make_provider()
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(text="hi", source="api"))
    with patch("vla.subtitle.audio_scan.scan_untranscribed_audio") as mscan:
        await p.fetch_asset(_task())
    assert not mscan.called


@pytest.mark.asyncio
async def test_fetch_asset_internal_spider_uses_browser_capture_by_default(tmp_path):
    """Phase 9.6.4+ (2026-09-10):bill-jc 长视频默认走浏览器 4x MediaRecorder 抽音
    (已验证,更快), extract_m3u8_audio 留作 fallback。
    fetch_asset path ② 默认调 extract_browser_audio。"""
    p = RealTextProvider.__new__(RealTextProvider)
    p.cfg = MagicMock()
    p.strategy = MagicMock()
    p.transcriber = MagicMock()
    p.source_factory = MagicMock()
    p.notifier = MagicMock()
    p.plugin_status = MagicMock()
    p._save_dir = tmp_path
    p.log = MagicMock()
    p.checker = MagicMock()
    p.refiner = None
    p._today_dir = tmp_path  # T14 requirement

    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(
        text=None, source="internal_spider",
        metadata={"video_url": "https://video.bill-jc.com/foo.m3u8"},
    ))
    p.source_factory.get = MagicMock(return_value=None)

    fake_wav = tmp_path / "audio_raw" / "BV1xx.wav"

    async def fake_browser(video_url, wav_path):
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        wav_path.write_bytes(b"RIFF")
        return wav_path

    with patch("vla.main_provider.extract_browser_audio", side_effect=fake_browser) as m_browser, \
         patch("vla.main_provider.extract_m3u8_audio") as m_m3u8:
        task = VideoTask(id="BV1xx", title="t", url="https://b-learning.bill-jc.com/x", expected_duration=3600)
        asset = await p.fetch_asset(task)

    # 默认走 browser(快路径,4x)
    m_browser.assert_called_once()
    assert m_browser.call_args[0][0] == "https://video.bill-jc.com/foo.m3u8"
    # m3u8 不应被调(browser 已成功)
    m_m3u8.assert_not_called()
    assert asset is not None
    assert asset.source == "whisper_internal_download"
    assert asset.audio_path == fake_wav
    assert asset.deletable is True


@pytest.mark.asyncio
async def test_fetch_asset_internal_spider_falls_back_to_ffmpeg_when_browser_fails(tmp_path):
    """Phase 9.6.4+ (2026-09-10):browser capture 失败(Chrome 未启 / SPA 无 <video>) →
    fallback 到 extract_m3u8_audio;成功 → fetch_asset 走 ② 返回
    Asset(source='whisper_internal_download')。"""
    p = RealTextProvider.__new__(RealTextProvider)
    p.cfg = MagicMock()
    p.strategy = MagicMock()
    p.transcriber = MagicMock()
    p.source_factory = MagicMock()
    p.notifier = MagicMock()
    p.plugin_status = MagicMock()
    p._save_dir = tmp_path
    p.log = MagicMock()
    p.checker = MagicMock()
    p.refiner = None
    p._today_dir = tmp_path  # T14 requirement

    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(
        text=None, source="internal_spider",
        metadata={"video_url": "https://video.bill-jc.com/foo.m3u8"},
    ))
    p.source_factory.get = MagicMock(return_value=None)

    fake_wav = tmp_path / "audio_raw" / "BV1xx.wav"

    def fake_ffmpeg(url, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"RIFF")

    with patch(
        "vla.main_provider.extract_browser_audio",
        side_effect=RuntimeError("Chrome 9222 unreachable"),
    ) as m_browser, patch(
        "vla.main_provider.extract_m3u8_audio",
        side_effect=fake_ffmpeg,
    ) as m_m3u8:
        task = VideoTask(id="BV1xx", title="t", url="https://b-learning.bill-jc.com/x", expected_duration=3600)
        asset = await p.fetch_asset(task)

    # browser 先被试(默认路径),失败
    m_browser.assert_called_once()
    # 然后 m3u8 fallback 成功
    m_m3u8.assert_called_once()
    assert m_m3u8.call_args[0][0] == "https://video.bill-jc.com/foo.m3u8"
    assert asset is not None
    assert asset.source == "whisper_internal_download"
    assert asset.audio_path == fake_wav
    assert asset.deletable is True
    assert asset.needs_transcribe is True


@pytest.mark.asyncio
async def test_fetch_asset_internal_spider_returns_none_when_both_extract_fail(tmp_path):
    """Phase 9.6.4+ (2026-09-10):browser + ffmpeg 双双失败 → 返回 None(用户决定)。"""
    p = RealTextProvider.__new__(RealTextProvider)
    p.cfg = MagicMock()
    p.strategy = MagicMock()
    p.transcriber = MagicMock()
    p.source_factory = MagicMock()
    p.notifier = MagicMock()
    p.plugin_status = MagicMock()
    p._save_dir = tmp_path
    p.log = MagicMock()
    p.checker = MagicMock()
    p.refiner = None
    p._today_dir = tmp_path

    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(
        text=None, source="internal_spider",
        metadata={"video_url": "https://video.bill-jc.com/foo.m3u8"},
    ))
    p.source_factory.get = MagicMock(return_value=None)

    with patch(
        "vla.main_provider.extract_browser_audio",
        side_effect=RuntimeError("Chrome 9222 unreachable"),
    ), patch(
        "vla.main_provider.extract_m3u8_audio",
        side_effect=RuntimeError("ffmpeg failed"),
    ):
        task = VideoTask(id="BV1xx", title="t", url="https://b-learning.bill-jc.com/x", expected_duration=3600)
        asset = await p.fetch_asset(task)

    assert asset is None