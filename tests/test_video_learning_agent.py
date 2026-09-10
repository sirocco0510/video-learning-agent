"""VideoLearningAgent 主调度测试(SSOT: requirements.md 第七章 数据流 + Phase 8)。

设计:
- 全依赖 stub(quality checker / notifier / fetch_asset / process_asset / log / summarizer)
- 验证:去重 → 质量 → save_transcribed + audio 清理 / save_failed_text + audio 保留
- 验证:配额触发 → summarize_batch → 写 notes
- 验证:on_exhausted=stop_session → 触发后停止

v3.3 (Task 10 / 2026-09-09 asset-pipeline-refactor):
text_provider 单 callable 拆成 fetch_asset + process_asset 双 callable,
由 _process_one 串接。本测试套件 stub 同步改用 (fetch_asset, process_asset)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vla.config import VLAConfig
from vla.log.transcription_log import TranscriptionLog
from vla.main import VideoLearningAgent
from vla.models import Asset, ProcessResult, QualityResult, VideoTask
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
        # 2026-09-10 结构调整:递归找 transcripts/*.txt
        if clear_after:
            for f in transcribed_dir.rglob("transcripts/*.txt"):
                f.unlink()
        return self.response

    def write_to_notes(self, content: str) -> None:
        self.write_calls.append(content)


def make_provider_pair(
    mapping: dict[str, tuple[str, str, Path | None]],
    checker: StubChecker,
    *,
    log: Any = None,
    plugin_status: Any = None,
    refiner: Any = None,
    cfg: Any = None,
    fetch_returns_none: bool = False,
) -> tuple[Any, Any]:
    """返回 (fetch_asset, process_asset) 双 stub,mapping: bvid → (text, source, audio_path)。

    fetch_asset: 把 mapping 转 Asset(audio_path 来自 wav, deletable=True if 非 None)。
        fetch_returns_none=True 时,fetch_asset 直接返 None(模拟 fetch_asset 全失败)。
    process_asset: 调 checker.check (StubChecker 控制 pass/fail),然后:
        - 失败: 调 log.log_quality_fail + plugin_status.mark_unavailable (if browser) → return None
        - 成功: 调 refiner.refine (if cfg.quality_check.refine_enabled + refiner) →
                 调 log.save_transcribed → unlink wav (if deletable) → return ProcessResult
    """
    async def fetch_asset(task: VideoTask) -> Asset | None:
        if fetch_returns_none:
            return None
        if task.id not in mapping:
            return None
        text, source, audio_path = mapping[task.id]
        return Asset(
            text=text,
            source=source,
            audio_path=audio_path,
            deletable=audio_path is not None,
        )

    async def process_asset(asset: Asset, task: VideoTask) -> ProcessResult | None:
        qr = checker.check(
            text=asset.text or "",
            title=task.title,
            duration_sec=task.expected_duration,
            model_size="small",
        )
        if not qr.passed:
            if log is not None:
                log.log_quality_fail(task.id, task.title, str(task.url), qr, asset.text or "")
            if plugin_status is not None and asset.source == "browser":
                plugin_status.mark_unavailable(reason="plugin_quality_fail")
            return None
        # Refine(可选,模拟 main_provider.process_asset 的 Step 4)
        text = asset.text or ""
        if (
            cfg is not None
            and getattr(cfg.quality_check, "refine_enabled", False)
            and refiner is not None
        ):
            try:
                refinement = refiner.refine(text, title=task.title)
                if refinement.cleaned_text:
                    text = refinement.cleaned_text
            except Exception:
                pass  # 失败用原文(stub 简化处理)
        # save_transcribed(模拟 main_provider.process_asset 的 Step 5)
        if log is not None:
            log.save_transcribed(
                video_id=task.id, title=task.title, text=text,
                quality=qr, source=asset.source,
                duration_sec=task.expected_duration,
            )
        # wav cleanup (模拟 main_provider.process_asset 的 Step 6 / FR-3.7)
        if asset.deletable and asset.audio_path is not None and asset.audio_path.exists():
            try:
                asset.audio_path.unlink()
            except Exception:
                pass
        return ProcessResult(
            text=text,
            qr=qr,
            source=asset.source,
            duration_sec=task.expected_duration,
        )

    return fetch_asset, process_asset


def make_agent(
    cfg: VLAConfig,
    *,
    notifier: StubNotifier,
    summarizer: StubSummarizer,
    fetch_asset,
    process_asset,
    plugin_status: PluginStatus | None = None,
    refiner: StubRefiner | None = None,
    browser_driver: Any = None,
    screenshot_controller: Any = None,
) -> VideoLearningAgent:
    log = TranscriptionLog(cfg.logging.log_dir)
    history = HistoryManager(cfg.history.file)
    quota = QuotaManager(cfg)
    return VideoLearningAgent(
        cfg=cfg,
        log=log,
        history=history,
        quota=quota,
        summarizer=summarizer,
        notifier=notifier,
        fetch_asset=fetch_asset,
        process_asset=process_asset,
        plugin_status=plugin_status,
        refiner=refiner,
        browser_driver=browser_driver,
        screenshot_controller=screenshot_controller,
    )


def make_agent_and_pair(
    cfg: VLAConfig,
    *,
    checker: StubChecker,
    notifier: StubNotifier,
    summarizer: StubSummarizer,
    mapping: dict[str, tuple[str, str, Path | None]],
    plugin_status: PluginStatus | None = None,
    refiner: StubRefiner | None = None,
    browser_driver: Any = None,
    screenshot_controller: Any = None,
    fetch_returns_none: bool = False,
) -> tuple[VideoLearningAgent, Any, Any]:
    """一体化的"造 agent + 造 (fetch_asset, process_asset)"。

    内部绑 log / cfg / plugin_status / refiner 进 process_asset 闭包,
    让 stub 行为接近 main_provider.process_asset(save / cleanup / plugin_status / refine)。

    Returns:
        (agent, fetch_asset, process_asset)
    """
    log = TranscriptionLog(cfg.logging.log_dir)
    fetch, process = make_provider_pair(
        mapping, checker,
        log=log, plugin_status=plugin_status, refiner=refiner, cfg=cfg,
        fetch_returns_none=fetch_returns_none,
    )
    agent = make_agent(
        cfg, notifier=notifier, summarizer=summarizer,
        fetch_asset=fetch, process_asset=process,
        plugin_status=plugin_status, refiner=refiner,
        browser_driver=browser_driver,
        screenshot_controller=screenshot_controller,
    )
    return agent, fetch, process


# ---------------- 主流程:通过路径 ----------------


class TestPassFlow:
    async def test_single_pass_saves_and_records(self, cfg, tmp_path):
        """单条通过 → save_transcribed + history 记录 + notifier info。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        audio = tmp_path / "v1.wav"
        audio.write_bytes(b"fake audio")

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("这是一段高质量字幕。", "whisper", audio)},
        )

        stats = await agent.run([make_task("BV1", "测试", duration=1800)])

        assert stats == {"processed": 1, "passed": 1, "failed": 0, "skipped": 0, "summarized": 0}
        # transcribed/ 有文件
        transcribed_dir = Path(cfg.logging.log_dir) / "transcribed"
        text_files = list(transcribed_dir.rglob("transcripts/*.txt"))
        assert len(text_files) == 1
        # audio 已删
        assert not audio.exists()
        # history 有记录
        assert agent.history.is_already_done(agent._url_key(make_task("BV1", "x")))
        # notifier 调了
        assert len(notifier.infos) == 1
        assert "质量通过" in notifier.infos[0][0]

    async def test_multiple_pass_accumulates(self, cfg, tmp_path):
        """多条通过 → 累加配额,未达 6h 不总结。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        audios = []
        for i in range(3):
            a = tmp_path / f"v{i}.wav"
            a.write_bytes(b"x")
            audios.append(a)

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={f"BV{i}": (f"内容{i}", "whisper", audios[i]) for i in range(3)},
        )

        tasks = [make_task(f"BV{i}", f"v{i}", duration=1800) for i in range(3)]
        stats = await agent.run(tasks)

        assert stats == {"processed": 3, "passed": 3, "failed": 0, "skipped": 0, "summarized": 0}
        assert agent.quota.current == 5400  # 3 * 1800
        # 3 个 transcribed 文件
        assert len(list((Path(cfg.logging.log_dir) / "transcribed").rglob("transcripts/*.txt"))) == 3
        # 没总结
        assert len(summarizer.calls) == 0


# ---------------- 主流程:失败路径 ----------------


class TestFailFlow:
    async def test_quality_fail_logs_csv_and_keeps_audio(self, cfg, tmp_path):
        """质量失败 → log_quality_fail + audio 保留 + failed_texts 有文件。"""
        checker = StubChecker(passed=False, score=30, issues=["语速异常"])
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        audio = tmp_path / "fail.wav"
        audio.write_bytes(b"x")

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("烂字幕。", "whisper", audio)},
        )

        stats = await agent.run([make_task("BV1", "失败视频")])

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

    async def test_plugin_quality_fail_marks_unavailable(self, cfg, tmp_path):
        """插件字幕质量失败 → plugin_status 标 unavailable(FR-2.11)。

        注:SubtitleStrategy 用 source="browser" 表示"通过浏览器插件取的字幕"。
        """
        checker = StubChecker(passed=False, score=30)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        plugin_status = PluginStatus()

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("插件字幕差。", "browser", None)},
            plugin_status=plugin_status,
        )

        await agent.run([make_task("BV1", "插件字幕视频")])

        assert plugin_status.is_unavailable()
        assert plugin_status.reason == "plugin_quality_fail"

    async def test_fetch_asset_returns_none_logs_transcribe_fail(self, cfg):
        """fetch_asset 返 None(全失败) → log_transcribe_fail(FR-6.4)。"""
        checker = StubChecker(passed=True)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={}, fetch_returns_none=True,
        )

        stats = await agent.run([make_task("BV1", "失败")])

        assert stats["failed"] == 1
        assert (Path(cfg.logging.log_dir) / "transcribe_fail.csv").exists()


# ---------------- 去重 ----------------


class TestDedup:
    async def test_skips_already_done_videos(self, cfg, tmp_path):
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
        log = TranscriptionLog(cfg.logging.log_dir)
        fetch, process = make_provider_pair(
            {"BV1": ("text", "whisper", audio)}, checker, log=log,
        )
        quota = QuotaManager(cfg)
        agent = VideoLearningAgent(
            cfg=cfg, log=log, history=history,
            quota=quota, summarizer=summarizer, notifier=notifier,
            fetch_asset=fetch, process_asset=process,
        )

        stats = await agent.run([make_task("BV1", "已转写")])

        assert stats["skipped"] == 1
        assert stats["processed"] == 0
        # provider 没被调(checker 没收到 call)
        assert len(checker.calls) == 0


# ---------------- 配额触发 ----------------


class TestQuotaTrigger:
    async def test_triggers_summarize_at_threshold(self, cfg, tmp_path):
        """累加 >= 6h → summarize_batch + 写 notes + session 结束(stop_session)。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer(response="批量总结。" * 30)

        # 一条 6h 视频 → 立即触发
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV_BIG": ("6小时字幕。" * 100, "whisper", None)},
        )

        stats = await agent.run([make_task("BV_BIG", "长视频", duration=21600, group="Python基础", group_title="Python基础")])

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

    async def test_summary_then_continue_does_not_break(self, cfg, tmp_path):
        """on_exhausted=summary_then_continue → 触发后继续下一条。"""
        cfg.quota.on_exhausted = "summary_then_continue"
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={
                "BV1": ("a" * 100, "whisper", None),
                "BV2": ("b" * 100, "whisper", None),
            },
        )

        tasks = [
            make_task("BV1", "first", duration=21600),  # 触发
            make_task("BV2", "second", duration=1800),
        ]
        stats = await agent.run(tasks)

        assert stats["processed"] == 2
        assert stats["summarized"] == 1
        assert stats["passed"] == 2

    async def test_stop_session_breaks_after_trigger(self, cfg, tmp_path):
        """on_exhausted=stop_session → 触发后立即 break,后面视频跳过。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={
                "BV1": ("a" * 100, "whisper", None),
                "BV2": ("b" * 100, "whisper", None),
                "BV3": ("c" * 100, "whisper", None),
            },
        )

        tasks = [
            make_task("BV1", "first", duration=21600),  # 触发
            make_task("BV2", "second", duration=1800),  # 应跳过
            make_task("BV3", "third", duration=1800),   # 应跳过
        ]
        stats = await agent.run(tasks)

        assert stats["processed"] == 1
        assert stats["summarized"] == 1


# ---------------- 空任务列表 ----------------


class TestEmpty:
    async def test_empty_tasks_returns_zero_stats(self, cfg):
        """空任务 → 全 0 计数,无报错。"""
        checker = StubChecker()
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={},
        )

        stats = await agent.run([])

        assert stats == {"processed": 0, "passed": 0, "failed": 0, "skipped": 0, "summarized": 0}


# ---------------- FR-6.6 FailureAlert 集成 ----------------


class TestFailureAlertIntegration:
    """VideoLearningAgent 与 FailureAlert 集成 — 失败后 check_after_write 被调。"""

    async def test_quality_fail_triggers_check_after_write(self, cfg, monkeypatch):
        """质量失败 → failure_alert.check_after_write() 被调(FR-6.6)。"""
        from unittest.mock import MagicMock
        from vla.log.failure_alert import FailureAlert

        checker = StubChecker(passed=False, score=30, issues=["low_cps"])
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        # 用 mock FailureAlert 注入,验证 check_after_write 被调
        alert = MagicMock(spec=FailureAlert)
        log = TranscriptionLog(cfg.logging.log_dir)
        history = HistoryManager(cfg.history.file)
        quota = QuotaManager(cfg)
        fetch, process = make_provider_pair(
            {"BV1": ("quality fail text", "whisper", None)}, checker, log=log,
        )
        agent = VideoLearningAgent(
            cfg=cfg,
            log=log,
            history=history,
            quota=quota,
            summarizer=summarizer,
            notifier=notifier,
            fetch_asset=fetch,
            process_asset=process,
            failure_alert=alert,
        )

        tasks = [make_task("BV1", "fail video")]
        await agent.run(tasks)

        # 失败 1 次 → check_after_write 至少调 1 次(FR-6.6)
        assert alert.check_after_write.call_count >= 1

    async def test_fetch_asset_none_triggers_check_after_write(self, cfg):
        """fetch_asset 返 None → check_after_write 也被调。"""
        from unittest.mock import MagicMock
        from vla.log.failure_alert import FailureAlert

        checker = StubChecker()
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        alert = MagicMock(spec=FailureAlert)

        log = TranscriptionLog(cfg.logging.log_dir)
        history = HistoryManager(cfg.history.file)
        quota = QuotaManager(cfg)
        fetch, process = make_provider_pair({}, checker, fetch_returns_none=True, log=log)
        agent = VideoLearningAgent(
            cfg=cfg,
            log=log,
            history=history,
            quota=quota,
            summarizer=summarizer,
            notifier=notifier,
            fetch_asset=fetch,
            process_asset=process,
            failure_alert=alert,
        )

        tasks = [make_task("BV1", "fail video")]
        await agent.run(tasks)

        assert alert.check_after_write.call_count >= 1

    async def test_default_failure_alert_uses_config_threshold(self, cfg):
        """不传 failure_alert → 默认按 cfg.logging.log_alert_threshold 构造。"""
        checker = StubChecker()
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer, mapping={},
        )
        assert agent.failure_alert is not None
        assert agent.failure_alert.threshold == cfg.logging.log_alert_threshold
        assert agent.failure_alert.enabled == cfg.logging.log_alert_enabled

    async def test_log_alert_disabled_via_config(self, cfg):
        """cfg.logging.log_alert_enabled=False → FailureAlert.enabled=False。"""
        cfg.logging.log_alert_enabled = False
        checker = StubChecker()
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer, mapping={},
        )
        assert agent.failure_alert.enabled is False


# ---------------- v3.2 Refine 顺序调整(FR-3.9) ----------------


class StubRefiner:
    """F2-6.3:SubtitleRefinerLike stub,可记录 refine() 调用 + 控制抛错。"""

    def __init__(self, cleaned_text: str = "refined text", raise_on_call: bool = False):
        self.cleaned_text = cleaned_text
        self.raise_on_call = raise_on_call
        self.calls: list[dict[str, Any]] = []

    def refine(self, text: str, title: str):
        self.calls.append({"text": text, "title": title})
        if self.raise_on_call:
            raise RuntimeError("refiner fail")
        from vla.quality.refiner import RefinementResult
        return RefinementResult(
            cleaned_text=self.cleaned_text,
            corrections=[],
            notes="",
        )


class TestRefineOrdering:
    """v3.2:Refine 由 main.py._process_one 在 quality.passed=True 之后显式调。

    改前:StreamingTranscriber 内部注入 refiner,不论质量都跑
    改后:refiner 由 main.py 调,quality_fail → 不调 refiner(节省 L4 token)
    """

    async def test_quality_fail_does_not_call_refiner(self, cfg, tmp_path):
        """quality.passed=False → refiner.refine 不被调用。"""
        checker = StubChecker(passed=False, score=40)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        refiner = StubRefiner(cleaned_text="SHOULD NOT APPEAR")

        audio = tmp_path / "v.wav"
        audio.write_text("x")

        cfg.quality_check.refine_enabled = True
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("good text " * 50, "whisper", audio)},
            refiner=refiner,
        )
        await agent.run([make_task("BV1", "fail vid")])

        # quality_fail → 不调 refiner
        assert len(refiner.calls) == 0
        # 音频保留(quality_fail 不删)
        assert audio.exists()

    async def test_quality_pass_calls_refiner(self, cfg, tmp_path):
        """quality.passed=True + refine_enabled → refiner.refine 被调。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        refiner = StubRefiner(cleaned_text="经过云端 LLM 整理的文本")

        audio = tmp_path / "v.wav"
        audio.write_text("x")
        original_text = "original text " * 50

        cfg.quality_check.refine_enabled = True
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": (original_text, "whisper", audio)},
            refiner=refiner,
        )
        await agent.run([make_task("BV1", "pass vid")])

        # quality_pass → 调 refiner
        assert len(refiner.calls) == 1
        assert refiner.calls[0]["title"] == "pass vid"
        # 音频删了(quality_pass)
        assert not audio.exists()

    async def test_refiner_disabled_skips_refine(self, cfg, tmp_path):
        """refine_enabled=False → refiner 不被调用(无论质量)。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        refiner = StubRefiner()

        audio = tmp_path / "v.wav"
        audio.write_text("x")

        cfg.quality_check.refine_enabled = False
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("text " * 50, "whisper", audio)},
            refiner=refiner,
        )
        await agent.run([make_task("BV1", "pass vid")])

        assert len(refiner.calls) == 0

    async def test_refiner_failure_falls_back_to_original(self, cfg, tmp_path):
        """refiner.refine 抛错 → 主流程不中断,用原文落盘。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        refiner = StubRefiner(raise_on_call=True)

        audio = tmp_path / "v.wav"
        audio.write_text("x")
        original_text = "original text content " * 30

        cfg.quality_check.refine_enabled = True
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": (original_text, "whisper", audio)},
            refiner=refiner,
        )
        stats = await agent.run([make_task("BV1", "pass vid")])

        # refiner 抛错不阻塞主流程
        assert stats["passed"] == 1
        assert stats["failed"] == 0
        # transcribed 文件仍落盘(用原文)
        transcripts = list((cfg.logging.log_dir / "transcribed").rglob("transcripts/*.txt"))
        assert len(transcripts) == 1
        assert original_text in transcripts[0].read_text()


