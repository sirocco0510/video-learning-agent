"""VideoLearningAgent 主调度测试(SSOT: requirements.md 第七章 数据流 + Phase 8)。

设计:
- 全依赖 stub(quality checker / notifier / text_provider / log / summarizer)
- 验证:去重 → 质量 → save_transcribed + audio 清理 / save_failed_text + audio 保留
- 验证:配额触发 → summarize_batch → 写 notes
- 验证:on_exhausted=stop_session → 触发后停止
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vla.config import VLAConfig
from vla.log.transcription_log import TranscriptionLog
from vla.main import VideoLearningAgent
from vla.models import QualityResult, VideoTask
from vla.state.history import HistoryManager
from vla.state.plugin_status import PluginStatus
from vla.state.quota import QuotaManager
from vla.summary.llm_summarizer import LLMSummarizer


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


def make_task(bvid: str, title: str, *, duration: int = 1800, group: str = "g1", group_title: str | None = "测试组") -> VideoTask:
    return VideoTask(
        id=bvid,
        title=title,
        url=f"https://www.bilibili.com/video/{bvid}",
        expected_duration=duration,
        group_id=group,
        group_title=group_title,
    )


class StubChecker:
    """可控制 pass/fail 的质量检查器。"""

    def __init__(self, *, passed: bool = True, score: int = 85, issues: list[str] | None = None):
        self.passed = passed
        self.score = score
        self.issues = issues or []
        self.calls: list[dict[str, Any]] = []

    def check(self, text: str, title: str, duration_sec: int, model_size: str) -> QualityResult:
        self.calls.append({"title": title, "duration": duration_sec})
        return QualityResult(
            passed=self.passed,
            score=self.score,
            issues=self.issues,
            suggestion="",
            char_count=len(text),
        )


class StubNotifier:
    def __init__(self):
        self.infos: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []

    def info(self, title: str, message: str) -> None:
        self.infos.append((title, message))

    def warning(self, title: str, message: str) -> None:
        self.warnings.append((title, message))


class StubSummarizer:
    def __init__(self, response: str = "统一总结内容。" * 50):
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.write_calls: list[str] = []

    def summarize_batch(self, transcribed_dir, group_title=None, clear_after=True) -> str:
        self.calls.append({
            "transcribed_dir": str(transcribed_dir),
            "group_title": group_title,
            "clear_after": clear_after,
        })
        # 实际清空 transcribed_dir(否则下次还会读)
        if clear_after:
            for f in transcribed_dir.glob("*.txt"):
                f.unlink()
        return self.response

    def write_to_notes(self, content: str) -> None:
        self.write_calls.append(content)


def make_text_provider(mapping: dict[str, tuple[str, str, Path | None]]):
    """mapping: bvid → (text, source, audio_path_or_None)"""
    def provider(task: VideoTask) -> tuple[str, str, Path | None]:
        if task.id not in mapping:
            raise RuntimeError(f"未知 bvid: {task.id}")
        return mapping[task.id]
    return provider


def make_agent(
    cfg: VLAConfig,
    *,
    checker: StubChecker,
    notifier: StubNotifier,
    summarizer: StubSummarizer,
    text_provider,
    plugin_status: PluginStatus | None = None,
) -> VideoLearningAgent:
    log = TranscriptionLog(cfg.logging.log_dir)
    history = HistoryManager(cfg.history.file)
    quota = QuotaManager(cfg)
    return VideoLearningAgent(
        cfg=cfg,
        checker=checker,
        log=log,
        history=history,
        quota=quota,
        summarizer=summarizer,
        notifier=notifier,
        text_provider=text_provider,
        plugin_status=plugin_status,
    )


# ---------------- 主流程:通过路径 ----------------


class TestPassFlow:
    def test_single_pass_saves_and_records(self, cfg, tmp_path):
        """单条通过 → save_transcribed + history 记录 + notifier info。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        audio = tmp_path / "v1.wav"
        audio.write_bytes(b"fake audio")

        provider = make_text_provider({
            "BV1": ("这是一段高质量字幕。", "whisper", audio),
        })
        agent = make_agent(cfg, checker=checker, notifier=notifier, summarizer=summarizer, text_provider=provider)

        stats = agent.run([make_task("BV1", "测试", duration=1800)])

        assert stats == {"processed": 1, "passed": 1, "failed": 0, "skipped": 0, "summarized": 0}
        # transcribed/ 有文件
        transcribed_dir = Path(cfg.logging.log_dir) / "transcribed"
        text_files = list(transcribed_dir.glob("*.txt"))
        assert len(text_files) == 1
        # audio 已删
        assert not audio.exists()
        # history 有记录
        assert agent.history.is_already_done(agent._url_key(make_task("BV1", "x")))
        # notifier 调了
        assert len(notifier.infos) == 1
        assert "质量通过" in notifier.infos[0][0]

    def test_multiple_pass_accumulates(self, cfg, tmp_path):
        """多条通过 → 累加配额,未达 6h 不总结。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        audios = []
        for i in range(3):
            a = tmp_path / f"v{i}.wav"
            a.write_bytes(b"x")
            audios.append(a)

        provider = make_text_provider({
            f"BV{i}": (f"内容{i}", "whisper", audios[i]) for i in range(3)
        })
        agent = make_agent(cfg, checker=checker, notifier=notifier, summarizer=summarizer, text_provider=provider)

        tasks = [make_task(f"BV{i}", f"v{i}", duration=1800) for i in range(3)]
        stats = agent.run(tasks)

        assert stats == {"processed": 3, "passed": 3, "failed": 0, "skipped": 0, "summarized": 0}
        assert agent.quota.current == 5400  # 3 * 1800
        # 3 个 transcribed 文件
        assert len(list((Path(cfg.logging.log_dir) / "transcribed").glob("*.txt"))) == 3
        # 没总结
        assert len(summarizer.calls) == 0


# ---------------- 主流程:失败路径 ----------------


class TestFailFlow:
    def test_quality_fail_logs_csv_and_keeps_audio(self, cfg, tmp_path):
        """质量失败 → log_quality_fail + audio 保留 + failed_texts 有文件。"""
        checker = StubChecker(passed=False, score=30, issues=["语速异常"])
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        audio = tmp_path / "fail.wav"
        audio.write_bytes(b"x")

        provider = make_text_provider({
            "BV1": ("烂字幕。", "whisper", audio),
        })
        agent = make_agent(cfg, checker=checker, notifier=notifier, summarizer=summarizer, text_provider=provider)

        stats = agent.run([make_task("BV1", "失败视频")])

        assert stats["failed"] == 1
        assert stats["passed"] == 0
        # audio 保留
        assert audio.exists()
        # quality_fail.csv 写了
        assert (Path(cfg.logging.log_dir) / "quality_fail.csv").exists()
        # failed_texts 有文件
        assert len(list((Path(cfg.logging.log_dir) / "failed_texts").glob("*.txt"))) == 1
        # transcribed/ 没文件
        assert list((Path(cfg.logging.log_dir) / "transcribed").glob("*.txt")) == []
        # history 没记录
        assert not agent.history.is_already_done(agent._url_key(make_task("BV1", "x")))

    def test_plugin_quality_fail_marks_unavailable(self, cfg, tmp_path):
        """插件字幕质量失败 → plugin_status 标 unavailable(FR-2.11)。

        注:SubtitleStrategy 用 source="browser" 表示"通过浏览器插件取的字幕"。
        """
        checker = StubChecker(passed=False, score=30)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        plugin_status = PluginStatus()

        provider = make_text_provider({
            "BV1": ("插件字幕差。", "browser", None),
        })
        agent = make_agent(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            text_provider=provider, plugin_status=plugin_status,
        )

        agent.run([make_task("BV1", "插件字幕视频")])

        assert plugin_status.is_unavailable()
        assert plugin_status.reason == "plugin_quality_fail"

    def test_text_provider_exception_logs_transcribe_fail(self, cfg):
        """text_provider 抛异常 → log_transcribe_fail(FR-6.4)。"""
        checker = StubChecker(passed=True)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        def broken_provider(task):
            raise RuntimeError("network error")

        agent = make_agent(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            text_provider=broken_provider,
        )

        stats = agent.run([make_task("BV1", "失败")])

        assert stats["failed"] == 1
        assert (Path(cfg.logging.log_dir) / "transcribe_fail.csv").exists()


# ---------------- 去重 ----------------


class TestDedup:
    def test_skips_already_done_videos(self, cfg, tmp_path):
        """已转写过的 url → 跳过,不动。"""
        checker = StubChecker(passed=True)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        audio = tmp_path / "v.wav"
        audio.write_bytes(b"x")

        # 先手动记录
        history = HistoryManager(cfg.history.file)
        history.record_success(
            url_key=HistoryManager.make_url_key("g1", "BV1"),
            title="已转写", duration_sec=1800, group_id="g1", source="whisper",
        )

        # provider 即使能返回 text,也不该被调
        provider = make_text_provider({
            "BV1": ("text", "whisper", audio),
        })
        log = TranscriptionLog(cfg.logging.log_dir)
        quota = QuotaManager(cfg)
        agent = VideoLearningAgent(
            cfg=cfg, checker=checker, log=log, history=history,
            quota=quota, summarizer=summarizer, notifier=notifier,
            text_provider=provider,
        )

        stats = agent.run([make_task("BV1", "已转写")])

        assert stats["skipped"] == 1
        assert stats["processed"] == 0
        # provider 没被调(checker 没收到 call)
        assert len(checker.calls) == 0


# ---------------- 配额触发 ----------------


class TestQuotaTrigger:
    def test_triggers_summarize_at_threshold(self, cfg, tmp_path):
        """累加 >= 6h → summarize_batch + 写 notes + session 结束(stop_session)。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer(response="批量总结。" * 30)

        # 一条 6h 视频 → 立即触发
        provider = make_text_provider({
            "BV_BIG": ("6小时字幕。" * 100, "whisper", None),
        })
        agent = make_agent(cfg, checker=checker, notifier=notifier, summarizer=summarizer, text_provider=provider)

        stats = agent.run([make_task("BV_BIG", "长视频", duration=21600, group="Python基础", group_title="Python基础")])

        assert stats["summarized"] == 1
        assert stats["passed"] == 1
        # summarizer 被调
        assert len(summarizer.calls) == 1
        assert summarizer.calls[0]["group_title"] == "Python基础"
        # notes 写了
        assert len(summarizer.write_calls) == 1
        assert "批量总结" in summarizer.write_calls[0]
        # transcribed/ 已清空
        assert list((Path(cfg.logging.log_dir) / "transcribed").glob("*.txt")) == []

    def test_summary_then_continue_does_not_break(self, cfg, tmp_path):
        """on_exhausted=summary_then_continue → 触发后继续下一条。"""
        cfg.quota.on_exhausted = "summary_then_continue"
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        provider = make_text_provider({
            "BV1": ("a" * 100, "whisper", None),
            "BV2": ("b" * 100, "whisper", None),
        })
        agent = make_agent(cfg, checker=checker, notifier=notifier, summarizer=summarizer, text_provider=provider)

        tasks = [
            make_task("BV1", "first", duration=21600),  # 触发
            make_task("BV2", "second", duration=1800),
        ]
        stats = agent.run(tasks)

        assert stats["processed"] == 2
        assert stats["summarized"] == 1
        assert stats["passed"] == 2

    def test_stop_session_breaks_after_trigger(self, cfg, tmp_path):
        """on_exhausted=stop_session → 触发后立即 break,后面视频跳过。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        provider = make_text_provider({
            "BV1": ("a" * 100, "whisper", None),
            "BV2": ("b" * 100, "whisper", None),
            "BV3": ("c" * 100, "whisper", None),
        })
        agent = make_agent(cfg, checker=checker, notifier=notifier, summarizer=summarizer, text_provider=provider)

        tasks = [
            make_task("BV1", "first", duration=21600),  # 触发
            make_task("BV2", "second", duration=1800),  # 应跳过
            make_task("BV3", "third", duration=1800),   # 应跳过
        ]
        stats = agent.run(tasks)

        assert stats["processed"] == 1
        assert stats["summarized"] == 1


# ---------------- 空任务列表 ----------------


class TestEmpty:
    def test_empty_tasks_returns_zero_stats(self, cfg):
        """空任务 → 全 0 计数,无报错。"""
        checker = StubChecker()
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        agent = make_agent(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            text_provider=make_text_provider({}),
        )

        stats = agent.run([])

        assert stats == {"processed": 0, "passed": 0, "failed": 0, "skipped": 0, "summarized": 0}