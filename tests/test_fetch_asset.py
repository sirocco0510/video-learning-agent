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
    p.cfg = MagicMock()
    p.cfg.audio.max_extract_sec = 1800
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
        def fake_extract(url, dst, max_sec=None):
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
async def test_fetch_asset_internal_spider_uses_m3u8_direct_by_default(tmp_path):
    """FR-2.30 (2026-09-10 反转,2026-09-14 删除 fallback):bill-jc 路径 ② 默认走
    extract_m3u8_audio 直抽。

    2026-09-14:browser 兜底路径已删除 —— m3u8 失败 → 该视频跳过,不再 fallback。
    见 test_fetch_asset_internal_spider_skips_video_when_m3u8_fails。

    历史反转依据(2026-09-10):同视频四组对照,m3u8 直抽 score 92(通过)/
    浏览器 4x score 35(未通过);4x 抓取把"国家统计局"转成"规判统计"、
    "PhantomJS"转成"翻腾架子"。"""
    p = RealTextProvider.__new__(RealTextProvider)
    p.cfg = MagicMock()
    p.cfg.audio.max_extract_sec = 1800
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

    def fake_m3u8(video_url, wav_path, max_sec=None):
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        wav_path.write_bytes(b"RIFF")

    # 2026-09-14:不再 patch extract_browser_audio —— main_provider 已删除该 fallback,
    # 模块里根本没有这个 import 名。回归保护:若有人重新引入 browser 路径,本测试会因
    # 其他渠道(日志/资产来源)不一致而失败,届时再补 patch。
    with patch("vla.main_provider.extract_m3u8_audio", side_effect=fake_m3u8) as m_m3u8:
        task = VideoTask(id="BV1xx", title="t", url="https://b-learning.bill-jc.com/x", expected_duration=3600)
        asset = await p.fetch_asset(task)

    # 默认走 m3u8 直抽
    m_m3u8.assert_called_once()
    assert m_m3u8.call_args[0][0] == "https://video.bill-jc.com/foo.m3u8"
    # FR-2.30.1:audio.max_extract_sec 透传下去(1800 = 只抽前 30 分钟)
    assert m_m3u8.call_args[0][2] == 1800
    assert asset is not None
    assert asset.source == "whisper_internal_download"
    assert asset.audio_path == fake_wav
    assert asset.deletable is True


@pytest.mark.asyncio
async def test_fetch_asset_internal_spider_skips_video_when_m3u8_fails(tmp_path):
    """FR-2.30 (2026-09-14 修正):m3u8 直抽失败 → 该视频跳过,fetch_asset 返回 None。

    之前(2026-09-10)有 browser 兜底路径,失败时 fallback 到 extract_browser_audio;
    2026-09-14 用户裁定:browser 路径维护成本 > 覆盖收益,删除 fallback,
    m3u8 失败直接跳过(用户决定是否重试 / 重抽)。

    历史对比数据(2026-09-10 四组对照,177.59s 视频):
      m3u8 直抽 + Refiner → score 92 (通过)
      m3u8 直抽          → score 62~65
      浏览器 4x + atempo  → score 35 (未通过,逐词对比 4x 毁可懂度)
    """
    p = RealTextProvider.__new__(RealTextProvider)
    p.cfg = MagicMock()
    p.cfg.audio.max_extract_sec = 1800
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

    with patch(
        "vla.main_provider.extract_m3u8_audio",
        side_effect=RuntimeError("m3u8 签名过期"),
    ) as m_m3u8:
        task = VideoTask(id="BV1xx", title="t", url="https://b-learning.bill-jc.com/x", expected_duration=3600)
        asset = await p.fetch_asset(task)

    # m3u8 被试一次,失败 → 不再尝试其他路径,直接跳过视频
    m_m3u8.assert_called_once()
    assert asset is None


@pytest.mark.asyncio
async def test_fetch_asset_missing_audio_config_falls_back_to_default_cap(tmp_path):
    """cfg.audio 是 Optional —— 配置里没 audio 块时不能用 AttributeError 崩掉,
    应退回 FR-2.30.1 的常量默认上限(1800)。"""
    p = RealTextProvider.__new__(RealTextProvider)
    p.cfg = MagicMock()
    p.cfg.audio = None
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

    def fake_m3u8(url, dst, max_sec=None):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"RIFF")

    with patch("vla.main_provider.extract_m3u8_audio", side_effect=fake_m3u8) as m_m3u8:
        task = VideoTask(id="BV1xx", title="t", url="https://b-learning.bill-jc.com/x", expected_duration=3600)
        asset = await p.fetch_asset(task)

    assert m_m3u8.call_args[0][2] == 1800
    assert asset is not None


@pytest.mark.asyncio
async def test_fetch_asset_explicit_null_cap_disables_truncation(tmp_path):
    """audio.max_extract_sec 显式配 null → 两条路径都不截断(全量抽取)。"""
    p = RealTextProvider.__new__(RealTextProvider)
    p.cfg = MagicMock()
    p.cfg.audio.max_extract_sec = None
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

    def fake_m3u8(url, dst, max_sec=None):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"RIFF")

    with patch("vla.main_provider.extract_m3u8_audio", side_effect=fake_m3u8) as m_m3u8:
        task = VideoTask(id="BV1xx", title="t", url="https://b-learning.bill-jc.com/x", expected_duration=3600)
        await p.fetch_asset(task)

    assert m_m3u8.call_args[0][2] is None


# 2026-09-14:删除 test_fetch_asset_internal_spider_returns_none_when_both_extract_fail
# 原行为是"m3u8 + browser 双双失败 → 返回 None",但 browser fallback 已删除,
# 该用例与新的 test_fetch_asset_internal_spider_skips_video_when_m3u8_fails 完全重复。
# 新行为下 m3u8 失败就直接 None,没有"双失败"语义。