"""VideoSummarizer(SSOT: requirements.md FR-2.15d,2026-09-10)。

长视频 Refiner 长度超限跳过云端清理时,调 VideoSummarizer 生成
200-300 字单视频摘要,落盘 <id>.summary.txt。

触发条件:cleaned_text 长度 > config.quality_check.refine_max_chars(默认 6000)。
低于阈值 → 不调 LLM,返回空 SummaryResult(便于调用方判断要不要落盘)。

为什么不在 Refiner 里做:
- Refiner 是"清理"(preserve original + 修正),与"压缩"语义不同
- Refiner 输入是 transcript,摘要输入是 cleaned_text(更干净,压缩效果更好)
- 长视频同时保留 cleaned.txt(全文本)+ .summary.txt(摘要),职责清晰
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vla.config import QualityCheckConfig, VLAConfig, WhisperConfig
from vla.models import SummaryResult
from vla.summary.video_summarizer import VideoSummarizer, sandwich_sample


# ---------------- Fixture ----------------


class FakeLLM:
    """最小可调 LLM stub — 可指定响应文本 / max_tokens 截断模拟 / 失败注入。"""

    def __init__(
        self,
        response: str = '{"summary_text": "这是一个测试摘要,约 250 字。"}',
        raise_exc: Exception | None = None,
        truncate_at: int | None = None,
    ) -> None:
        self.response = response
        self.raise_exc = raise_exc
        self.truncate_at = truncate_at
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, *, max_tokens: int, **kwargs: Any) -> str:
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens, **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.truncate_at is not None:
            return self.response[: self.truncate_at]
        return self.response


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    """配置:refine_max_chars=6000,summarize 字数 200-300。"""
    return VLAConfig(
        storage={"tmp_dir": str(tmp_path), "auto_cleanup_on_pass": True},
        whisper={"model": "small", "language": "zh", "segment_seconds": 30,
                 "compute_type": "int8"},
        video_source={"prefer_download": True,
                      "download": {"format": "best"},
                      "record": {"enabled": False, "screen_index": 0, "fps": 30,
                                 "crf": 28, "audio_input": "0",
                                 "preset": "ultrafast"}},
        quality_check=QualityCheckConfig(
            enabled=True, model="gpt-4o-mini",
            min_score_to_pass=50, min_char_per_second=0.5, max_char_per_second=20.0,
            refine_enabled=True, refine_max_chars=6000, refine_max_output_tokens=4000,
        ),
        summary={"model": "gpt-4o-mini", "target_words_min": 500,
                 "target_words_max": 800, "notes_file": "./notes/v.md",
                 "cross_video_dedup": True, "trigger_mode": "quota",
                 "notes_section_header": "## x"},
        quota={"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        history={"file": "./logs/history.jsonl"},
        logging={"log_dir": str(tmp_path / "logs"), "notify_on_fail": False,
                 "log_alert_threshold": 50, "log_alert_enabled": True},
        audio={"downloads_dir": str(tmp_path / "downloads")},
        llm_client={"provider": "minimax", "api_key_env": "OPENAI_API_KEY",
                    "base_url_env": "OPENAI_BASE_URL"},
        llm={"refine_model": "x", "quality_model": "x", "summary_model": "x"},
        puppeteer={"debugging_port": 9222, "cdp_host": "localhost"},
        platforms={"bilibili": {"enabled": True, "match_hosts": ["bilibili.com"]},
                   "internal_site": {"enabled": False, "match_hosts": []}},
        chrome_session={"enabled": False, "debug_port": 9222},
        audio_dl=None,
    )


@pytest.fixture
def long_text() -> str:
    """7000 字 > refine_max_chars=6000 → 触发摘要。"""
    # 用真实感的句子循环,模拟长视频
    base = (
        "这一节我们详细讨论了命令行参数的概念,包括如何定义参数、如何在程序中"
        "接收参数、以及如何处理边界情况。我们看到了 Java 中 args 数组的使用方法,"
        "以及 String[] 类型转换的注意事项。然后我们分析了 main 方法的签名,"
        "包括 public static void 这几个关键字的含义。最后我们演示了一个完整的"
        "Hello World 程序如何在命令行接收用户的输入参数并打印出来。"
    )
    # 重复直到 > 7000 字
    text = ""
    while len(text) < 7000:
        text += base
    return text


@pytest.fixture
def short_text() -> str:
    """1000 字 < refine_max_chars=6000 → 不触发摘要。"""
    return "大家好,这一节我们讲解命令行参数。" * 30  # ~720 字


# ---------------- 测试 ----------------


class TestTrigger:
    """触发条件:cleaned_text 长度 vs refine_max_chars。"""

    def test_long_text_triggers_summary(self, cfg: VLAConfig, long_text: str) -> None:
        """7000 字 > 6000 → 调 LLM,生成 SummaryResult。"""
        llm = FakeLLM(response='{"summary_text": "本节讲解了命令行参数的使用方法。"}')
        summarizer = VideoSummarizer(cfg, llm)

        result = summarizer.summarize_one(long_text, title="命令行参数")

        assert isinstance(result, SummaryResult)
        assert result.summary_text != ""  # 有内容
        assert len(llm.calls) == 1  # 调了 LLM

    def test_short_text_skips_llm(self, cfg: VLAConfig, short_text: str) -> None:
        """1000 字 < 6000 → 不调 LLM,返回空 SummaryResult。"""
        llm = FakeLLM()
        summarizer = VideoSummarizer(cfg, llm)

        result = summarizer.summarize_one(short_text, title="短视频")

        assert isinstance(result, SummaryResult)
        assert result.summary_text == ""  # 空文本 = 没生成
        assert result.notes == ""  # 没调 LLM 也没 notes
        assert len(llm.calls) == 0  # 没调 LLM

    def test_at_threshold_skips_llm(self, cfg: VLAConfig) -> None:
        """恰好 = refine_max_chars(6000)→ 不调 LLM(> 不包含 =)。"""
        text = "中" * 6000
        llm = FakeLLM()
        summarizer = VideoSummarizer(cfg, llm)

        result = summarizer.summarize_one(text, title="边界")

        assert result.summary_text == ""
        assert len(llm.calls) == 0

    def test_just_over_threshold_triggers(self, cfg: VLAConfig) -> None:
        """6001 字 > 6000 → 调 LLM。"""
        text = "中" * 6001
        llm = FakeLLM(response='{"summary_text": "摘要。"}')
        summarizer = VideoSummarizer(cfg, llm)

        result = summarizer.summarize_one(text, title="刚好超")

        assert result.summary_text != ""
        assert len(llm.calls) == 1


class TestPrompt:
    """Prompt 设计:字数控制 + 内容要求。"""

    def test_prompt_specifies_word_count(self, cfg: VLAConfig, long_text: str) -> None:
        """Prompt 必须明确 200-300 字。"""
        llm = FakeLLM(response='{"summary_text": "x"}')
        summarizer = VideoSummarizer(cfg, llm)
        summarizer.summarize_one(long_text, title="t")

        prompt = llm.calls[0]["prompt"]
        assert "200" in prompt and "300" in prompt  # 字数范围

    def test_prompt_includes_title_and_text(self, cfg: VLAConfig, long_text: str) -> None:
        """Prompt 包含视频标题 + 待摘要文本。"""
        llm = FakeLLM(response='{"summary_text": "x"}')
        summarizer = VideoSummarizer(cfg, llm)
        summarizer.summarize_one(long_text, title="命令行参数详解")

        prompt = llm.calls[0]["prompt"]
        assert "命令行参数详解" in prompt
        assert long_text[:200] in prompt  # 至少包含文本开头

    def test_prompt_truncates_very_long_input(self, cfg: VLAConfig) -> None:
        """超长输入(20000 字)在送 LLM 前三明治抽样,避免爆 token。"""
        text = "中" * 20000
        llm = FakeLLM(response='{"summary_text": "x"}')
        summarizer = VideoSummarizer(cfg, llm)

        # 强制触发(超过阈值太多 → 三明治抽样到可输入 LLM 的大小)
        summarizer.summarize_one(text, title="超长视频")

        # 输入给 LLM 的 prompt 长度应远小于原始 20000 字
        prompt_len = len(llm.calls[0]["prompt"])
        assert prompt_len < 15000  # 抽样了一些

        # 三明治抽样的特征标记应在 prompt 中
        prompt = llm.calls[0]["prompt"]
        assert "[..." in prompt  # 中间省略标记


class TestSandwich:
    """三明治抽样(head + mid + tail)替代纯截断(SSOT: 2026-09-10)。

    视频字幕信息密度不均:开头导入 / 中间口水话多 / 结尾结论。
    三明治覆盖关键位置,一次 LLM call。
    """

    def test_short_text_unchanged(self) -> None:
        """≤ _PROMPT_MAX_CHARS(12000) → 整篇保留,不抽样。"""
        text = "中" * 10000  # < 12000
        sampled = sandwich_sample(text)
        assert sampled == text

    def test_exactly_at_budget_unchanged(self) -> None:
        """恰好 = 12000 → 不抽样(边界检查)。"""
        text = "中" * 12000
        sampled = sandwich_sample(text)
        assert sampled == text

    def test_just_over_budget_uses_sandwich(self) -> None:
        """12001 → 抽样(省略标记出现)。sandwich 总长会略 > 原长(多了省略标记),
        所以不判 len(sampled) < len(text),只判采样启动信号。"""
        text = "中" * 12001
        sampled = sandwich_sample(text)
        assert "[..." in sampled  # 含省略标记 = 已抽样
        assert "省略 1 字" in sampled  # 12001 - 12000 = 1 字省略

    def test_long_text_preserves_head(self) -> None:
        """长文本抽样后保留开头内容。"""
        text = "HEAD_START" + "中" * 30000 + "TAIL_END"
        sampled = sandwich_sample(text)
        assert sampled.startswith("HEAD_START")

    def test_long_text_preserves_tail(self) -> None:
        """长文本抽样后保留结尾内容。"""
        text = "HEAD_START" + "中" * 30000 + "TAIL_END"
        sampled = sandwich_sample(text)
        assert sampled.endswith("TAIL_END")

    def test_long_text_includes_middle_window(self) -> None:
        """长文本抽样后中间窗口保留(50000 → 中间 4000 字窗口)。"""
        # 中间放唯一标识符,验证中间窗口确实取到了那一段
        middle_marker = "中" * 1000 + "MIDDLE_UNIQUE_MARKER_XYZ" + "中" * 1000
        text = "HEAD" + "中" * 15000 + middle_marker + "中" * 15000 + "TAIL"
        sampled = sandwich_sample(text)
        assert "MIDDLE_UNIQUE_MARKER_XYZ" in sampled

    def test_long_text_total_chars_under_budget(self) -> None:
        """抽样后总长(head + mid + tail + 2 个 separator) < 13000。"""
        text = "中" * 50000
        sampled = sandwich_sample(text)
        # head(4000) + mid(4000) + tail(4000) + 2 个 separator(~50)
        assert len(sampled) < 12100  # 实际 < 12050

    def test_separator_includes_omitted_count(self) -> None:
        """省略标记里带"省略 N 字"字样,帮 LLM 知道原文多长。"""
        text = "中" * 20000  # 省略 20000 - 4000 - 4000 - 4000 = 8000 字
        sampled = sandwich_sample(text)
        assert "省略 8000 字" in sampled

    def test_empty_string_unchanged(self) -> None:
        """空串 → 空串(不抛错)。"""
        assert sandwich_sample("") == ""

    def test_real_spike_text_35513_chars(self) -> None:
        """复现 spike 真实场景:35513 字 bill-jc transcript → 抽样后 < 12100。"""
        # 真实长度的烟雾测试
        text = (
            "这一节我们讲用户管理模块的实现,"
            "那么实现了主要包括了一些注册登录啊,编辑用户信息界面啊,"
        ) * 1000  # ≈ 35500 字
        sampled = sandwich_sample(text)
        assert len(sampled) < 12100
        # 抽样应包含至少一段完整原文(sandwich mid 窗口 4000 字足以容下)
        assert "用户管理模块" in sampled


class TestFailureModes:
    """失败 fallback:不抛错,返回空 SummaryResult + notes 说明。"""

    def test_llm_call_failure(self, cfg: VLAConfig, long_text: str) -> None:
        """LLM 抛异常 → fallback 空 SummaryResult + notes 记录。"""
        llm = FakeLLM(raise_exc=RuntimeError("API quota exceeded"))
        summarizer = VideoSummarizer(cfg, llm)

        result = summarizer.summarize_one(long_text, title="t")

        assert result.summary_text == ""
        assert "RuntimeError" in result.notes or "quota" in result.notes.lower()

    def test_invalid_json_response(self, cfg: VLAConfig, long_text: str) -> None:
        """LLM 返回非 JSON → fallback 空 + notes 解析失败。"""
        llm = FakeLLM(response="这是 LLM 的自由文本回复,没有 JSON。")
        summarizer = VideoSummarizer(cfg, llm)

        result = summarizer.summarize_one(long_text, title="t")

        assert result.summary_text == ""
        assert "解析" in result.notes or "JSON" in result.notes or "失败" in result.notes

    def test_empty_summary_text(self, cfg: VLAConfig, long_text: str) -> None:
        """LLM 返回 summary_text="" → fallback 空 + notes 说明。"""
        llm = FakeLLM(response='{"summary_text": ""}')
        summarizer = VideoSummarizer(cfg, llm)

        result = summarizer.summarize_one(long_text, title="t")

        assert result.summary_text == ""

    def test_no_llm_injected_raises(self, cfg: VLAConfig, long_text: str) -> None:
        """没注入 LLM + 长文本 → RuntimeError(同 QualityChecker 风格)。"""
        summarizer = VideoSummarizer(cfg, llm=None)

        with pytest.raises(RuntimeError, match="LLM"):
            summarizer.summarize_one(long_text, title="t")


class TestWriteToFile:
    """落盘 helper:.summary.txt 格式。"""

    def test_write_summary_file(self, cfg: VLAConfig, tmp_path: Path) -> None:
        """落盘:文件存在 + 内容是 summary_text + 元数据头。"""
        llm = FakeLLM(response='{"summary_text": "本节讲解命令行参数。"}')
        summarizer = VideoSummarizer(cfg, llm)
        result = summarizer.summarize_one("中" * 7000, title="命令行参数")

        out_path = tmp_path / "video1.summary.txt"
        written = summarizer.write_summary(out_path, result)

        assert written == out_path
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert "本节讲解命令行参数" in content

    def test_empty_result_writes_nothing(self, cfg: VLAConfig, tmp_path: Path) -> None:
        """空 SummaryResult(没生成摘要)→ 不写文件。"""
        summarizer = VideoSummarizer(cfg, llm=None)
        result = SummaryResult(summary_text="", notes="", model="x")
        out_path = tmp_path / "video2.summary.txt"

        written = summarizer.write_summary(out_path, result)

        assert written is None
        assert not out_path.exists()
