import pytest
from unittest.mock import MagicMock

from vla.main_provider import RealTextProvider


# 注(2026-09-09 Task 10):T6 placeholder tests test_fetch_asset_placeholder +
# test_process_asset_placeholder 已删除 — T7 / T8 把 NotImplementedError 替成真实实装,
# 这两个 placeholder 测试 obsolete。coverage 走 tests/test_fetch_asset.py +
# tests/test_process_asset.py。


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
