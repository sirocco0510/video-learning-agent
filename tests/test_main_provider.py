import pytest
from unittest.mock import MagicMock

from vla.main_provider import RealTextProvider


@pytest.mark.asyncio
async def test_fetch_asset_placeholder():
    p = RealTextProvider.__new__(RealTextProvider)  # 跳过 __init__ 依赖
    # 手动塞依赖(stub)
    p.strategy = None
    p.transcriber = None
    p.source_factory = None
    p.checker = None
    p.refiner = None
    p.log = None
    p.plugin_status = None
    p._save_dir = None
    p._today_dir = None
    from vla.models import VideoTask
    task = VideoTask(id="t", title="t", url="https://x", expected_duration=60)
    with pytest.raises(NotImplementedError):
        await p.fetch_asset(task)


@pytest.mark.asyncio
async def test_process_asset_placeholder():
    p = RealTextProvider.__new__(RealTextProvider)
    from vla.models import Asset, VideoTask
    asset = Asset(text=None, source="whisper_download", audio_path=None)
    task = VideoTask(id="t", title="t", url="https://x", expected_duration=60)
    with pytest.raises(NotImplementedError):
        await p.process_asset(asset, task)


@pytest.mark.asyncio
async def test_call_delegates_to_fetch_then_process():
    """__call__ = fetch_asset + process_asset 串起来。"""
    p = RealTextProvider.__new__(RealTextProvider)
    from vla.models import Asset, ProcessResult, QualityResult, VideoTask
    fake_asset = Asset(text="hi", source="api", audio_path=None)
    fake_result = ProcessResult(text="hi", qr=QualityResult(score=90, passed=True,
                                                          issues=[], suggestion="ok", char_count=2),
                                source="api", duration_sec=60)
    async def fake_fetch(task): return fake_asset
    async def fake_process(asset, task): return fake_result
    p.fetch_asset = fake_fetch  # type: ignore
    p.process_asset = fake_process  # type: ignore
    task = VideoTask(id="t", title="t", url="https://x", expected_duration=60)
    asset, result = await p(task)
    assert asset is fake_asset
    assert result is fake_result


def test_build_text_provider_returns_tuple():
    from vla.main_provider import build_text_provider
    from vla.models import VideoTask
    cfg = MagicMock()
    transcriber = MagicMock()
    notifier = MagicMock()
    fetch, process = build_text_provider(cfg, transcriber, notifier)
    assert callable(fetch)
    assert callable(process)
