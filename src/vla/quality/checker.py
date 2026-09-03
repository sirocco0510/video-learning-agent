"""QualityChecker(SSOT: requirements.md FR-4 + implementation-plan.md Phase 5)。

职责:
- 字幕质量门控:启发式预筛 + 云端 LLM 评估
- 启发式快速失败:语速异常(<min 或 >max)/ 重复异常(≥3 重复 ≥5 字)
- 启发式通过才调 LLM(省钱 + 省时)
- LLM 返回 pass / score / issues / suggestion,组装成 QualityResult

设计:
- LLM 通过构造 / set_llm 注入(默认 None,首次 check 时报错)
- 异常向上传播(FR-3.5 风格:由 Phase 6 log 模块负责记录)
- JSON 解析鲁棒:处理 ```json``` 代码块 / 前缀文字
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from vla.config import VLAConfig
from vla.llm.client import LLMClientLike
from vla.models import QualityResult


logger = logging.getLogger(__name__)


# LLM Prompt(从 implementation-plan.md Phase 5 复制,保持一致)
PROMPT = """你是字幕质量审核员。请评估以下 Whisper 转写的字幕质量。

【视频标题】:{title}
【视频时长】:{duration_sec} 秒
【转写引擎】faster-whisper-{model_size}
【文本长度】{char_count} 字
【估算语速】{char_per_second:.1f} 字/秒(中文正常 4-7)

【转写文本】
{text}

【检查维度】
1. **通顺度**:有无明显乱码、无意义重复、语序混乱?
2. **完整性**:是否覆盖视频大部分内容?语速是否在正常范围?
3. **准确性**:专业术语是否正确(可基于标题推断)?
4. **重复异常**:是否出现 ≥3 次重复的同一句话(Whisper 失败的典型表现)?

【输出 JSON】
{{
  "pass": true/false,
  "score": 0-100,
  "issues": ["问题1", "问题2"],
  "suggestion": "如果 fail,具体修复建议(如重新转写/人工修正)"
}}

只输出 JSON,不要其他文字。"""


class QualityChecker:
    """字幕质量门控(FR-4.1/4.2/4.3/4.4/4.5/4.6)。"""

    def __init__(
        self,
        config: VLAConfig,
        llm: LLMClientLike | None = None,
    ) -> None:
        self.config = config
        self._llm = llm

    def set_llm(self, llm: LLMClientLike) -> None:
        """注入 LLM 客户端(测试用 + 延迟初始化)。"""
        self._llm = llm

    # ---------------- 主流程 ----------------

    def check(
        self,
        text: str,
        title: str,
        duration_sec: int,
        model_size: str,
    ) -> QualityResult:
        """检查转写文本质量,返回 QualityResult。

        流程:
        1. 计算 char_count + char_per_second(duration 用 max(duration, 1) 防除 0)
        2. 启发式 1:语速异常 → 直接 fail score=20 / 30
        3. 启发式 2:重复异常(≥3 重复 ≥5 字)→ 直接 fail score=10
        4. 调 LLM,解析 JSON → 组装 QualityResult
        5. passed = llm_pass AND score >= min_score_to_pass
        """
        char_count = len(text)
        safe_duration = max(duration_sec, 1)
        cps = char_count / safe_duration

        # 启发式 1a:语速过低(< min)
        min_cps = self.config.quality_check.min_char_per_second
        if cps < min_cps:
            return QualityResult(
                passed=False,
                score=20,
                issues=[f"语速过低:{cps:.2f} 字/秒(阈值 {min_cps})"],
                suggestion="视频可能大量静音 / 转写失败,建议人工核查或重转",
                char_count=char_count,
            )

        # 启发式 1b:语速过高(> max)— Whisper 幻觉典型表现
        max_cps = self.config.quality_check.max_char_per_second
        if cps > max_cps:
            return QualityResult(
                passed=False,
                score=30,
                issues=[f"语速过高:{cps:.2f} 字/秒(阈值 {max_cps})"],
                suggestion="疑似 Whisper 幻觉,建议重新转写或换更大的模型",
                char_count=char_count,
            )

        # 启发式 2:重复异常
        if self._is_repetitive(text):
            return QualityResult(
                passed=False,
                score=10,
                issues=["重复异常:同一句子 ≥3 次重复"],
                suggestion="Whisper 进入死循环 / 重复状态,建议重新转写",
                char_count=char_count,
            )

        # LLM 检查
        if self._llm is None:
            raise RuntimeError("QualityChecker 没有 LLM 客户端,请先 set_llm() 或构造时注入")

        prompt = PROMPT.format(
            title=title,
            duration_sec=duration_sec,
            model_size=model_size,
            char_count=char_count,
            char_per_second=cps,
            text=text,
        )
        response = self._llm.complete(prompt, max_tokens=500)
        from vla.llm.response import parse_json_response
        data = parse_json_response(response)

        llm_pass = bool(data.get("pass", False))
        score = int(data.get("score", 0))
        issues = list(data.get("issues", []))
        suggestion = str(data.get("suggestion", ""))

        # passed 综合判定:LLM 通过 AND 分数 ≥ 阈值
        passed = llm_pass and score >= self.config.quality_check.min_score_to_pass

        return QualityResult(
            passed=passed,
            score=score,
            issues=issues,
            suggestion=suggestion,
            char_count=char_count,
        )

    # ---------------- 启发式 helper ----------------

    @staticmethod
    def _is_repetitive(text: str, min_sentence_len: int = 5, repeat_threshold: int = 3) -> bool:
        """检测 ≥3 重复的 ≥5 字句子。

        按中英文句末标点切(. ! ? 。 ! ?),统计每个句子的出现次数。
        - 句子长度 < min_sentence_len(默认 5)忽略(避免对"是的"等常见词误判)
        - 出现次数 ≥ repeat_threshold(默认 3)即视为重复异常
        """
        sentences = re.split(r"[。!?\.!\?]", text)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            return False
        most_common = Counter(sentences).most_common(1)[0]
        sent, count = most_common
        return len(sent) >= min_sentence_len and count >= repeat_threshold