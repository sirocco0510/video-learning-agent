"""QualityChecker(SSOT: requirements.md FR-4 + implementation-plan.md Phase 5)。

职责:
- 字幕质量门控:启发式预筛 + 云端 LLM 评估
- 启发式快速失败:语速异常(<min 或 >max)/ 重复异常(≥3 重复 ≥5 字)
- 启发式通过才调 LLM(省钱 + 省时)
- LLM 返回 pass / score / issues / suggestion,组装成 QualityResult

设计:
- LLM 通过构造 / set_llm 注入;**没注入则首次 check 时按 cfg 惰性构造**
  (2026-09-10,见 `_resolve_llm`)
- 异常向上传播(FR-3.5 风格:由 Phase 6 log 模块负责记录)
- JSON 解析鲁棒:处理 ```json``` 代码块 / 前缀文字
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from vla.config import VLAConfig
from vla.llm.client import LLMClient, LLMClientLike
from vla.models import QualityResult


logger = logging.getLogger(__name__)


# LLM Prompt(FR-4.2 / FR-4.4,2026-09-10 重写为两类判定 + pass/score 锚定)
PROMPT = """你是字幕质量审核员。判断这份 Whisper 转写的字幕**能不能用**。

【视频标题】:{title}
【视频时长】:{duration_sec} 秒
【转写引擎】faster-whisper-{model_size}
【文本长度】{char_count} 字
【估算语速】{char_per_second:.1f} 字/秒(中文正常 4-7;公司内部培训课程通常偏慢,
3-5 也算正常,关注文本是否完整可读而非绝对语速)

【转写文本】
{text}

【判定类别 —— 只有第一类才 fail】

■ 第一类:**转写失败** → pass=false
  出现任一即属此类:
  - 大面积乱码 / 无意义字符
  - 同一句话重复 ≥3 次的死循环
  - 覆盖面严重不足:只转出视频开头一小段(字幕远少于视频实际内容)
  - 语速远超正常范围(疑似幻觉)

■ 第二类:**可读但需校对** → pass=true
  这些是**可修复的瑕疵**,不是转写失败 —— 记进 issues 并在 score 上扣分即可:
  - 错别字 / 同音字(如 "PASS" 应为 "Python"、"加碼" 应为 "Java")
  - 繁简混杂(部分片段输出繁体)
  - 口语化、语气词冗余
  - 个别语句语序混乱,但结合上下文能读懂
  - 专有名词拼写不一致
  **注意**:内容读得懂就不算失败。不要因为"有错别字"或"繁简混杂"判 pass=false。

【评分与 pass 的关系】
score 是 0-100 的可读性评分。pass 必须与 score 一致,规则:
    pass = (score >= {min_score})
即 score >= {min_score} 时 pass 必须为 true;只有低于 {min_score} 时才为 false。
不要给出 pass 与 score 互相矛盾的结论。

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

    @property
    def model(self) -> str:
        """质量检查用的模型名(与 spike / config 的 quality_check.model 一致)。"""
        return self.config.quality_check.model

    def _resolve_llm(self) -> LLMClientLike:
        """取 LLM 客户端;没注入就按 config **惰性构造**一个。

        为什么不在 __init__ 里构造(2026-09-10):
        构造期就要凭据 → `build_text_provider(cfg)` 在没加载 .env 的环境(CI /
        单测)直接炸 `OpenAIError: Missing credentials`,而装配路径本身与
        凭据无关(同 SubtitleRefiner:`Xxx(cfg)` 构造期零依赖)。

        为什么要兜底而不是"没注入就报错"(2026-09-10 真机批量修复):
        调用方 `build_text_provider` 的 T13 兜底只建 `QualityChecker(cfg)`、
        没注入 LLM → 旧实现抛 RuntimeError → 该异常**穿过 process_asset /
        _process_one 一路上抛**,把整个 `vla learn` 批量带走(不是 fail 单条,
        是崩全批 —— 比 VideoSummarizer 那次的"静默不产出"更狠)。

        与 `VideoSummarizer._resolve_llm` 同一契约:构造期零依赖 + 类内惰性
        构造,装配路径不再决定功能是否可用。
        """
        if self._llm is None:
            self._llm = LLMClient(self.config.llm_client, model=self.model)
        return self._llm

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
        min_score = self.config.quality_check.min_score_to_pass

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

        # LLM 检查(没注入则惰性构造 —— 见 _resolve_llm)
        llm = self._resolve_llm()

        prompt = PROMPT.format(
            title=title,
            duration_sec=duration_sec,
            model_size=model_size,
            char_count=char_count,
            char_per_second=cps,
            text=text,
            # 2026-09-10:把阈值**注入 prompt**,让 LLM 的 `pass` 锚定到与代码
            # 同一个数 —— 否则 LLM 可以给出 `score=55 / pass=false` 这种
            # 自相矛盾的结果,而下面那记 AND 会让 `pass=false` 一票否决。
            min_score=min_score,
        )
        response = llm.complete(prompt, max_tokens=2000)
        from vla.llm.response import parse_json_response
        data = parse_json_response(response)

        llm_pass = bool(data.get("pass", False))
        score = int(data.get("score", 0))
        issues = list(data.get("issues", []))
        suggestion = str(data.get("suggestion", ""))

        # passed 综合判定:LLM 通过 AND 分数 ≥ 阈值。
        #
        # 2026-09-10:prompt 已注入同一个 min_score、并要求 `pass = score >= 阈值`,
        # 所以两个判据**按构造一致** —— 这里的 AND 退化为安全网(只在 LLM 仍
        # 返回自相矛盾结果时才咬),不再是"双重否决"。
        passed = llm_pass and score >= min_score

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