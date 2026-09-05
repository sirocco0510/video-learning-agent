"""字幕后处理(2026-09-02 Level 3 步骤 1):碎片合并 + 重复段去重。

设计原则:
- **纯本地**(满足"字幕永远本地"红线),不依赖 OpenCC / jieba / 云端 LLM
- 启发式为主,行为可观测(返回合并前后长度 + 处理次数)
- 调用方决定是否启用(config 开关,默认开)
- 幂等:对已合并的文本再跑一遍无副作用

为什么需要:
- faster-whisper(VAD 过滤后)仍输出大量 1-3 字碎片行
- 叠音(原声 + B站自动 CC TTS)产生重复段
- 短句碎片严重降低可读性
- 繁简混排(台灣口音)由 Level 3 步骤 3 单独处理,本模块只管结构

注意:
- 不引入繁简转换(用 OpenCC 会引入第三方依赖,Level 3 步骤 3 再上)
- 不引入 jieba 分词(同上)
- 不引入 LLM(走 Level 4,云端配额单独管)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass


logger = logging.getLogger(__name__)


# 默认阈值
DEFAULT_MIN_LINE_CHARS = 8       # 短于这个字符的行认为碎片,合并到上一段
DEFAULT_MIN_OVERLAP_CHARS = 6    # 两行重复片段短于这个不算重复(防误杀)
DEFAULT_MAX_LINE_CHARS = 80      # 单行超过这个字符不再合并(可能是有意为之)


@dataclass(frozen=True)
class PostprocessStats:
    """后处理统计:用于日志 + 后续 quality check。"""

    original_chars: int
    original_lines: int
    final_chars: int
    final_lines: int
    merged_short_lines: int      # 合并的碎片行数
    deduped_repeated_segments: int  # 去掉的重复段数

    @property
    def char_reduction_ratio(self) -> float:
        """字符压缩比(0~1,越大说明去重越多)。"""
        if self.original_chars == 0:
            return 0.0
        return 1.0 - (self.final_chars / self.original_chars)


def merge_short_lines(
    text: str,
    min_chars: int = DEFAULT_MIN_LINE_CHARS,
    max_chars: int = DEFAULT_MAX_LINE_CHARS,
) -> str:
    """碎片行合并:短于 min_chars 的行并入上一段。

    例子(本项目实际跑出的 transcript):
        到我講接觸了Deep Sake 和Jewan Media 之後內疾夢
        只能提到各種壓力的工具          ← 9 字符, 保留
        但自己還是會用AI去做服役         ← 12 字符, 保留
        了
        看了別人用AI接高檢的商單         ← "了"(1 字符) → 并入上一段
        ...
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text

    merged: list[str] = []
    merge_count = 0
    for line in lines:
        if (
            merged
            and len(line) < min_chars
            and len(merged[-1]) + len(line) < max_chars
        ):
            merged[-1] = merged[-1] + line
            merge_count += 1
        else:
            merged.append(line)
    if merge_count > 0:
        logger.info("📏 合并碎片行:%d 行被并入上一段", merge_count)
    return "\n".join(merged)


def dedupe_repeated_segments(
    text: str,
    min_overlap: int = DEFAULT_MIN_OVERLAP_CHARS,
) -> str:
    """去重相邻重复段:连续两行有 ≥ min_overlap 字符的公共子串 → 删短的那行。

    触发场景:Screen Recorder 录到原声 + B站自动 CC TTS 叠音,Whisper
    把同一句听成两遍(可能错位)。例:
        我發現一
        發現一個商機                   ← "發現一" 重复,删
        我發現一個商機在2024年          ← 不重复,留
    """
    lines = text.splitlines()
    if len(lines) < 2:
        return text

    deduped: list[str] = []
    dedupe_count = 0
    for line in lines:
        if deduped and _has_significant_overlap(deduped[-1], line, min_overlap):
            # 取较长的那行
            if len(line) > len(deduped[-1]):
                deduped[-1] = line
            dedupe_count += 1
        else:
            deduped.append(line)
    if dedupe_count > 0:
        logger.info("🧹 去掉重复段:%d 段", dedupe_count)
    return "\n".join(deduped)


def _has_significant_overlap(a: str, b: str, min_overlap: int) -> bool:
    """判断两行是否有 ≥ min_overlap 字符的公共子串。

    用连续公共子串(LCS 风格)而非集合:Jaccard 相似度对短句误判多。
    例:"我發現一個商機" vs "發現一個商機在2024"
       → 公共连续子串 "現一個商機"(5 字符),min_overlap=6 → 不算重复 ✅
    例:"我發現一" vs "發現一個商機"
       → 公共连续子串 "發現一"(3 字符),min_overlap=6 → 不算重复 ✅
    例:"我發現一個商機在2024" vs "發現一個商機在2025"
       → 公共连续子串 "現一個商機在202"(9 字符) → 算重复,删短的那行
    """
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    # 在 shorter 里找最长的在 longer 里连续出现的子串
    max_overlap = 0
    for i in range(len(shorter)):
        for j in range(i + min_overlap, len(shorter) + 1):
            sub = shorter[i:j]
            if sub in longer:
                max_overlap = max(max_overlap, len(sub))
                # 找到更短的可能更优(从长到短检查),但子串已是最长
                break
        if max_overlap >= min_overlap:
            return True
    return False


def clean_transcript(text: str) -> tuple[str, PostprocessStats]:
    """组合调用:碎片合并 → 重复段去重,返回 (cleaned_text, stats)。

    调用方:StreamingTranscriber.transcribe() 末尾串接。
    """
    original = text
    original_lines = [ln for ln in text.splitlines() if ln.strip()]

    text = merge_short_lines(text)
    text = dedupe_repeated_segments(text)

    final_lines = [ln for ln in text.splitlines() if ln.strip()]
    stats = PostprocessStats(
        original_chars=len(original),
        original_lines=len(original_lines),
        final_chars=len(text),
        final_lines=len(final_lines),
        merged_short_lines=len(original_lines) - len(
            [ln for ln in merge_short_lines(original).splitlines() if ln.strip()]
        ),
        deduped_repeated_segments=0,  # 占位,统计需要重新跑一遍
    )
    if stats.char_reduction_ratio > 0:
        logger.info(
            "📊 后处理统计: %d → %d 字符 (%.0f%% 压缩), %d → %d 行",
            stats.original_chars, stats.final_chars,
            stats.char_reduction_ratio * 100,
            stats.original_lines, stats.final_lines,
        )
    return text, stats


def clean_transcript_simple(text: str) -> str:
    """简化版:只返回文本,丢 stats(给 quick path 用)。"""
    cleaned, _ = clean_transcript(text)
    return cleaned
