"""Pass/Fail Flow 集成测试(SSOT: requirements.md FR-4.5/4.6 + implementation-plan.md Phase 6/7)。

模拟主调度 Phase 8 处理单条视频的核心循环:
  1. 转写 → 视频源已删(FR-3.3),audio.wav 保留(FR-3.7)
  2. 质量门控 → pass/fail
     - pass:save_transcribed() + StreamingTranscriber.cleanup(audio.wav)
     - fail:save_failed_text()(自动由 log_quality_fail 触发)+ 保留 audio.wav
  3. 配额触发 → summarize_batch(transcribed_dir)

不依赖 main.py / Phase 8,只在组件层验证数据流。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vla.config import VLAConfig
from vla.log.transcription_log import TranscriptionLog
from vla.models import QualityResult
from vla.summary.llm_summarizer import LLMSummarizer
from vla.transcribe.streaming import StreamingTranscriber


# ---------------- Fixtures ----------------


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


def make_video_and_audio(work_dir: Path, video_id: str) -> tuple[Path, Path]:
    """fake 一个 video + audio 配对文件。"""
    video = work_dir / f"{video_id}.webm"
    audio = video.with_suffix(".wav")
    video.write_bytes(b"fake video")
    audio.write_bytes(b"fake audio")
    return video, audio


class FakeQualityChecker:
    """Stub quality checker,固定返回 passed/score。"""

    def __init__(self, passed: bool, score: int = 85, issues: list[str] | None = None):
        self.passed = passed
        self.score = score
        self.issues = issues or []
        self.last_text: str | None = None

    def check(self, text: str, title: str, duration_sec: int, model_size: str) -> QualityResult:
        self.last_text = text
        return QualityResult(
            passed=self.passed,
            score=self.score,
            issues=self.issues,
            suggestion="",
            char_count=len(text),
        )


class FakeLLM:
    def __init__(self, response: str = "统一总结内容。" * 100):
        self.calls: list[dict[str, Any]] = []
        self.response = response

    def complete(self, prompt: str, max_tokens: int = 1000, temperature: float = 0.3) -> str:
        self.calls.append({"prompt": prompt})
        return self.response


class FakeTranscriber:
    """Mock StreamingTranscriber,返回预设 text,不调 ffmpeg/whisper。"""

    def __init__(self, text: str = "fake 转写文本"):
        self.text = text
        self.cleanup_calls: list[tuple[Path, ...]] = []

    def transcribe(self, video_path: Path) -> str:
        # 模拟 FR-3.3:删视频源
        if video_path.exists():
            video_path.unlink()
        return self.text

    def cleanup(self, *paths: Path) -> None:
        self.cleanup_calls.append(tuple(paths))
        for p in paths:
            if p.exists():
                p.unlink()


# ---------------- 质量通过流程 ----------------


class TestPassFlow:
    def test_pass_saves_text_and_cleans_audio(self, cfg, tmp_path):
        """通过 → save_transcribed + cleanup(audio.wav)。"""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        log_dir = tmp_path / "logs"
        video, audio = make_video_and_audio(work_dir, "v_pass")
        assert video.exists() and audio.exists()

        log = TranscriptionLog(log_dir)
        transcriber = FakeTranscriber(text="高质量的字幕内容。")
        checker = FakeQualityChecker(passed=True, score=85)

        # 模拟 Phase 8 主调度循环
        text = transcriber.transcribe(video)  # video 已删
        quality = checker.check(text, "Python 教程", duration_sec=1800, model_size="small")

        if quality.passed:
            log.save_transcribed(
                video_id="v_pass",
                title="Python 教程",
                text=text,
                quality=quality,
                source="whisper",
                duration_sec=1800,
            )
            StreamingTranscriber.cleanup(audio)  # FR-3.7:删音频

        # 验证:video 已删(FR-3.3),audio 已删(FR-4.5 + FR-3.7)
        assert not video.exists()
        assert not audio.exists()
        # 验证:text 已存盘
        text_files = list((log_dir / "transcribed").glob("*.txt"))
        assert len(text_files) == 1
        assert "Python 教程" in text_files[0].read_text(encoding="utf-8")
        assert "高质量的字幕内容" in text_files[0].read_text(encoding="utf-8")
        # 验证:cleanup 被调(用静态方法引用传 log 的 actual 调用)
        # (这里用静态方法直接调,verify 只检查 audio 已删)


# ---------------- 质量失败流程 ----------------


class TestFailFlow:
    def test_fail_saves_text_and_keeps_audio(self, cfg, tmp_path):
        """失败 → save_failed_text + 保留 audio.wav。"""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        log_dir = tmp_path / "logs"
        video, audio = make_video_and_audio(work_dir, "v_fail")

        log = TranscriptionLog(log_dir)
        transcriber = FakeTranscriber(text="质量差的字幕。")
        checker = FakeQualityChecker(passed=False, score=30, issues=["语速异常"])

        text = transcriber.transcribe(video)
        quality = checker.check(text, "失败视频", duration_sec=1800, model_size="small")

        if not quality.passed:
            log.log_quality_fail(
                video_id="v_fail",
                title="失败视频",
                url="https://www.bilibili.com/video/v_fail",
                result=quality,
                text=text,
            )
            # 注意:不删 audio.wav(FR-4.6 + FR-3.7)

        # 验证:video 已删(FR-3.3),audio 保留
        assert not video.exists()
        assert audio.exists()
        # 验证:text 已存到 failed_texts
        text_files = list((log_dir / "failed_texts").glob("*.txt"))
        assert len(text_files) == 1
        assert "失败视频" in text_files[0].read_text(encoding="utf-8")
        # 验证:quality_fail.csv 有记录
        assert (log_dir / "quality_fail.csv").exists()


# ---------------- 端到端:批量总结 ----------------


class TestBatchSummarizeAfterPasses:
    def test_quota_triggers_summarize_batch(self, cfg, tmp_path):
        """配额触发 → summarize_batch(transcribed_dir) → 写 notes_file。"""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        log_dir = tmp_path / "logs"
        transcribed_dir = log_dir / "transcribed"

        log = TranscriptionLog(log_dir)
        transcriber = FakeTranscriber(text="内容。" * 50)
        checker = FakeQualityChecker(passed=True, score=85)
        llm = FakeLLM()

        # 模拟 3 条视频都通过
        for i, title in enumerate(["Python 列表推导式", "Python 装饰器", "Python 生成器"]):
            video, audio = make_video_and_audio(work_dir, f"v{i}")
            text = transcriber.transcribe(video)
            quality = checker.check(text, title, duration_sec=1800, model_size="small")
            assert quality.passed
            log.save_transcribed(
                video_id=f"v{i}",
                title=title,
                text=text,
                quality=quality,
                source="whisper",
                duration_sec=1800 * (i + 1),
            )
            StreamingTranscriber.cleanup(audio)

        # 配额触发 → 总结
        assert len(list(transcribed_dir.glob("*.txt"))) == 3

        summarizer = LLMSummarizer(llm, cfg.summary.notes_file)
        summarizer.cfg = cfg
        result = summarizer.summarize_batch(transcribed_dir, group_title="Python 基础")
        summarizer.write_to_notes(result)

        # 验证
        notes = cfg.summary.notes_file.read_text(encoding="utf-8")
        assert "Python 基础" in notes
        assert "统一总结内容" in notes
        # 验证:transcribed 目录已清空(避免下次重复总结)
        assert list(transcribed_dir.glob("*.txt")) == []
        # 验证:LLM 调一次
        assert len(llm.calls) == 1


class TestFailureQuotaKeepsAudio:
    def test_audio_retained_after_quality_fail_across_multiple_videos(self, cfg, tmp_path):
        """多条视频连续失败 → 所有 audio.wav 都被保留(供批量重转写)。"""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        log_dir = tmp_path / "logs"

        log = TranscriptionLog(log_dir)
        transcriber = FakeTranscriber(text="烂字幕。")
        checker = FakeQualityChecker(passed=False, score=20)

        audios = []
        for i in range(3):
            video, audio = make_video_and_audio(work_dir, f"fail_{i}")
            audios.append(audio)
            text = transcriber.transcribe(video)
            quality = checker.check(text, f"失败{i}", duration_sec=1800, model_size="small")
            log.log_quality_fail(f"fail_{i}", f"失败{i}", "https://x", quality, text)

        # 所有 audio 都还在(FR-3.7)
        for audio in audios:
            assert audio.exists()
        # failed_texts 有 3 个文件
        assert len(list((log_dir / "failed_texts").glob("*.txt"))) == 3