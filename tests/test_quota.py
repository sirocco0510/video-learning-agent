"""QuotaManager 测试(SSOT: requirements.md FR-9 + implementation-plan.md Phase 7.5)。

设计:
- 累加器 >= threshold_sec(默认 21600 = 6h)→ 触发总结
- drain() 取出累计时长并清零
- progress 属性给 UI 用(0.0 ~ 1.0)
- on_exhausted 钩子("stop_session" / "summary_then_continue" 等)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vla.config import QuotaConfig, VLAConfig
from vla.state.quota import QuotaManager


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": str(tmp_path), "auto_cleanup_on_pass": True},
        "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
        "video_source": {"prefer_download": True, "download": {"format": "worst"}, "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"}},
        "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
        "browser_plugin": {"name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30, "plugin_paths": []},
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": str(tmp_path / "notes.md"), "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": str(tmp_path / "h.jsonl")},
        "logging": {"log_dir": str(tmp_path / "logs"), "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


class TestAdd:
    def test_below_threshold_does_not_trigger(self, cfg):
        """累加器 < threshold → add 返回 False。"""
        q = QuotaManager(cfg)
        q.add(duration_sec=1800)  # 30min
        assert q.current == 1800
        assert q.should_summarize() is False

    def test_at_threshold_triggers(self, cfg):
        """累加到 threshold → 返回 True。"""
        q = QuotaManager(cfg)
        triggered = q.add(duration_sec=21600)
        assert triggered is True
        assert q.current == 21600

    def test_exceeds_threshold_triggers(self, cfg):
        """累加超过 threshold → 立即返回 True。"""
        q = QuotaManager(cfg)
        triggered = q.add(duration_sec=30000)  # > 6h
        assert triggered is True
        assert q.current == 30000

    def test_accumulates_across_calls(self, cfg):
        """多次 add 累加。"""
        q = QuotaManager(cfg)
        assert q.add(duration_sec=3600) is False  # 1h
        assert q.add(duration_sec=3600) is False  # 2h
        assert q.add(duration_sec=14400) is True  # 6h,触发

    def test_not_drained_keeps_count(self, cfg):
        """未 drain → current 不归零。"""
        q = QuotaManager(cfg)
        q.add(duration_sec=21600)
        assert q.current == 21600


class TestDrain:
    def test_drain_returns_total_and_resets(self, cfg):
        """drain() → 返回总秒数 + current=0。"""
        q = QuotaManager(cfg)
        q.add(duration_sec=1800)
        q.add(duration_sec=2400)
        assert q.current == 4200

        drained = q.drain()
        assert drained == 4200
        assert q.current == 0

    def test_drain_after_trigger_clears_state(self, cfg):
        """触发后 drain → 下一轮重新计数。"""
        q = QuotaManager(cfg)
        q.add(duration_sec=21600)  # 触发
        assert q.should_summarize() is True

        q.drain()
        assert q.should_summarize() is False
        assert q.current == 0


class TestProgress:
    def test_progress_zero_at_start(self, cfg):
        """未累加 → progress=0.0。"""
        q = QuotaManager(cfg)
        assert q.progress == 0.0

    def test_progress_half_at_half(self, cfg):
        """累加 50% → progress=0.5。"""
        q = QuotaManager(cfg)
        q.add(duration_sec=10800)  # 3h / 6h
        assert q.progress == 0.5

    def test_progress_capped_at_one(self, cfg):
        """超过 threshold → progress=1.0(不 > 1)。"""
        q = QuotaManager(cfg)
        q.add(duration_sec=50000)
        assert q.progress == 1.0


class TestShouldContinue:
    def test_default_stop_session(self, cfg):
        """on_exhausted=stop_session → 触发后 should_continue()=False。"""
        q = QuotaManager(cfg)
        q.add(duration_sec=21600)  # 触发
        assert q.should_continue() is False

    def test_summary_then_continue(self, cfg):
        """on_exhausted=summary_then_continue → 触发后 should_continue()=True。"""
        cfg.quota.on_exhausted = "summary_then_continue"
        q = QuotaManager(cfg)
        q.add(duration_sec=21600)
        assert q.should_continue() is True

    def test_before_trigger_should_continue_true(self, cfg):
        """未触发 → should_continue()=True(继续)。"""
        q = QuotaManager(cfg)
        q.add(duration_sec=1000)
        assert q.should_continue() is True