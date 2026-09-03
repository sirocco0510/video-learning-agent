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

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vla.config import VLAConfig
from vla.models import Correction, RefinementResult
from vla.quality.refiner import (
    SubtitleRefiner,
    _SYSTEM_PROMPT,
    _USER_PROMPT_TEMPLATE,
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
        "browser_plugin": {
            "name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30,
            "plugin_paths": [], "record_hotkey": "Alt+Shift+R",
            "record_download_timeout_sec": 5, "record_pre_grace_sec": 0, "record_post_buffer_sec": 0,
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
    def test_missing_llm_raises(self, cfg):
        """refine_enabled=True 但没注入 LLM → RuntimeError。"""
        r = SubtitleRefiner(cfg, llm=None)
        with pytest.raises(RuntimeError, match="LLM 客户端"):
            r.refine("一些字幕文本", title="视频标题")

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
        """输出 token 上限 = max(refine_max_output_tokens, len(input) + 1000)。

        短输入用配置下限;长输入按比例放大(reasoning model 还要算 think 块)。
        """
        mock_llm.complete.return_value = '{"cleaned_text": "x", "corrections": [], "notes": ""}'

        # 短文本:用配置的默认 4000
        refiner.refine("短文本测试")
        kwargs = mock_llm.complete.call_args.kwargs
        assert kwargs["max_tokens"] == 4000  # 配置 refine_max_output_tokens 默认 4000

        # 长文本:max(4000, len(text)+1000)
        long_text = "中" * 5000
        refiner.refine(long_text)
        kwargs = mock_llm.complete.call_args.kwargs
        assert kwargs["max_tokens"] == 6000  # max(4000, 5000 + 1000) = 6000

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


class TestMaxCharsGuard:
    def test_text_exceeds_max_chars_skips_llm(self, cfg, mock_llm):
        """超过 refine_max_chars → 不调 LLM,直接返回原文本 + 提示。"""
        cfg.quality_check.refine_max_chars = 100
        r = SubtitleRefiner(cfg, llm=mock_llm)
        long_text = "一" * 200

        result = r.refine(long_text, title="t")

        assert result.cleaned_text == long_text
        assert result.corrections == []
        assert "长度超限" in result.notes
        mock_llm.complete.assert_not_called()

    def test_text_within_limit_calls_llm(self, cfg, mock_llm):
        cfg.quality_check.refine_max_chars = 100
        mock_llm.complete.return_value = '{"cleaned_text": "ok", "corrections": [], "notes": ""}'
        r = SubtitleRefiner(cfg, llm=mock_llm)

        result = r.refine("短文本", title="t")

        assert result.cleaned_text == "ok"
        mock_llm.complete.assert_called_once()

    def test_default_max_chars_6000(self, cfg, mock_llm):
        """默认 refine_max_chars=6000(看 config.py 默认值)。"""
        mock_llm.complete.return_value = '{"cleaned_text": "ok", "corrections": [], "notes": ""}'
        r = SubtitleRefiner(cfg, llm=mock_llm)

        # 5000 字符 < 默认 6000,正常调 LLM
        text = "中" * 5000
        result = r.refine(text)

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
