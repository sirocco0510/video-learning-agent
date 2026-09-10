"""SubtitleRefiner 测试(SSOT: requirements.md FR-2.15c / Level 4,2026-09-02)。

测试覆盖:
- LLM 调用参数(prompt 内容 + max_tokens + temperature)
- JSON 解析鲁棒性(纯 JSON / ```json``` 代码块 / 嵌套文字)
- 失败 fallback(LLM 抛错 / JSON 解析失败 / 空 cleaned_text)
- 长度超限保护(refine_max_chars)
- 修正项构造(Correction 列表)
- write_cleaned_transcript 落盘格式
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vla.config import VLAConfig
from vla.models import Correction, RefinementResult
from vla.quality.refiner import (
    SubtitleRefiner,
    _MAX_CHUNKS,
    _SYSTEM_PROMPT,
    _USER_PROMPT_TEMPLATE,
    _split_by_lines,
    write_cleaned_transcript,
)


# ---------------- Fixtures ----------------


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    """带 refine 字段的完整配置。"""
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
        "whisper": {
            "model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8",
        },
        "video_source": {
            "prefer_download": True,
            "download": {"format": "worst"},
            "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"},
        },
        "quality_check": {
            "enabled": True, "model": "gpt-4o-mini",
            "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0,
            "refine_enabled": True, "refine_model": None, "refine_max_chars": 6000,
        },
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./notes/v.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


@pytest.fixture
def mock_llm() -> MagicMock:
    """mock LLMClientLike.complete() 返回值。"""
    m = MagicMock()
    return m


@pytest.fixture
def refiner(cfg: VLAConfig, mock_llm: MagicMock) -> SubtitleRefiner:
    return SubtitleRefiner(cfg, llm=mock_llm)


# ---------------- 属性 ----------------


class TestProperties:
    def test_enabled_reads_config(self, cfg, mock_llm):
        cfg.quality_check.refine_enabled = True
        r = SubtitleRefiner(cfg, llm=mock_llm)
        assert r.enabled is True

    def test_enabled_false(self, cfg, mock_llm):
        cfg.quality_check.refine_enabled = False
        r = SubtitleRefiner(cfg, llm=mock_llm)
        assert r.enabled is False

    def test_model_prefers_refine_model(self, cfg, mock_llm):
        cfg.llm.refine_model = "gpt-4o"
        r = SubtitleRefiner(cfg, llm=mock_llm)
        assert r.model == "gpt-4o"

    def test_model_fallback_to_quality_model(self, cfg, mock_llm):
        # R-10:fallback 语义在 VLAConfig._migrate_legacy_llm_keys 的 pre-validator 里实现
        # (llm.refine_model 默认 = llm.quality_model);refiner 直接返回 cfg.llm.refine_model。
        cfg.llm.quality_model = "gpt-4o-mini"
        cfg.llm.refine_model = "gpt-4o-mini"
        r = SubtitleRefiner(cfg, llm=mock_llm)
        assert r.model == "gpt-4o-mini"

    def test_set_llm_late_injection(self, cfg):
        """先不传 llm,后 set_llm 注入。"""
        r = SubtitleRefiner(cfg, llm=None)
        assert r._llm is None
        m = MagicMock()
        r.set_llm(m)
        assert r._llm is m


# ---------------- refine() 主流程 ----------------


class TestRefine:
    def test_lazy_constructs_llm_when_not_injected(self, cfg, monkeypatch):
        """refine_enabled=True 且没注入 LLM → **惰性构造**,不再抛 RuntimeError。

        2026-09-10 契约变更:原 `test_missing_llm_raises` 断言"没注入 → RuntimeError",
        现与 `QualityChecker` / `VideoSummarizer` 统一为"构造期零依赖 + 类内惰性构造"。

        为什么:`build_text_provider` 在 `refine_enabled=true` 且未注入时会自动建
        `SubtitleRefiner(cfg)`(**不带 LLM**)—— `vla process --real-provider` 因此
        必崩。装配方只保证 `Xxx(cfg)`,不保证 `set_llm`,组件必须自给自足。
        """
        built: list[tuple[Any, str]] = []
        llm = MagicMock()
        llm.complete.return_value = '{"cleaned_text": "清理后的文本", "corrections": [], "notes": ""}'

        def fake_llm_client(client_cfg: Any, model: str = "") -> MagicMock:
            built.append((client_cfg, model))
            return llm

        monkeypatch.setattr("vla.quality.refiner.LLMClient", fake_llm_client)
        r = SubtitleRefiner(cfg, llm=None)

        result = r.refine("一些字幕文本", title="视频标题")

        assert result.cleaned_text == "清理后的文本"
        assert len(built) == 1, "应恰好惰性构造一次 LLMClient"
        assert built[0][0] is cfg.llm_client, "应传 cfg.llm_client"
        assert built[0][1] == r.model, "model 应与 self.model 一致"

    def test_lazy_construction_happens_once(self, cfg, monkeypatch):
        """惰性构造只做一次 —— 第二次 refine 复用已建的客户端。"""
        calls = {"n": 0}
        llm = MagicMock()
        llm.complete.return_value = '{"cleaned_text": "清理后的文本", "corrections": [], "notes": ""}'

        def fake_llm_client(client_cfg: Any, model: str = "") -> MagicMock:
            calls["n"] += 1
            return llm

        monkeypatch.setattr("vla.quality.refiner.LLMClient", fake_llm_client)
        r = SubtitleRefiner(cfg, llm=None)

        r.refine("第一段文本", title="t")
        r.refine("第二段文本", title="t")

        assert calls["n"] == 1
        assert llm.complete.call_count == 2, "两次调用都该打到同一个客户端"

    def test_injected_llm_wins_over_lazy(self, cfg, monkeypatch):
        """显式注入优先 —— 注入了就不该再惰性构造。"""

        def boom(client_cfg: Any, model: str = "") -> Any:
            raise AssertionError("注入了 LLM 却仍去构造新的")

        monkeypatch.setattr("vla.quality.refiner.LLMClient", boom)
        llm = MagicMock()
        llm.complete.return_value = '{"cleaned_text": "注入的文本", "corrections": [], "notes": ""}'
        r = SubtitleRefiner(cfg, llm=llm)

        result = r.refine("一些字幕文本", title="t")

        assert result.cleaned_text == "注入的文本"

    def test_calls_llm_with_system_and_user_prompt(self, refiner, mock_llm):
        """完整 prompt 包含 system + user 两部分。"""
        mock_llm.complete.return_value = '{"cleaned_text": "好的整理版本", "corrections": [], "notes": "无修正"}'

        refiner.refine("原始字幕文本片段", title="测试视频")

        mock_llm.complete.assert_called_once()
        call_args = mock_llm.complete.call_args
        prompt = call_args.args[0]
        # system prompt 特征
        assert "你是专业的中文(简体/繁体)字幕清理助手" in prompt
        # user prompt 特征
        assert "【视频标题】" in prompt
        assert "测试视频" in prompt
        assert "原始字幕文本片段" in prompt
        assert "请按系统指令输出 JSON" in prompt

    def test_llm_max_tokens_scales_with_input(self, refiner, mock_llm):
        """输出 token 上限 = max(refine_max_output_tokens, len(input) * 2 + 1000)。

        2026-09-10 修正:`len(text) + 1000` 这个旧启发式是按 MiniMax 的内联
        `<think>` 标定的,**不够**。实测 deepseek-flash 处理 2507 字输入需要
        **3268** completion tokens(≈1.3 token/字 —— 输出是 JSON 转义后的文本
        还带 corrections,比输入长),旧公式只给 3507,余量仅 7%,输入再长一点
        就截断。改用 ×2 留出余量。
        """
        mock_llm.complete.return_value = '{"cleaned_text": "x", "corrections": [], "notes": ""}'

        # 短文本:用配置的默认 4000
        refiner.refine("短文本测试")
        kwargs = mock_llm.complete.call_args.kwargs
        assert kwargs["max_tokens"] == 4000  # 配置 refine_max_output_tokens 默认 4000

        # 长文本:max(4000, len(text)*2 + 1000)
        long_text = "中" * 5000
        refiner.refine(long_text)
        kwargs = mock_llm.complete.call_args.kwargs
        assert kwargs["max_tokens"] == 11000  # max(4000, 5000*2 + 1000) = 11000

    def test_llm_max_tokens_covers_observed_deepseek_need(self, refiner, mock_llm):
        """回归护栏:2507 字输入(实测需 3268 tokens)必须给足额度。

        这是导致 2026-09-10 Refiner 返回空 content 的真实输入规模 ——
        旧公式给 3507(仅 7% 余量),新公式给 6014。
        """
        mock_llm.complete.return_value = '{"cleaned_text": "x", "corrections": [], "notes": ""}'

        refiner.refine("中" * 2507)

        kwargs = mock_llm.complete.call_args.kwargs
        assert kwargs["max_tokens"] > 3268, "必须覆盖实测的 3268 completion tokens"
        assert kwargs["max_tokens"] == 6014  # max(4000, 2507*2 + 1000)

    def test_llm_temperature_low(self, refiner, mock_llm):
        """temperature=0.2(低随机,稳定输出)。"""
        mock_llm.complete.return_value = '{"cleaned_text": "x", "corrections": [], "notes": ""}'

        refiner.refine("文本")

        kwargs = mock_llm.complete.call_args.kwargs
        assert kwargs["temperature"] == 0.2

    def test_char_count_in_prompt(self, refiner, mock_llm):
        """prompt 含字符数(帮 LLM 估算 token)。"""
        mock_llm.complete.return_value = '{"cleaned_text": "x", "corrections": [], "notes": ""}'

        text = "一二三四五六七八九十"  # 10 字符
        refiner.refine(text, title="t")

        prompt = mock_llm.complete.call_args.args[0]
        assert "10 字符" in prompt

    def test_default_title_placeholder(self, refiner, mock_llm):
        """title 缺省时 prompt 用 '(无标题)'。"""
        mock_llm.complete.return_value = '{"cleaned_text": "x", "corrections": [], "notes": ""}'

        refiner.refine("文本")  # 无 title

        prompt = mock_llm.complete.call_args.args[0]
        assert "(无标题)" in prompt


# ---------------- 成功路径 ----------------


class TestHappyPath:
    def test_returns_refinement_result_with_cleaned_text(self, refiner, mock_llm):
        mock_llm.complete.return_value = json_response(
            cleaned_text="整理后的文本内容",
            corrections=[
                {"original": "Deep Sake", "fixed": "Deep Seek", "reason": "根据视频标题判断,应为 Deep Seek"},
                {"original": "視頻", "fixed": "视频", "reason": "繁简统一"},
            ],
            notes="繁简统一 + 修正 2 个术语",
        )

        result = refiner.refine("Deep Sake 講解視頻內容", title="DeepSeek 教程")

        assert isinstance(result, RefinementResult)
        assert result.cleaned_text == "整理后的文本内容"
        assert result.original_text == "Deep Sake 講解視頻內容"
        assert result.notes == "繁简统一 + 修正 2 个术语"
        assert result.model == "gpt-4o-mini"  # refine_model=None fallback

    def test_corrections_parsed_to_correction_objects(self, refiner, mock_llm):
        mock_llm.complete.return_value = json_response(
            cleaned_text="x",
            corrections=[
                {"original": "a", "fixed": "b", "reason": "c"},
                {"original": "d", "fixed": "e", "reason": "f"},
            ],
        )

        result = refiner.refine("text")

        assert len(result.corrections) == 2
        assert all(isinstance(c, Correction) for c in result.corrections)
        assert result.corrections[0].original == "a"
        assert result.corrections[0].fixed == "b"
        assert result.corrections[0].reason == "c"
        assert result.corrections[1].original == "d"

    def test_empty_corrections_list(self, refiner, mock_llm):
        mock_llm.complete.return_value = '{"cleaned_text": "good", "corrections": [], "notes": ""}'

        result = refiner.refine("text")

        assert result.corrections == []

    def test_missing_corrections_field_defaults_empty(self, refiner, mock_llm):
        """LLM 漏写 corrections 字段 → 默认空 list(不报错)。"""
        mock_llm.complete.return_value = '{"cleaned_text": "good", "notes": ""}'

        result = refiner.refine("text")

        assert result.corrections == []

    def test_invalid_correction_skipped(self, refiner, mock_llm):
        """corrections 里有非 dict 项 → 跳过,不抛错。"""
        mock_llm.complete.return_value = (
            '{"cleaned_text": "x", "corrections": ['
            '{"original": "a", "fixed": "b", "reason": "c"}, '
            '"not-a-dict", '
            '{"original": "d", "fixed": "e", "reason": "f"}'
            '], "notes": ""}'
        )

        result = refiner.refine("text")

        assert len(result.corrections) == 2

    def test_uses_refine_model_when_set(self, cfg, mock_llm):
        """refine_model 显式设置 → 用 refine_model,不用 quality_model(R-10:统一从 cfg.llm.* 取值)。"""
        cfg.llm.refine_model = "gpt-4o"
        mock_llm.complete.return_value = '{"cleaned_text": "x", "corrections": [], "notes": ""}'
        r = SubtitleRefiner(cfg, llm=mock_llm)

        result = r.refine("text")

        assert result.model == "gpt-4o"


# ---------------- 失败 fallback ----------------


class TestFailureFallback:
    def test_llm_exception_returns_original_text(self, refiner, mock_llm):
        """LLM 抛错 → 返回原 text,corrections=[],notes 记录错误。"""
        mock_llm.complete.side_effect = RuntimeError("API 限流")

        result = refiner.refine("原始字幕文本", title="视频")

        assert result.cleaned_text == "原始字幕文本"
        assert result.original_text == "原始字幕文本"
        assert result.corrections == []
        assert "RuntimeError" in result.notes
        assert "API 限流" in result.notes

    def test_json_decode_error_returns_original(self, refiner, mock_llm):
        """JSON 解析失败 → fallback 原文本。"""
        mock_llm.complete.return_value = "这不是 JSON,只是文字"

        result = refiner.refine("原始文本")

        assert result.cleaned_text == "原始文本"
        assert result.corrections == []
        assert "解析失败" in result.notes

    def test_empty_cleaned_text_returns_original(self, refiner, mock_llm):
        """LLM 返回 cleaned_text 为空 → fallback 原文本。"""
        mock_llm.complete.return_value = '{"cleaned_text": "", "corrections": [], "notes": ""}'

        result = refiner.refine("原始文本")

        assert result.cleaned_text == "原始文本"
        assert "空 cleaned_text" in result.notes

    def test_missing_cleaned_text_field(self, refiner, mock_llm):
        """cleaned_text 字段缺失 → 当作空,fallback。"""
        mock_llm.complete.return_value = '{"corrections": [], "notes": ""}'

        result = refiner.refine("原始文本")

        assert result.cleaned_text == "原始文本"
        assert "空 cleaned_text" in result.notes

    def test_failure_does_not_raise(self, refiner, mock_llm):
        """任何失败都不抛错(主流程不中断)。"""
        mock_llm.complete.side_effect = ConnectionError("network down")

        # 不应该抛
        result = refiner.refine("text")

        assert result is not None


# ---------------- 长度超限保护 ----------------


class TestSplitByLines:
    """`_split_by_lines` 纯函数:按行边界切块,无损。

    转写产物是段落行结构(实测 252 行 / 平均 36 字每行),按行切**不会切断句子**
    —— 这是分块精修能成立的前提。单行本身超过 max_chars 才硬切。
    """

    def test_lossless_roundtrip(self):
        """核心不变量:"".join(chunks) == 原文。

        分块只改变"送给 LLM 的切法",**任何一块都不能被丢弃** ——
        返回文本会被 process_asset 当成 canonical 转写落盘。"""
        for text in [
            "一" * 250,
            "\n".join(["一" * 40] * 3),
            "\n".join(f"第{i}行" + "字" * 30 for i in range(20)),
            "短",
        ]:
            for max_chars in (10, 37, 100, 6000):
                chunks = _split_by_lines(text, max_chars)
                assert "".join(chunks) == text

    def test_each_chunk_within_limit(self):
        """切完每块都不超 cap(否则送 LLM 就失去意义)。"""
        text = "\n".join(f"第{i}行" + "字" * 30 for i in range(20))
        chunks = _split_by_lines(text, 100)

        assert all(len(c) <= 100 for c in chunks)

    def test_splits_on_newline_boundary(self):
        """优先在换行处切 —— 不切在句子中间。"""
        text = "\n".join(["一" * 40] * 3)   # 122 字,3 行

        chunks = _split_by_lines(text, 100)

        assert len(chunks) == 2
        # 第一块吃掉前两行(82 字),第二块是第三行
        assert chunks[0] == "\n".join(["一" * 40] * 2) + "\n"
        assert chunks[1] == "一" * 40

    def test_hard_cuts_single_oversized_line(self):
        """单行超长(无换行可依)→ 硬切,不能死循环、不能丢字。"""
        text = "一" * 250

        chunks = _split_by_lines(text, 100)

        assert [len(c) for c in chunks] == [100, 100, 50]
        assert "".join(chunks) == text

    def test_short_text_single_chunk(self):
        assert _split_by_lines("短文本", 100) == ["短文本"]

    def test_non_positive_limit_returns_whole(self):
        """cap ≤ 0 是配置错误,但不该把文本切成无穷多块。"""
        assert _split_by_lines("一二三", 0) == ["一二三"]


class TestChunkedRefine:
    """FR-2.15c 2026-09-10 二次修正:超限由"截断精修前段 + 尾部原文接回"改为**分块精修**。

    旧行为**自己制造了门控失败**:头部 6000 字被精修(繁简统一为简体),
    尾部 2911 字保持 Whisper 原生繁体 → 质量 LLM 报"后半段繁简混杂"而 fail。
    那个混杂是精修造的,不是转写造的。
    """

    # 40 字/行 × 3 行 = 122 字;在 max_chars=100 下切成 2 块
    LINES = ("一" * 40, "二" * 40, "三" * 40)
    TEXT = "\n".join(LINES)

    def test_calls_llm_once_per_chunk(self, cfg, mock_llm):
        """超限 → 每块调一次,不再"只送前段"。"""
        cfg.quality_check.refine_max_chars = 100
        mock_llm.complete.return_value = '{"cleaned_text": "X", "corrections": [], "notes": ""}'
        r = SubtitleRefiner(cfg, llm=mock_llm)

        r.refine(self.TEXT, title="t")

        assert mock_llm.complete.call_count == 2

    def test_chunks_concatenated_in_order(self, cfg, mock_llm):
        """各块结果按原顺序拼回。"""
        cfg.quality_check.refine_max_chars = 100
        mock_llm.complete.side_effect = [
            '{"cleaned_text": "块一", "corrections": [], "notes": ""}',
            '{"cleaned_text": "块二", "corrections": [], "notes": ""}',
        ]
        r = SubtitleRefiner(cfg, llm=mock_llm)

        result = r.refine(self.TEXT, title="t")

        assert result.cleaned_text == "块一块二"
        assert result.original_text == self.TEXT

    def test_failed_chunk_keeps_raw_text_others_refined(self, cfg, mock_llm):
        """某块失败 → **该块保原文**,其余块照用精修结果。

        选"保该块原文"而非"整体回退原文":已成功那块的 token 已经花掉了,
        整体回退等于白丢;而门控看到的混杂是**真实的**(该块确实没清理过)。
        """
        cfg.quality_check.refine_max_chars = 100
        mock_llm.complete.side_effect = [
            '{"cleaned_text": "块一", "corrections": [], "notes": ""}',
            RuntimeError("第二块炸了"),
        ]
        r = SubtitleRefiner(cfg, llm=mock_llm)

        result = r.refine(self.TEXT, title="t")

        assert result.cleaned_text.startswith("块一")
        assert result.cleaned_text.endswith(self.LINES[2])
        assert mock_llm.complete.call_count == 2

    def test_failed_chunk_recorded_in_notes(self, cfg, mock_llm):
        """哪几块没精修,notes 里要说清。"""
        cfg.quality_check.refine_max_chars = 100
        mock_llm.complete.side_effect = [
            '{"cleaned_text": "块一", "corrections": [], "notes": ""}',
            RuntimeError("第二块炸了"),
        ]
        r = SubtitleRefiner(cfg, llm=mock_llm)

        result = r.refine(self.TEXT, title="t")

        assert "2" in result.notes
        assert "保留原文" in result.notes

    def test_failure_does_not_raise_in_chunked_path(self, cfg, mock_llm):
        """所有块都失败也不抛错(与单块路径同契约)。"""
        cfg.quality_check.refine_max_chars = 100
        mock_llm.complete.side_effect = RuntimeError("全挂")
        r = SubtitleRefiner(cfg, llm=mock_llm)

        result = r.refine(self.TEXT, title="t")

        assert result.cleaned_text == self.TEXT

    def test_notes_from_all_chunks_aggregated(self, cfg, mock_llm):
        """每块自己的 notes 不能被吞掉。"""
        cfg.quality_check.refine_max_chars = 100
        mock_llm.complete.side_effect = [
            '{"cleaned_text": "块一", "corrections": [], "notes": "统一繁简"}',
            '{"cleaned_text": "块二", "corrections": [], "notes": "修 3 个术语"}',
        ]
        r = SubtitleRefiner(cfg, llm=mock_llm)

        result = r.refine(self.TEXT, title="t")

        assert "统一繁简" in result.notes
        assert "修 3 个术语" in result.notes

    def test_corrections_aggregated_across_chunks(self, cfg, mock_llm):
        """corrections 跨块累加。"""
        cfg.quality_check.refine_max_chars = 100
        mock_llm.complete.side_effect = [
            json.dumps({"cleaned_text": "块一", "notes": "", "corrections": [
                {"original": "PASS", "fixed": "Python", "reason": "同音字"}]}),
            json.dumps({"cleaned_text": "块二", "notes": "", "corrections": [
                {"original": "加碼", "fixed": "Java", "reason": "同音字"}]}),
        ]
        r = SubtitleRefiner(cfg, llm=mock_llm)

        result = r.refine(self.TEXT, title="t")

        assert [c.fixed for c in result.corrections] == ["Python", "Java"]

    def test_max_chunks_guard_keeps_remainder_verbatim(self, cfg, mock_llm):
        """块数超 `_MAX_CHUNKS` → 只精修前 N 块,其余保原文 + 打 warning。

        明确退化,不静默:既要在 notes 里写明,也不能丢字。
        """
        cfg.quality_check.refine_max_chars = 100
        mock_llm.complete.return_value = '{"cleaned_text": "", "corrections": [], "notes": ""}'
        r = SubtitleRefiner(cfg, llm=mock_llm)
        # 20 行 × 40 字 → 每块 2 行 → 10 块,超过上限 6
        lines = [f"{i:02d}" + "一" * 38 for i in range(20)]
        text = "\n".join(lines)

        result = r.refine(text, title="t")

        assert mock_llm.complete.call_count == _MAX_CHUNKS
        assert "保留原文" in result.notes
        assert result.cleaned_text.endswith(lines[-1])

    def test_within_limit_stays_single_call(self, cfg, mock_llm):
        """未超限 → 路径与旧实现完全一致(一次调用,notes 不被污染)。"""
        mock_llm.complete.return_value = '{"cleaned_text": "OK", "corrections": [], "notes": "正常"}'
        r = SubtitleRefiner(cfg, llm=mock_llm)

        result = r.refine("短文本", title="t")

        assert result.cleaned_text == "OK"
        assert result.notes == "正常"
        mock_llm.complete.assert_called_once()

    def test_default_max_chars_6000(self, cfg, mock_llm):
        """默认 refine_max_chars=6000(看 config.py 默认值)。"""
        mock_llm.complete.return_value = '{"cleaned_text": "ok", "corrections": [], "notes": ""}'
        r = SubtitleRefiner(cfg, llm=mock_llm)

        # 5000 字符 < 默认 6000,正常调 LLM
        result = r.refine("中" * 5000)

        mock_llm.complete.assert_called_once()
        assert result.cleaned_text == "ok"


# ---------------- 真实场景:spike transcript 风格 ----------------


class TestRealStyle:
    """用之前 spike 输出的"繁体+碎片"样本模拟真实调用。"""

    def test_traditional_to_simplified_conversion(self, refiner, mock_llm):
        """模拟 LLM 把繁体转简体。"""
        traditional = "這是一段繁體字幕,講解深度學習的基礎知識,包括神經網路的工作原理"
        mock_llm.complete.return_value = json_response(
            cleaned_text="这是一段繁体字幕,讲解深度学习的基础知识,包括神经网络的工作原理",
            corrections=[
                {"original": "繁體", "fixed": "繁体", "reason": "繁简统一"},
                {"original": "講解", "fixed": "讲解", "reason": "繁简统一"},
                {"original": "神經網路", "fixed": "神经网络", "reason": "繁简统一"},
            ],
            notes="全文繁体转简体",
        )

        result = refiner.refine(traditional, title="深度学习入门")

        assert "简体" not in result.cleaned_text or "繁" not in result.cleaned_text.split("，")[0] if "，" in result.cleaned_text else True
        assert len(result.corrections) == 3

    def test_homophone_correction(self, refiner, mock_llm):
        """模拟 LLM 修正同音字(Deep Sake → Deep Seek)。"""
        text = "今天我们来讲 Deep Sake 这个模型的基本架构"
        mock_llm.complete.return_value = json_response(
            cleaned_text="今天我们来聊 Deep Seek 这个模型的基本架构。",
            corrections=[
                {"original": "Deep Sake", "fixed": "Deep Seek", "reason": "根据视频标题,同音字修正"},
                {"original": "讲", "fixed": "聊", "reason": "口语化更自然"},
            ],
            notes="修正 1 个技术术语 + 1 处口语化",
        )

        result = refiner.refine(text, title="DeepSeek 架构解析")

        assert "Deep Seek" in result.cleaned_text
        assert any(c.fixed == "Deep Seek" for c in result.corrections)


# ---------------- write_cleaned_transcript 落盘 ----------------


class TestWriteCleanedTranscript:
    def test_writes_cleaned_text_as_body(self, tmp_path):
        result = RefinementResult(
            original_text="原文",
            cleaned_text="整理后的文本",
            corrections=[],
            notes="测试",
            model="gpt-4o-mini",
        )
        path = tmp_path / "test.cleaned.txt"

        write_cleaned_transcript(path, result)

        content = path.read_text(encoding="utf-8")
        assert content.startswith("整理后的文本")
        assert "---" in content
        assert "model: gpt-4o-mini" in content
        assert "notes: 测试" in content

    def test_includes_corrections_section(self, tmp_path):
        result = RefinementResult(
            original_text="a",
            cleaned_text="b",
            corrections=[
                Correction(original="x", fixed="y", reason="z"),
                Correction(original="p", fixed="q", reason="r"),
            ],
            notes="",
            model="m",
        )
        path = tmp_path / "test.cleaned.txt"

        write_cleaned_transcript(path, result)

        content = path.read_text(encoding="utf-8")
        assert "corrections (2):" in content
        assert "x → y (z)" in content
        assert "p → q (r)" in content

    def test_skips_empty_notes(self, tmp_path):
        result = RefinementResult(
            original_text="a", cleaned_text="b", corrections=[], notes="", model="m",
        )
        path = tmp_path / "test.cleaned.txt"

        write_cleaned_transcript(path, result)

        content = path.read_text(encoding="utf-8")
        assert "notes:" not in content  # 空 notes 不写入

    def test_returns_path(self, tmp_path):
        result = RefinementResult(
            original_text="a", cleaned_text="b", corrections=[], notes="", model="m",
        )
        path = tmp_path / "test.cleaned.txt"

        returned = write_cleaned_transcript(path, result)

        assert returned == path


# ---------------- 模块导出 ----------------


class TestModuleExports:
    def test_subtitle_refiner_exported(self):
        from vla.quality import refiner
        assert hasattr(refiner, "SubtitleRefiner")
        assert hasattr(refiner, "write_cleaned_transcript")

    def test_protocol_satisfied_by_mock(self):
        """LLMClientLike Protocol 用 runtime_checkable,可 isinstance 检查。"""
        from vla.quality.refiner import LLMClientLike

        mock = MagicMock()
        mock.complete = MagicMock(return_value="x")
        assert isinstance(mock, LLMClientLike)


# ---------------- Helpers ----------------


def json_response(
    cleaned_text: str,
    corrections: list[dict] | None = None,
    notes: str = "",
) -> str:
    """构造 LLM JSON 响应的快捷 helper。"""
    import json
    return json.dumps({
        "cleaned_text": cleaned_text,
        "corrections": corrections or [],
        "notes": notes,
    }, ensure_ascii=False)


def _unused_helper_check_prompts():
    """smoke test:system + user prompt 模板能 format。"""
    sys_part = _SYSTEM_PROMPT[:50]
    user_part = _USER_PROMPT_TEMPLATE.format(
        title="t", char_count=10, text="x"
    )
    assert "字幕清理助手" in sys_part
    assert "t" in user_part
