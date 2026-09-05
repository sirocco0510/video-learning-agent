"""字幕后处理(postprocess)测试,SSOT:requirements.md Level 3 步骤 1。

测试覆盖:
- merge_short_lines:碎片合并的 5 个边界
- dedupe_repeated_segments:相邻重复段去重
- clean_transcript:组合调用 + stats
- 幂等性
- 真实 transcript 风格样本(来自 spike 实测输出)
"""

from __future__ import annotations

import pytest

from vla.transcribe.postprocess import (
    DEFAULT_MAX_LINE_CHARS,
    DEFAULT_MIN_LINE_CHARS,
    DEFAULT_MIN_OVERLAP_CHARS,
    PostprocessStats,
    _has_significant_overlap,
    clean_transcript,
    clean_transcript_simple,
    dedupe_repeated_segments,
    merge_short_lines,
)


# ---------------- merge_short_lines ----------------


class TestMergeShortLines:
    def test_short_line_merged_into_previous(self):
        text = "這是一個比較長的句子\n了\n這是另一個獨立句子"  # 最后一行 ≥ min_chars
        result = merge_short_lines(text)
        # "了"(1 字符) → 并入上一段;"這是另一個獨立句子"(9 字符) ≥ min_chars → 独立成行
        assert result == "這是一個比較長的句子了\n這是另一個獨立句子"

    def test_short_line_at_start_kept(self):
        """文件首行是碎片 → 没上一段可并,保留为独立行(防御性)。"""
        text = "了\n這是一個比較長的句子"
        result = merge_short_lines(text)
        # 首行没法并,保留
        assert result == "了\n這是一個比較長的句子"

    def test_consecutive_short_lines_all_merged_into_previous_long_line(self):
        text = "這是一個比較長的句子\n了\n呢\n嗎"
        result = merge_short_lines(text)
        # 首行加入 merged,后 3 个碎片行(都是 1 字符,< min_chars=8)依次并入
        assert result == "這是一個比較長的句子了呢嗎"

    def test_respects_max_line_chars(self):
        """合并后单行不能超过 max_chars(可能是有意为之的段落)。"""
        text = "一二三四五六七八九十一二三四五六七八九十一二三四五\n了"
        # 首行 23 字符,加 "了" = 24,刚好 < max(默认 80)→ 仍合并
        result_default = merge_short_lines(text)
        assert result_default == "一二三四五六七八九十一二三四五六七八九十一二三四五了"
        # max=23:加 "了" 后 24 > 23 → 不合并
        result_tight = merge_short_lines(text, max_chars=23)
        assert result_tight == "一二三四五六七八九十一二三四五六七八九十一二三四五\n了"

    def test_empty_input_returns_empty(self):
        # 空输入直接返回原 text(走 if not lines 分支)
        assert merge_short_lines("") == ""

    def test_all_long_lines_unchanged(self):
        # 全部 ≥ min_chars=8,不应有任何合并
        text = "第一行長度已經超過八個字符\n第二行也是夠長的字串\n第三行也是一樣這麼長的句子"
        result = merge_short_lines(text)
        assert result == text

    def test_custom_min_chars_threshold(self):
        """min_chars 阈值可调:阈值越小,越多行被判定为"短",越倾向于合并。"""
        # 默认 min=8:第二行 7 字符 < 8 → 并入
        text = "這是測試文字\n這是另一行"  # "這是另一行" = 5 字符
        result = merge_short_lines(text, min_chars=8)
        assert result == "這是測試文字這是另一行"
        # min=3:两行都 ≥ 3 字符,都不合并
        no_merge = merge_short_lines(text, min_chars=3)
        assert no_merge == text


# ---------------- dedupe_repeated_segments ----------------


class TestDedupeRepeatedSegments:
    def test_consecutive_overlap_deduped(self):
        # 第二行明显更长 → 保留长的
        text = "我發現一個商機在2024\n發現一個商機在2024年了啊"
        result = dedupe_repeated_segments(text)
        # 公共连续子串 "現一個商機在2024" 10 字符 > 6 → 算重复
        # 短的 12 字符 vs 长的 14 字符 → 保留长的
        assert result == "發現一個商機在2024年了啊"

    def test_equal_length_keeps_first(self):
        """两行重复且等长 → 保留第一行(原序)。"""
        text = "我發現一個商機在2024\n發現一個商機在2024年"  # 都是 12 字符
        result = dedupe_repeated_segments(text)
        assert result == "我發現一個商機在2024"

    def test_no_overlap_kept(self):
        text = "第一句完全不同的內容\n第二句也是無關的句子"
        result = dedupe_repeated_segments(text)
        assert result == text

    def test_short_overlap_below_threshold_kept(self):
        """短于 min_overlap 的公共子串不算重复(防误杀)。"""
        text = "我發現一\n發現一個商機"  # 公共 "現一" 2 字符 < min_overlap=6
        result = dedupe_repeated_segments(text)
        assert result == text

    def test_three_lines_middle_is_dup(self):
        text = "我發現一個商機\n我發現一個商機在2024\n下一句完全不同"
        result = dedupe_repeated_segments(text)
        # 第 1 行和第 2 行重复 → 取长的;第 2 行(去重后)和第 3 行不重复
        # 但 _has_significant_overlap 是相邻比较,所以:
        # 1. line0 + line1 重复 → 保留 line1
        # 2. line1 + line2 不重复 → 保留 line2
        assert result == "我發現一個商機在2024\n下一句完全不同"

    def test_empty_input_returns_empty(self):
        assert dedupe_repeated_segments("") == ""
        assert dedupe_repeated_segments("單行") == "單行"

    def test_real_spike_style_overlap(self):
        """实际 spike 输出的典型叠音模式(原声 + B站 CC TTS 同句重复)。"""
        text = (
            "我會用AI去做一些事情\n我會用AI去做一些事情的話\n"
            "看了別人用AI接高檢的商單\n看了別人用AI接高檢的商單之後"
        )
        result = dedupe_repeated_segments(text)
        # 前两行重复("我用AI去做一些事")→ 保留第二行(更长)
        # 之后两行重复("看了別人用AI接高檢的商單")→ 保留第二行
        lines = result.splitlines()
        assert len(lines) == 2
        assert "話" in lines[0]  # 更长的那个
        assert "之後" in lines[1]