# ---------------- v3.2 音频删除显式化(FR-3.7) ----------------


class TestAudioCleanupExplicit:
    """v3.2:audio_path.unlink() 由 main.py 显式调,在 quality.passed=True 之后。

    改前:streaming.py.cleanup() 内部清理,quality_fail 时音频也删,无 retry 源
    改后:quality.pass → unlink;quality.fail → 保留;删失败 → log warning + 继续
    """

    async def test_quality_pass_unlinks_audio(self, cfg, tmp_path):
        """quality.passed=True + audio 存在 → audio 删了。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        audio = tmp_path / "v.wav"
        audio.write_bytes(b"audio data")
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("text " * 50, "whisper", audio)},
        )
        await agent.run([make_task("BV1", "pass vid")])

        assert not audio.exists()

    async def test_quality_fail_keeps_audio(self, cfg, tmp_path):
        """quality.passed=False + audio 存在 → audio 保留(供 retry / 排查)。"""
        checker = StubChecker(passed=False, score=40)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        audio = tmp_path / "fail.wav"
        audio.write_bytes(b"audio data")
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("烂字幕 " * 30, "whisper", audio)},
        )
        await agent.run([make_task("BV1", "fail vid")])

        assert audio.exists()  # 保留!

    async def test_no_audio_path_skips_unlink(self, cfg, tmp_path):
        """audio_path=None(走官方字幕,无 audio)→ 不调 unlink,不抛错。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        # None 表示无 audio(官方/插件字幕)
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("官方字幕 " * 30, "api", None)},
        )
        # 不应抛错
        stats = await agent.run([make_task("BV1", "official vid")])
        assert stats["passed"] == 1

    async def test_unlink_failure_does_not_crash(self, cfg, tmp_path, monkeypatch):
        """audio_path.unlink() 抛错(权限/已被删)→ log warning + 主流程继续。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()

        audio = tmp_path / "v.wav"
        audio.write_bytes(b"x")
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("text " * 50, "whisper", audio)},
        )

        # 强制 unlink 抛错(模拟文件被其他进程锁住)
        def fake_unlink(*args, **kwargs):
            raise PermissionError("locked by another process")

        monkeypatch.setattr("pathlib.Path.unlink", fake_unlink)

        # 不应抛错
        stats = await agent.run([make_task("BV1", "lock vid")])
        assert stats["passed"] == 1  # 流程仍继续


# ---------------- v3.2 截图关键路径(FR-2.28) ----------------


class StubScreenshotController:
    """v3.2.1:ScreenshotPhaseController 的 async stub,可控制抛错。"""

    def __init__(self, *, raise_on_phase_a: bool = False, raise_on_phase_c: bool = False):
        self.raise_on_phase_a = raise_on_phase_a
        self.raise_on_phase_c = raise_on_phase_c
        self.phase_a_calls: list[dict[str, Any]] = []
        self.phase_c_calls: list[dict[str, Any]] = []

    async def phase_a_start(self, *, url: str, video_id: str, title: str, duration_sec: int):
        self.phase_a_calls.append({
            "url": url, "video_id": video_id,
            "title": title, "duration_sec": duration_sec,
        })
        if self.raise_on_phase_a:
            raise RuntimeError("screencapture failed (no permission)")

    async def phase_c_end(self, *, url: str, video_id: str, title: str, duration_sec: int):
        self.phase_c_calls.append({
            "url": url, "video_id": video_id,
            "title": title, "duration_sec": duration_sec,
        })
        if self.raise_on_phase_c:
            raise RuntimeError("screencapture failed (no permission)")


class StubBrowserDriver:
    """v3.2:BrowserDriver 的 stub,支持 page 槽复用。"""

    def __init__(self, *, raise_on_connect: bool = False):
        self.raise_on_connect = raise_on_connect
        self.connected = False
        self.page_goto_calls: list[str] = []
        self._page = None

    def connect(self):
        if self.raise_on_connect:
            raise RuntimeError("chrome not running on debug port 9222")
        self.connected = True

    def disconnect(self):
        self.connected = False

    def new_background_page(self):
        if not self.connected:
            raise RuntimeError("not connected")
        self._page = object()  # 占位
        return self._page

    def goto(self, url: str):
        self.page_goto_calls.append(url)


class TestScreenshotCriticalPath:
    """v3.2 (FR-2.28):截图升级为关键路径,Phase A 失败 → 跳过视频。

    设计:
    - screenshot_controller / browser_driver 可选(None = 不启用截图)
    - Phase A 失败 → _process_one 返回 None(决策 A)
    - Phase C 失败 → log warning + 继续(不阻塞)
    - chrome_session.enabled=False 或 driver connect 失败 → 跳过截图步骤
    """

    async def test_phase_a_fail_skips_video(self, cfg, tmp_path):
        """Phase A 截图失败 → 跳过本视频,字幕 / 转写都不跑。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        driver = StubBrowserDriver()
        screenshot = StubScreenshotController(raise_on_phase_a=True)

        cfg.chrome_session.enabled = True
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("text " * 50, "whisper", None)},
            browser_driver=driver, screenshot_controller=screenshot,
        )
        stats = await agent.run([make_task("BV1", "screenshot fail vid")])

        # Phase A 失败 → 跳过本视频
        assert stats["processed"] == 1
        assert stats["passed"] == 0
        assert stats["failed"] == 1
        # checker 没被调(字幕 / 转写都没跑)
        assert len(checker.calls) == 0
        # Phase A 被调 1 次
        assert len(screenshot.phase_a_calls) == 1
        # Phase C 没被调(因为已经失败跳过了)
        assert len(screenshot.phase_c_calls) == 0

    async def test_phase_a_ok_proceeds_to_subtitle(self, cfg, tmp_path):
        """Phase A 成功 → 继续字幕 + 质量 + 落盘。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        driver = StubBrowserDriver()
        screenshot = StubScreenshotController()

        cfg.chrome_session.enabled = True
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("text " * 50, "whisper", None)},
            browser_driver=driver, screenshot_controller=screenshot,
        )
        stats = await agent.run([make_task("BV1", "ok vid")])

        assert stats["passed"] == 1
        # Phase A + Phase C 各调 1 次
        assert len(screenshot.phase_a_calls) == 1
        assert len(screenshot.phase_c_calls) == 1

    async def test_phase_c_fail_logs_warning_continues(self, cfg, tmp_path):
        """Phase C 截图失败 → log warning,流程继续(passed=1)。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        driver = StubBrowserDriver()
        screenshot = StubScreenshotController(raise_on_phase_c=True)

        cfg.chrome_session.enabled = True
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("text " * 50, "whisper", None)},
            browser_driver=driver, screenshot_controller=screenshot,
        )
        stats = await agent.run([make_task("BV1", "phase c fail vid")])

        # Phase C 失败不阻塞
        assert stats["passed"] == 1

    async def test_chrome_session_disabled_skips_screenshot(self, cfg, tmp_path):
        """chrome_session.enabled=False → 不调 Phase A/C(无 Chrome 模式)。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        driver = StubBrowserDriver()
        screenshot = StubScreenshotController()

        cfg.chrome_session.enabled = False  # 关键
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("text " * 50, "whisper", None)},
            browser_driver=driver, screenshot_controller=screenshot,
        )
        stats = await agent.run([make_task("BV1", "no chrome vid")])

        # 流程正常,passed=1
        assert stats["passed"] == 1
        # 截图步骤被跳过
        assert len(screenshot.phase_a_calls) == 0
        assert len(screenshot.phase_c_calls) == 0

    async def test_chrome_connect_fail_continues_without_screenshot(self, cfg, tmp_path):
        """Chrome CDP 连不上(端口未监听)→ 降级无截图模式继续。"""
        checker = StubChecker(passed=True, score=85)
        notifier = StubNotifier()
        summarizer = StubSummarizer()
        driver = StubBrowserDriver(raise_on_connect=True)
        screenshot = StubScreenshotController()

        cfg.chrome_session.enabled = True
        agent, _fetch, _process = make_agent_and_pair(
            cfg, checker=checker, notifier=notifier, summarizer=summarizer,
            mapping={"BV1": ("text " * 50, "whisper", None)},
            browser_driver=driver, screenshot_controller=screenshot,
        )
        # 不应抛错
        stats = await agent.run([make_task("BV1", "no chrome cd vid")])

        # 降级处理
        assert stats["passed"] == 1
        # 截图步骤全跳过
        assert len(screenshot.phase_a_calls) == 0


# ---------------- v3.2 streaming.py 不暴露 cleanup(FR-3.7) ----------------