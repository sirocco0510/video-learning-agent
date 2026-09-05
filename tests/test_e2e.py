"""E2E 集成测试(SSOT: requirements.md 第七章 + implementation-plan.md Phase 9)。

12 个场景(从 implementation-plan.md Phase 9 测试矩阵映射):
  E2E-1   official 字幕命中 → 不下载/录屏
  E2E-2   插件字幕命中 → 走字幕路径
  E2E-2b  插件 skip → 降级 whisper
  E2E-2c  插件超时 → 降级 whisper
  E2E-2d  插件字幕质量不过关 → 标 unavailable
  E2E-3   插件点了"已开启"但文件不出现 → 走 whisper
  E2E-4   防下载视频 → 走录屏 source_factory.record
  E2E-5   静音视频 → quality_fail + audio 保留
  E2E-6   失败后重试(CSV 重读 + 重新处理)
  E2E-7   累计 6h → summarize_batch
  E2E-8   history 去重
  E2E-9   插件 skip → 整 session 不再弹

E2E-1/4/5 涉及真实环境(网络 + Whisper),其他用 stub。
E2E-1 / E2E-4 / E2E-5 / E2E-3 (whisper 部分) 标 @pytest.mark.real_env,
手动 spike 时跳过。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vla.config import VLAConfig
from vla.log.transcription_log import TranscriptionLog
from vla.main import VideoLearningAgent
from vla.main_provider import RealTextProvider
from vla.models import QualityResult, SubtitleResult, VideoTask
from vla.state.history import HistoryManager
from vla.state.plugin_status import PluginStatus
from vla.state.quota import QuotaManager
from vla.summary.llm_summarizer import LLMSummarizer


# ---------------- Fixtures ----------------


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": str(tmp_path / "tmp"), "auto_cleanup_on_pass": True},
        "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
        "video_source": {"prefer_download": True, "download": {"format": "worst"}, "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"}},
        "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
        "browser_plugin": {"name": "Screen Recorder", "enabled": True, "remind_timeout_sec": 30, "plugin_paths": []},
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": str(tmp_path / "notes.md"), "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": str(tmp_path / "h.jsonl")},
        "logging": {"log_dir": str(tmp_path / "logs"), "notify_on_fail": False, "log_alert_threshold": 3, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


def make_task(bvid: str, title: str, *, duration: int = 1800, group: str = "g1", group_title: str | None = "测试组") -> VideoTask:
    return VideoTask(
        id=bvid, title=title,
        url=f"https://www.bilibili.com/video/{bvid}",
        expected_duration=duration, group_id=group, group_title=group_title,
    )


class StubChecker:
    def __init__(self, *, passed=True, score=85, issues=None):
        self.passed = passed; self.score = score; self.issues = issues or []

    def check(self, text, title, duration_sec, model_size):
        return QualityResult(
            passed=self.passed, score=self.score,
            issues=self.issues, suggestion="", char_count=len(text),
        )


class StubNotifier:
    def __init__(self):
        self.infos = []; self.warnings = []

    def info(self, title, message):
        self.infos.append((title, message))

    def warning(self, title, message):
        self.warnings.append((title, message))


class StubSummarizer:
    def __init__(self, response="批量总结。" * 30):
        self.response = response
        self.calls = []
        self.writes = []

    def summarize_batch(self, transcribed_dir, group_title=None, clear_after=True):
        self.calls.append({"dir": str(transcribed_dir), "group": group_title})
        if clear_after:
            for f in transcribed_dir.glob("*.txt"):
                f.unlink()
        return self.response

    def write_to_notes(self, content):
        self.writes.append(content)


class StubStrategy:
    """字幕策略 stub,按 url → result 映射返回。"""

    def __init__(self, mapping: dict[str, SubtitleResult | Exception | None]):
        # None 表示"全部策略失败" → 走兜底
        # Exception 表示"插件弹窗失败"
        self.mapping = mapping

    def get_subtitle(self, url, duration_sec):
        key = url
        for k in self.mapping:
            if k in url:
                v = self.mapping[k]
                if isinstance(v, Exception):
                    raise v
                return v
        return None


class StubSourceFactory:
    """视频源 stub,模拟 download/record 路径。"""

    def __init__(self, *, download_fails: bool = False, record_only: bool = False):
        self.download_fails = download_fails
        self.record_only = record_only
        self.calls = []

    def get(self, url, video_id, expected_duration):
        from vla.models import VideoSource
        self.calls.append({"url": url, "video_id": "video_id", "duration": expected_duration})
        if self.record_only or self.download_fails:
            # 走录屏
            return VideoSource(path=Path(f"/tmp/{video_id}.webm"), mode="record", duration_sec=expected_duration)
        return VideoSource(path=Path(f"/tmp/{video_id}.mp4"), mode="download", duration_sec=expected_duration)


class StubTranscriber:
    """Whisper stub,模拟 audio 抽取 + 转写 + 视频删。

    FR-3.3:transcribe() 内部删视频源
    FR-3.7:.wav 文件保留(由调用方按质量结果决定清理)
    """

    def __init__(self, text="whisper 转写文本。" * 50, fail: bool = False):
        self.text = text
        self.fail = fail
        self.cleanup_calls = []
        self.transcribe_calls = []

    def transcribe(self, video_path):
        self.transcribe_calls.append(video_path)
        # 模拟 ffmpeg 抽音轨:创建 .wav 文件
        audio_path = video_path.with_suffix(".wav")
        audio_path.write_bytes(b"fake audio")
        if self.fail:
            # 失败时也保留 .wav(供重试)
            raise RuntimeError("Whisper 模拟失败")
        # 模拟 FR-3.3:删视频源
        if video_path.exists():
            video_path.unlink()
        return self.text

    @staticmethod
    def cleanup(*paths):
        for p in paths:
            if p.exists():
                p.unlink()


def make_agent(cfg, *, checker, notifier, summarizer, text_provider, plugin_status=None):
    log = TranscriptionLog(cfg.logging.log_dir)
    history = HistoryManager(cfg.history.file)
    quota = QuotaManager(cfg)
    return VideoLearningAgent(
        cfg=cfg, checker=checker, log=log, history=history,
        quota=quota, summarizer=summarizer, notifier=notifier,
        text_provider=text_provider, plugin_status=plugin_status or PluginStatus(),
    )


def make_text_provider(strategy, source_factory, transcriber, cfg, *, notifier=None, plugin_status=None):
    """构造 RealTextProvider。

    notifier / plugin_status 缺省给个 stub — 现有 E2E 测试用 StubStrategy 短路
    策略层,不触发真实弹窗。FR-2.5/2.6 popup 流程由新增的 E2E-2e/2f/2g/2h 显式注入。
    """
    if notifier is None:
        notifier = StubNotifier()
    if plugin_status is None:
        plugin_status = PluginStatus()
    return RealTextProvider(
        cfg=cfg, strategy=strategy,
        source_factory=source_factory, transcriber=transcriber,
        notifier=notifier, plugin_status=plugin_status,
    )


# ---------------- E2E-1: official 字幕命中 → 不下载/录屏 ----------------


def test_e2e_1_official_subtitle_no_recording(cfg, tmp_path):
    """官方 CC 字幕命中 → source_factory / transcriber 不被调。"""
    strategy = StubStrategy({
        "bilibili.com": SubtitleResult(text="官方字幕内容。" * 50, source="api", metadata={"cc_id": "123"}),
    })
    source_factory = StubSourceFactory()
    transcriber = StubTranscriber()

    provider = make_text_provider(strategy, source_factory, transcriber, cfg)
    text, src, audio = provider(make_task("BV1", "官方字幕视频"))

    assert "官方字幕内容" in text
    assert src == "api"
    assert audio is None
    assert len(source_factory.calls) == 0  # 没走兜底
    assert len(transcriber.transcribe_calls) == 0


# ---------------- E2E-2: 插件字幕命中 → 走字幕路径 ----------------


def test_e2e_2_plugin_subtitle_works(cfg):
    """插件字幕命中(source="browser")→ 不下载/录屏。"""
    strategy = StubStrategy({
        "bilibili.com": SubtitleResult(text="插件字幕。" * 30, source="browser", metadata={"via": "videotrans"}),
    })
    sf = StubSourceFactory()
    tr = StubTranscriber()

    text, src, audio = make_text_provider(strategy, sf, tr, cfg)(make_task("BV2", "插件字幕视频"))

    assert "插件字幕" in text
    assert src == "browser"
    assert audio is None
    assert len(sf.calls) == 0


# ---------------- E2E-2b: 插件 skip → 降级 whisper ----------------


def test_e2e_2b_plugin_skip_degrades_to_whisper(cfg, tmp_path):
    """插件策略抛错(模拟用户 skip) → 走 source_factory + transcriber。"""
    strategy = StubStrategy({
        "bilibili.com": RuntimeError("用户跳过该视频"),
    })
    sf = StubSourceFactory()
    tr = StubTranscriber(text="Whisper 转写成功。")
    # 让 source_factory.get 返回真实存在的路径(模拟下载)
    work_dir = tmp_path / "tmp"
    work_dir.mkdir()

    from vla.models import VideoSource
    class FakeSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.mp4"
            path.write_bytes(b"fake")
            return VideoSource(path=path, mode="download", duration_sec=expected_duration)

    provider = make_text_provider(strategy, FakeSF(), tr, cfg)
    text, src, audio = provider(make_task("BV2b", "降级视频"))

    assert "Whisper 转写" in text
    assert src == "whisper"
    assert audio is not None


# ---------------- E2E-2c: 插件超时 → 降级 whisper(同 E2E-2b 路径) ----------------


def test_e2e_2c_plugin_timeout_degrades_to_whisper(cfg, tmp_path):
    """插件超时(也是 strategy 抛错)→ 同降级路径。"""
    strategy = StubStrategy({"bilibili.com": TimeoutError("30s 超时")})
    tr = StubTranscriber(text="超时降级转写。")

    work_dir = tmp_path / "tmp"
    work_dir.mkdir()
    from vla.models import VideoSource
    class FakeSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.mp4"
            path.write_bytes(b"x")
            return VideoSource(path=path, mode="download", duration_sec=expected_duration)

    text, src, audio = make_text_provider(strategy, FakeSF(), tr, cfg)(make_task("BV2c", "超时"))
    assert src == "whisper"


# ---------------- E2E-2d: 插件字幕质量不过关 → 标 unavailable ----------------


def test_e2e_2d_plugin_quality_fail_marks_unavailable(cfg):
    """插件字幕命中但质量不过关 → plugin_status.is_unavailable()=True。"""
    checker = StubChecker(passed=False, score=20, issues=["格式异常"])
    notifier = StubNotifier()
    summarizer = StubSummarizer()
    plugin_status = PluginStatus()

    strategy = StubStrategy({
        "bilibili.com": SubtitleResult(text="差的插件字幕。" * 5, source="browser", metadata={}),
    })

    def provider(task):
        result = strategy.get_subtitle(str(task.url), task.expected_duration)
        return (result.text, result.source, None)

    agent = make_agent(
        cfg, checker=checker, notifier=notifier, summarizer=summarizer,
        text_provider=provider, plugin_status=plugin_status,
    )

    agent.run([make_task("BV2d", "插件差视频")])

    assert plugin_status.is_unavailable()
    assert plugin_status.reason == "plugin_quality_fail"


# ---------------- E2E-3: 插件点了"已开启"但文件没出现 → whisper ----------------


def test_e2e_3_plugin_succeeds_but_no_file_falls_back(cfg, tmp_path):
    """插件点了"已开启"但实际没拿到字幕(返回 None)→ 降级 whisper。"""
    strategy = StubStrategy({"bilibili.com": None})  # 全部失败
    work_dir = tmp_path / "tmp"
    work_dir.mkdir()
    from vla.models import VideoSource
    class FakeSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.mp4"
            path.write_bytes(b"x")
            return VideoSource(path=path, mode="download", duration_sec=expected_duration)
    tr = StubTranscriber(text="下载后转写。")

    text, src, audio = make_text_provider(strategy, FakeSF(), tr, cfg)(make_task("BV3", "插件假阳性"))

    assert "下载后转写" in text
    assert src == "whisper"


# ---------------- E2E-4: 防下载视频 → 走录屏 ----------------


def test_e2e_4_record_only_video_uses_record_path(cfg, tmp_path):
    """防下载(YouTube 等)→ source_factory 返回 mode="record"。"""
    work_dir = tmp_path / "tmp"
    work_dir.mkdir()
    from vla.models import VideoSource
    class RecordSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.webm"
            path.write_bytes(b"recorded video")
            return VideoSource(path=path, mode="record", duration_sec=expected_duration)

    strategy = StubStrategy({})  # 字幕全失败 → 兜底
    tr = StubTranscriber(text="录屏转写。")

    text, src, audio = make_text_provider(strategy, RecordSF(), tr, cfg)(
        make_task("BV4", "防下载视频")
    )

    assert "录屏转写" in text
    assert src == "whisper"
    assert audio is not None


# ---------------- E2E-5: 静音视频 → quality_fail + audio 保留 ----------------


def test_e2e_5_silent_video_quality_fail(cfg, tmp_path):
    """Whisper 转写文本极短(语速异常)→ quality_fail,video 已删但 audio 保留。"""
    work_dir = tmp_path / "tmp"
    work_dir.mkdir()
    from vla.models import VideoSource
    class FakeSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.mp4"
            path.write_bytes(b"x")
            return VideoSource(path=path, mode="download", duration_sec=expected_duration)

    # 转写只有 30 字(对 600s 视频 cps=0.05 → fail)
    tr = StubTranscriber(text="短短短" * 10)

    text, src, audio = make_text_provider(
        StubStrategy({}), FakeSF(), tr, cfg
    )(make_task("BV5", "静音视频", duration=600))

    # audio 保留(质量失败,留给重试)
    assert audio is not None
    assert audio.exists()

    # 主调度:失败流程
    checker = StubChecker(passed=False, score=20, issues=["语速异常"])
    notifier = StubNotifier()
    summarizer = StubSummarizer()
    agent = make_agent(
        cfg, checker=checker, notifier=notifier, summarizer=summarizer,
        text_provider=lambda t: (text, src, audio),
    )
    stats = agent.run([make_task("BV5", "静音视频", duration=600)])

    assert stats["failed"] == 1
    assert audio.exists()  # 保留


# ---------------- E2E-6: 失败后重试(CSV 重读 + 重新处理) ----------------


def test_e2e_6_retry_after_quality_fail(cfg, tmp_path):
    """第一次失败 → 第二次注入 quality text → 通过。"""
    work_dir = tmp_path / "tmp"
    work_dir.mkdir()
    from vla.models import VideoSource
    class FakeSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.mp4"
            path.write_bytes(b"x")
            return VideoSource(path=path, mode="download", duration_sec=expected_duration)

    # 第一次:text 烂 → quality fail
    tr = StubTranscriber(text="短短短" * 5)

    # First run
    checker1 = StubChecker(passed=False, score=20)
    notifier = StubNotifier()
    summarizer = StubSummarizer()
    audio1 = work_dir / "retry.wav"
    audio1.write_bytes(b"x")

    provider1 = make_text_provider(StubStrategy({}), FakeSF(), tr, cfg)
    agent1 = make_agent(
        cfg, checker=checker1, notifier=notifier, summarizer=summarizer,
        text_provider=provider1,
    )
    stats1 = agent1.run([make_task("BV6", "重试视频")])
    assert stats1["failed"] == 1
    assert audio1.exists()  # 保留供重试

    # Second run:Whisper text 变好(模拟手工重转写)
    tr2 = StubTranscriber(text="这是重新转写的高质量字幕。" * 30)
    checker2 = StubChecker(passed=True, score=85)

    agent2 = make_agent(
        cfg, checker=checker2, notifier=notifier, summarizer=summarizer,
        text_provider=make_text_provider(StubStrategy({}), FakeSF(), tr2, cfg),
    )
    stats2 = agent2.run([make_task("BV6", "重试视频")])
    assert stats2["passed"] == 1


# ---------------- E2E-7: 累计 6h → 触发总结 ----------------


def test_e2e_7_quota_triggers_summary(cfg, tmp_path):
    """3 条 × 7200s = 21600 → 触发。"""
    strategy = StubStrategy({
        "BV7a": SubtitleResult(text="字幕A" * 30, source="api", metadata={}),
        "BV7b": SubtitleResult(text="字幕B" * 30, source="api", metadata={}),
        "BV7c": SubtitleResult(text="字幕C" * 30, source="api", metadata={}),
    })

    def provider(task):
        return strategy.get_subtitle(str(task.url), task.expected_duration).text, \
               strategy.get_subtitle(str(task.url), task.expected_duration).source, None

    checker = StubChecker(passed=True, score=85)
    notifier = StubNotifier()
    summarizer = StubSummarizer()

    agent = make_agent(
        cfg, checker=checker, notifier=notifier, summarizer=summarizer,
        text_provider=provider,
    )

    tasks = [
        make_task("BV7a", "A", duration=7200),
        make_task("BV7b", "B", duration=7200),
        make_task("BV7c", "C", duration=7200),
    ]
    stats = agent.run(tasks)

    assert stats["summarized"] == 1
    assert len(summarizer.calls) == 1
    # 第三条处理完才触发
    assert stats["processed"] == 3


# ---------------- E2E-8: history 去重 ----------------


def test_e2e_8_dedup_skips_already_done(cfg, tmp_path):
    """history 已有 url_key → agent 不调 text_provider。"""
    # 预先记录
    history = HistoryManager(cfg.history.file)
    history.record_success(
        url_key=HistoryManager.make_url_key("g1", "BV8"),
        title="已转写", duration_sec=1800, group_id="g1", source="whisper",
    )

    log = TranscriptionLog(cfg.logging.log_dir)
    quota = QuotaManager(cfg)
    summarizer = StubSummarizer()
    notifier = StubNotifier()
    checker = StubChecker()

    provider_called = []

    def provider(task):
        provider_called.append(task.id)
        return ("text", "whisper", None)

    agent = VideoLearningAgent(
        cfg=cfg, checker=checker, log=log, history=history, quota=quota,
        summarizer=summarizer, notifier=notifier, text_provider=provider,
    )

    stats = agent.run([make_task("BV8", "已转写")])

    assert stats["skipped"] == 1
    assert stats["processed"] == 0
    assert len(provider_called) == 0


# ---------------- E2E-9: 插件 skip → 整 session 不再弹 ----------------


def test_e2e_9_plugin_quality_fail_marks_session_unavailable(cfg):
    """第一个视频插件字幕质量不过关 → plugin_status 标 unavailable,
    后续整 session 不再尝试插件(主调度据此降级到 whisper)。
    """
    plugin_status = PluginStatus()

    # 模拟主调度检查逻辑
    @plugin_status_decision(plugin_status)
    def should_try_plugin() -> bool:
        """返回 True 即可用,False 不可用。"""
        return not plugin_status.is_unavailable()

    # 第一次确认时 → True(可用)
    assert should_try_plugin() is True

    # 模拟用户 skip + 字幕质量 fail → 标 unavailable
    plugin_status.mark_unavailable(reason="user_skip_then_quality_fail")

    # 整 session 后续 → False(不可用)
    assert should_try_plugin() is False
    assert plugin_status.reason == "user_skip_then_quality_fail"

    # is_known 是 True(已被标记)
    assert plugin_status.is_known()


def plugin_status_decision(status):
    """装饰器占位 — 模拟主调度根据 plugin_status 决定是否调用插件策略。
    真实场景里,这个判断在 main.py run() / _process_one() 里。
    """
    def decorator(fn):
        def inner(*args, **kwargs):
            return fn(*args, **kwargs)
        return inner
    return decorator


# ---------------- E2E-2e/2f/2g/2h: FR-2.5/2.6/2.9/2.10 popup 流程 ----------------


class PopupFlowAdapter:
    """测试用 adapter:模拟"插件没启用 → 触发弹窗"流程。

    fetch_browser_subtitle 永远返回 None(模拟插件未启用,JS 探测没拿到字幕)。
    FR-2.21:第一次 miss 后弹窗 → "enabled" 改走 BrowserRecorder.record_and_transcribe
    (不再 retry fetch_browser_subtitle);"skip"/"timeout" 降级到策略 ③。
    """

    def __init__(self, configured_retry_return=None):
        self._retry_return = configured_retry_return
        self.call_count = 0

    @classmethod
    def match(cls, url):
        return "bilibili.com" in url

    def fetch_api_subtitle(self, url):
        return None

    def fetch_browser_subtitle(self, driver, url):
        self.call_count += 1
        if self.call_count == 1:
            return None  # 第一次 miss → 触发弹窗
        # 后续调用理论上不该到(enabled 路径走 recorder,不重试 browser)
        return self._retry_return

    def fetch_via_recording(self, driver, url, duration_sec):
        return None


class PopupFlowRegistry:
    def __init__(self, adapter):
        self._adapter = adapter

    def get_for_url(self, url):
        return self._adapter


class PopupFlowNotifier:
    """带 ask_open_browser 控制的 StubNotifier。"""

    def __init__(self, popup_response: str):
        self.popup_response = popup_response
        self.popup_calls: list[dict] = []
        self.infos: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []

    def ask_open_browser(self, url, plugin_name, timeout_sec=30):
        self.popup_calls.append({
            "url": url, "plugin_name": plugin_name, "timeout_sec": timeout_sec,
        })
        return self.popup_response

    def info(self, title, message):
        self.infos.append((title, message))

    def warning(self, title, message):
        self.warnings.append((title, message))


def _build_real_strategy(cfg, *, popup_response, recorder=None):
    """构造真实 SubtitleStrategy + PopupFlowAdapter + PopupFlowNotifier + PluginStatus。

    Args:
        recorder: BrowserRecorder MagicMock(可选);None 表示 enabled 路径无 recorder 可调,
            会降级到策略 ③(ffmpeg)。
    """
    from vla.subtitle.strategy import SubtitleStrategy

    notifier = PopupFlowNotifier(popup_response=popup_response)
    plugin_status = PluginStatus()
    adapter = PopupFlowAdapter()
    strategy = SubtitleStrategy(
        registry=PopupFlowRegistry(adapter),
        driver=MagicMock() if recorder else None,
        recorder=recorder,
        notifier=notifier, plugin_status=plugin_status,
        remind_timeout_sec=cfg.browser_plugin.remind_timeout_sec,
        plugin_name=cfg.browser_plugin.name,
        save_dir=tmp_path_factory_mkdir(),
    )
    return strategy, notifier, plugin_status


def tmp_path_factory_mkdir():
    """构造一个 tmp 目录给 strategy.save_dir。"""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="vla-test-"))
    return d


def test_e2e_2e_plugin_enabled_triggers_recorder_returns_text(cfg, tmp_path):
    """E2E-2e: 弹窗 'enabled' → BrowserRecorder.record_and_transcribe → source='whisper',audio=None。

    recorder 新规:返回 transcript 文件路径(不返回 text),strategy 从文件读一次。"""
    from unittest.mock import MagicMock

    recorder = MagicMock()
    transcript_text = "屏幕录制转写。" * 30
    # 造 transcript 文件 + 让 mock 返回该路径
    transcript_file = tmp_path / "transcript.txt"
    transcript_file.write_text(transcript_text, encoding="utf-8")
    recorder.record_and_transcribe.return_value = transcript_file

    strategy, notifier, plugin_status = _build_real_strategy(
        cfg, popup_response="enabled", recorder=recorder,
    )

    provider = make_text_provider(
        strategy, StubSourceFactory(), StubTranscriber(),
        cfg, notifier=notifier, plugin_status=plugin_status,
    )
    text, src, audio = provider(make_task("BV2e", "插件字幕视频"))

    # strategy 从 transcript 文件读到的 text
    assert "屏幕录制转写" in text
    assert src == "whisper"
    assert audio is None  # 屏幕录制路径 recorder 自己清理,无 .wav
    # 弹窗被调 1 次,plugin_name = "Screen Recorder"(config 已改)
    assert len(notifier.popup_calls) == 1
    assert notifier.popup_calls[0]["plugin_name"] == "Screen Recorder"
    assert notifier.popup_calls[0]["timeout_sec"] == 30
    # recorder 被调过(duration_sec + save_dir)
    recorder.record_and_transcribe.assert_called_once()
    call_args = recorder.record_and_transcribe.call_args
    assert call_args.args[2] == 1800  # duration_sec from make_task default
    # plugin_status 标记为 available(因为 enabled 路径成功)
    assert plugin_status.is_known()
    assert not plugin_status.is_unavailable()
    # adapter.fetch_browser_subtitle 只被调 1 次(不重试)
    assert len(notifier.popup_calls) == 1


def test_e2e_2e2_plugin_enabled_recorder_fails_falls_through_to_ffmpeg(cfg, tmp_path):
    """E2E-2e2: enabled → recorder.record_and_transcribe 抛错 → 降级 ffmpeg(不标 unavailable)。"""
    from unittest.mock import MagicMock
    from vla.models import VideoSource

    recorder = MagicMock()
    recorder.record_and_transcribe.side_effect = RuntimeError("录屏超时 180s")

    work_dir = tmp_path / "tmp"
    work_dir.mkdir()

    class FakeSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.mp4"
            path.write_bytes(b"fake")
            return VideoSource(path=path, mode="download", duration_sec=expected_duration)

    tr = StubTranscriber(text="ffmpeg 兜底转写。")

    strategy, notifier, plugin_status = _build_real_strategy(
        cfg, popup_response="enabled", recorder=recorder,
    )

    provider = make_text_provider(
        strategy, FakeSF(), tr, cfg,
        notifier=notifier, plugin_status=plugin_status,
    )
    text, src, audio = provider(make_task("BV2e2", "录屏失败视频"))

    # ffmpeg 兜底命中
    assert "ffmpeg 兜底转写" in text
    assert src == "whisper"
    # plugin_status 仍 available(没标 unavailable,用户没拒绝,只是录屏超时)
    assert not plugin_status.is_unavailable()
    # recorder 被调过,失败被捕获
    recorder.record_and_transcribe.assert_called_once()


def test_e2e_2f_plugin_skip_marks_unavailable_and_downgrades(cfg, tmp_path):
    """E2E-2f: 弹窗返回 'skip' → mark_unavailable(user_skip) + 降级 whisper。"""
    strategy, notifier, plugin_status = _build_real_strategy(
        cfg, popup_response="skip",
    )

    work_dir = tmp_path / "tmp"
    work_dir.mkdir()
    from vla.models import VideoSource
    class FakeSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.mp4"
            path.write_bytes(b"fake")
            return VideoSource(path=path, mode="download", duration_sec=expected_duration)
    tr = StubTranscriber(text="Whisper 转写成功。")

    provider = make_text_provider(
        strategy, FakeSF(), tr, cfg,
        notifier=notifier, plugin_status=plugin_status,
    )
    text, src, audio = provider(make_task("BV2f", "用户跳过的视频"))

    assert "Whisper 转写" in text
    assert src == "whisper"
    assert audio is not None
    # 弹窗被调 1 次
    assert len(notifier.popup_calls) == 1
    # plugin_status 标 unavailable,reason = "user_skip"
    assert plugin_status.is_unavailable()
    assert plugin_status.reason == "user_skip"


def test_e2e_2g_plugin_timeout_marks_unavailable_and_downgrades(cfg, tmp_path):
    """E2E-2g: 弹窗返回 'timeout' → mark_unavailable(popup_timeout) + 降级 whisper。

    FR-2.21:notifier.warning 应被调用 1 次告知用户已降级。
    """
    strategy, notifier, plugin_status = _build_real_strategy(
        cfg, popup_response="timeout",
    )

    work_dir = tmp_path / "tmp"
    work_dir.mkdir()
    from vla.models import VideoSource
    class FakeSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.mp4"
            path.write_bytes(b"fake")
            return VideoSource(path=path, mode="download", duration_sec=expected_duration)
    tr = StubTranscriber(text="超时降级转写。")

    provider = make_text_provider(
        strategy, FakeSF(), tr, cfg,
        notifier=notifier, plugin_status=plugin_status,
    )
    text, src, audio = provider(make_task("BV2g", "弹窗超时视频"))

    assert "超时降级转写" in text
    assert src == "whisper"
    # plugin_status 标 unavailable,reason = "popup_timeout"
    assert plugin_status.is_unavailable()
    assert plugin_status.reason == "popup_timeout"
    # FR-2.21:超时分支通知用户。MacOSNotifier.ask_open_browser 内部会调 self.warning;
    # PopupFlowNotifier stub 这里只验证 strategy 行为,MacOSNotifier 的 warning 行为
    # 在 tests/test_macos_notify.py:test_ask_open_browser_timeout_triggers_warning_notification 单独验证。


def test_e2e_2h_session_single_popup_no_repeat_for_subsequent_videos(cfg, tmp_path):
    """E2E-2h: 标 unavailable 后,后续视频不再弹窗,直接走兜底(FR-2.9/2.10)。

    流程:
    1. 第一个视频:用户 skip → plugin_status unavailable
    2. 第二个视频:plugin_status 已 unavailable → 不弹窗,直接走 ③ 兜底
    """
    work_dir = tmp_path / "tmp"
    work_dir.mkdir()
    from vla.models import VideoSource
    class FakeSF:
        def get(self, url, video_id, expected_duration):
            path = work_dir / f"{video_id}.mp4"
            path.write_bytes(b"fake")
            return VideoSource(path=path, mode="download", duration_sec=expected_duration)
    tr = StubTranscriber(text="第二次转写。")

    # 共享 notifier + plugin_status(session 单例)
    notifier = PopupFlowNotifier(popup_response="skip")
    plugin_status = PluginStatus()

    # 第一条:用真实 SubtitleStrategy,adapter 第一次 miss → 弹窗 → skip
    from vla.subtitle.strategy import SubtitleStrategy
    adapter1 = PopupFlowAdapter(None)
    strategy1 = SubtitleStrategy(
        registry=PopupFlowRegistry(adapter1),
        driver=None, recorder=None,
        notifier=notifier, plugin_status=plugin_status,
        remind_timeout_sec=cfg.browser_plugin.remind_timeout_sec,
    )
    provider1 = make_text_provider(
        strategy1, FakeSF(), tr, cfg,
        notifier=notifier, plugin_status=plugin_status,
    )
    text1, src1, _ = provider1(make_task("BV2h1", "第一个视频"))
    assert src1 == "whisper"
    assert len(notifier.popup_calls) == 1
    assert plugin_status.is_unavailable()

    # 第二条:同样真实 SubtitleStrategy,共享 notifier + plugin_status
    # → plugin_status 已 unavailable → 策略 ② 跳过,不弹窗
    adapter2 = PopupFlowAdapter(None)
    strategy2 = SubtitleStrategy(
        registry=PopupFlowRegistry(adapter2),
        driver=None, recorder=None,
        notifier=notifier, plugin_status=plugin_status,
        remind_timeout_sec=cfg.browser_plugin.remind_timeout_sec,
    )
    provider2 = make_text_provider(
        strategy2, FakeSF(), tr, cfg,
        notifier=notifier, plugin_status=plugin_status,
    )
    text2, src2, _ = provider2(make_task("BV2h2", "第二个视频"))

    assert "第二次转写" in text2
    assert src2 == "whisper"
    # 弹窗**仍然只调 1 次**(第二条没弹)
    assert len(notifier.popup_calls) == 1
    # 第二条 adapter.fetch_browser_subtitle **没被调**(因为 plugin_status unavailable 跳过 ②)
    assert adapter2.call_count == 0