# ---------------- _has_significant_overlap ----------------


class TestHasSignificantOverlap:
    def test_long_substring_match(self):
        assert _has_significant_overlap("我發現一個商機在2024", "發現一個商機在2024年", 6) is True

    def test_short_substring_below_threshold(self):
        # "發現" 2 字符 < 6
        assert _has_significant_overlap("我發現了", "發現新的", 6) is False

    def test_no_common_substring(self):
        assert _has_significant_overlap("完全不同的內容", "另一個無關句子", 6) is False

    def test_empty_input(self):
        assert _has_significant_overlap("", "任何内容", 6) is False
        assert _has_significant_overlap("任何内容", "", 6) is False

    def test_threshold_boundary(self):
        # 公共子串恰好 = min_overlap → 算重复(>=)
        # "ABCDEF" 6 字符
        assert _has_significant_overlap("XABCDEFY", "ZABCDEF", 6) is True
        # "ABCDE" 5 字符 < 6
        assert _has_significant_overlap("XABCDEFY", "ZABCDE", 6) is False


# ---------------- clean_transcript ----------------


class TestCleanTranscript:
    def test_combined_short_line_and_dedup(self):
        text = "我發現一個商機在2024\n我發現一個商機在2024年了\n嗎"
        cleaned, stats = clean_transcript(text)
        # step 1 merge_short_lines:
        #   line0 "我發現一個商機在2024"(11 chars) → merged[0]
        #   line1 "我發現一個商機在2024年了"(12 chars) ≥ min_chars → merged[1]
        #   line2 "嗎"(1 char) < 8,merged[-1]=12+1=13<80 → 并入 → merged[1]="我發現一個商機在2024年了嗎"
        # step 2 dedupe:
        #   merged[0] = "我發現一個商機在2024" vs merged[1] = "我發現一個商機在2024年了嗎"
        #   公共连续子串 "現一個商機在2024"(10 chars) > 6 → 算重复
        #   保留长的 merged[1]
        lines = [ln for ln in cleaned.splitlines() if ln]
        assert lines == ["我發現一個商機在2024年了嗎"]

    def test_returns_stats(self):
        text = "短\n" * 20 + "這是一個長度足夠的句子\n短"
        cleaned, stats = clean_transcript(text)
        assert isinstance(stats, PostprocessStats)
        assert stats.original_lines > stats.final_lines

    def test_idempotent(self):
        """跑两遍结果一样。"""
        text = "我發現一個商機\n我發現一個商機在2024年了\n短"
        first, _ = clean_transcript(text)
        second, _ = clean_transcript(first)
        assert first == second

    def test_empty_input_returns_empty(self):
        cleaned, stats = clean_transcript("")
        assert cleaned == ""
        assert stats.original_chars == 0

    def test_clean_transcript_simple_returns_only_text(self):
        text = "a\nb"  # 极简 case
        result = clean_transcript_simple(text)
        assert isinstance(result, str)


# ---------------- 真实 transcript 风格集成测试 ----------------


class TestRealTranscriptStyle:
    """基于 spike 实测 transcript 的风格(简体 + 繁体 + 大量碎片)集成测试。"""

    SPIKE_FRAGMENT = """到我講接觸了Deep Sake 和Jewan Media 之後內疾夢
只能提到各種壓力的工具
但自己還是會用AI去做服役
了
看了別人用AI接高檢的商單
做報款自媒體自製圈的路都沒回答
因為你學的是找了用的
見它不是怎麼用AI去搶商業
我在過去幾年時間裡
其實做了幾百個合作的視頻
很自放的樣過癮"""

    def test_short_lines_merged(self):
        """'了'(1 字符) → 并入上一行。"""
        cleaned, stats = clean_transcript(self.SPIKE_FRAGMENT)
        # 原 10 行 → 9 行(少一个碎片行)
        assert stats.final_lines == 9
        # 单独一行的 '了' 消失了(并入上一行变成 '服役了')
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        assert "了" not in lines  # 没有独立成行的 '了'
        assert "服役了" in cleaned  # 合并后的内容

    def test_idempotent_on_spike_fragment(self):
        first, _ = clean_transcript(self.SPIKE_FRAGMENT)
        second, _ = clean_transcript(first)
        assert first == second


# ---------------- Module exports ----------------


def test_module_exports():
    """sanity check:主要公开 API 都能正常 import。"""
    from vla.transcribe import postprocess
    assert hasattr(postprocess, "clean_transcript")
    assert hasattr(postprocess, "merge_short_lines")
    assert hasattr(postprocess, "dedupe_repeated_segments")
    assert hasattr(postprocess, "PostprocessStats")
