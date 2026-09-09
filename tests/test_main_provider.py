from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vla.config import VLAConfig
from vla.main_provider import RealTextProvider, build_text_provider
from vla.quality.checker import QualityChecker
from vla.quality.refiner import SubtitleRefiner


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


# ---------------- T13: auto-construct checker / refiner ----------------


def _full_cfg(refine_enabled: bool) -> VLAConfig:
    """构造最小可用 VLAConfig(refine_enabled 控制 refiner 是否自动构造)。"""
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
        "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
        "video_source": {
            "prefer_download": True,
            "download": {"format": "worst"},
            "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28,
                       "audio_input": "0", "preset": "ultrafast"},
        },
        "quality_check": {
            "enabled": True,
            "model": "gpt-4o-mini",
            "min_score_to_pass": 70,
            "min_char_per_second": 1.0,
            "max_char_per_second": 20.0,
            "refine_enabled": refine_enabled,
            "refine_model": None,
            "refine_max_chars": 6000,
            "refine_max_output_tokens": 2000,
        },
        "browser_plugin": {
            "name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30,
            "plugin_paths": [],
        },
        "summary": {
            "model": "x", "target_words_min": 500, "target_words_max": 800,
            "notes_file": "./notes/v.md", "cross_video_dedup": True,
            "trigger_mode": "quota", "notes_section_header": "## x",
        },
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {
            "log_dir": "./logs", "notify_on_fail": False,
            "log_alert_threshold": 50, "log_alert_enabled": True,
        },
        "llm_client": {
            "provider": "openai",
            "api_key_env": "OPENAI_API_KEY",
            "base_url_env": "OPENAI_BASE_URL",
        },
    })


def _stub_provider(cfg: VLAConfig, tmp_path: Path) -> RealTextProvider:
    """调 build_text_provider(不传 checker/refiner)并取回底层 provider 实例。

    transcriber / notifier / strategy / log 全 stub,避免触发真实
    StreamingTranscriber / SubtitleStrategy / TranscriptionLog 的重初始化。
    """
    fetch, process = build_text_provider(
        cfg,
        transcriber=MagicMock(),
        notifier=MagicMock(),
        strategy=MagicMock(),
        log=MagicMock(),
        save_dir=tmp_path,
    )
    # bound method 的 __self__ 就是 RealTextProvider 实例
    return process.__self__  # type: ignore[attr-defined]


def test_build_text_provider_auto_constructs_checker(tmp_path: Path):
    """T13:不传 checker → build_text_provider 自动构造 QualityChecker。

    之前(cli._build_real_provider 不传 checker)会在第一次
    RealTextProvider.process_asset 走到 self.checker.check(...) 时
    AttributeError — 此 fix 修复生产路径 vla process / vla batch。
    """
    cfg = _full_cfg(refine_enabled=False)
    provider = _stub_provider(cfg, tmp_path)
    assert provider.checker is not None
    assert isinstance(provider.checker, QualityChecker)


def test_build_text_provider_skips_refiner_when_disabled(tmp_path: Path):
    """T13:refine_enabled=False → 即便不传 refiner,也不自动构造(避免无谓 LLM 客户端浪费)。"""
    cfg = _full_cfg(refine_enabled=False)
    provider = _stub_provider(cfg, tmp_path)
    assert provider.refiner is None


def test_build_text_provider_auto_constructs_refiner_when_enabled(tmp_path: Path):
    """T13:refine_enabled=True 且不传 refiner → 自动构造 SubtitleRefiner。

    process_asset Step 4 才能安全调 self.refiner.refine(...) 而不 AttributeError。
    """
    cfg = _full_cfg(refine_enabled=True)
    provider = _stub_provider(cfg, tmp_path)
    assert provider.refiner is not None
    assert isinstance(provider.refiner, SubtitleRefiner)
