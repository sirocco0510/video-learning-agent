"""SubtitleRefiner(SSOT: requirements.md FR-2.15c / Level 4,2026-09-02)。

职责:
- 接 faster-whisper + 本地 postprocess 之后的 transcript
- 调云端 LLM 做语义整理:繁简统一 + 同音字错字修正 + 碎片重组
- 输出 RefinementResult:cleaned_text + corrections + notes

为什么不直接覆盖 .transcript.txt?
- 用户/审计需要保留原 Whisper 输出(可能是模型对比、回放)
- cleaned.txt 是"二次加工",可追溯
- 与 FR-2.15b (transcript 落盘) 模式一致:同目录 `<stem>.cleaned.txt`

配额归类:
- 项目 SSOT:"云端 LLM 限定两件事: ① 字幕质量检查 ② 6h 批量总结"
- 本类归入 ①(字幕质量相关,清理后还会被 QualityChecker 评分)
- 调用方负责 QuotaManager / API budget 控制;本类**不**自己加限流

失败 fallback:
- LLM 调用失败 / 解析失败 / quota 用完 → 返回原始 text + notes="(LLM 清理失败,使用原始文本)"
- 不抛错(主流程不因 cleanup 失败中断)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from vla.config import VLAConfig
from vla.llm.client import LLMClientLike
from vla.models import Correction, RefinementResult


logger = logging.getLogger(__name__)


# ---------------- Prompt 设计 ----------------

_SYSTEM_PROMPT = """你是专业的中文(简体/繁体)字幕清理助手。你的工作是把 faster-whisper 转写出的"半成品"字幕整理成可读性接近人工字幕的版本。

【输入】一段经过本地碎片合并 + 重复段去重的 transcript,可能含:
- 繁简混排(如"視頻"和"视频"在同一段)
- 同音字错字(如"Deep Sake"应为"Deep Seek",前提是上下文能推断)
- 残留碎片(如单独一个"了"、"呢")
- 句意不通 / 语序混乱(Whisper 时序错位)
- B站自动 CC 叠音导致的重复片段

【任务】
1. **繁简统一**:根据【视频标题】和整体语料判断用 简体 还是 繁体,全文统一。
   标题含台湾用语 / 繁体 → 繁体
   标题含大陆用语 / 简体 → 简体
   无法判断 → 默认简体
2. **同音字修正**:根据视频主题修正明显的同音字错字(AI / 技术术语 / 人名 / 地名)。
3. **碎片合并**:把上下文明显属同一句的碎片自然连接(用合适的标点)。
4. **保留原意**:不删内容、不总结、不翻译、不加注释;只整理不创作。
5. **段落切分**:按语义自然段落用 \\n\\n 分隔,避免一大坨。

【输出格式 — 严格 JSON,只输出 JSON,不要其他文字】
{
  "cleaned_text": "整理后的完整文本(段落用 \\n\\n 分隔)",
  "corrections": [
    {"original": "原文片段", "fixed": "修正后", "reason": "为什么这么修"}
  ],
  "notes": "一句话说明本次清理做了什么(繁简统一 / 修正了几个术语 / 合并了多少碎片)"
}
"""


_USER_PROMPT_TEMPLATE = """【视频标题】
{title}

【原始 transcript(共 {char_count} 字符,已做本地碎片合并 + 重复段去重)】
{text}

请按系统指令输出 JSON。"""


# ---------------- 主类 ----------------


class SubtitleRefiner:
    """字幕语义清理(2026-09-02 Level 4)。

    用法:
        refiner = SubtitleRefiner(config, llm_client)
        result = refiner.refine(text, title="xxx")
        # result.cleaned_text → 写到 <stem>.cleaned.txt
        # result.corrections → 审计 / 词典生成
    """

    def __init__(
        self,
        config: VLAConfig,
        llm: LLMClientLike | None = None,
    ) -> None:
        self.config = config
        self._llm = llm

    def set_llm(self, llm: LLMClientLike) -> None:
        """延迟注入 LLM 客户端(同 QualityChecker 模式)。"""
        self._llm = llm

    @property
    def enabled(self) -> bool:
        """config.quality_check.refine_enabled — 调用方决定是否调用 refine()。"""
        return self.config.quality_check.refine_enabled

    @property
    def model(self) -> str:
        """R-10:统一从 cfg.llm.refine_model 取值。

        历史语义(refine_model 为空时 fallback 到 quality_check.model)在
        VLAConfig._migrate_legacy_llm_keys 的 pre-validator 里实现:
        旧 YAML 缺 refine_model 时,llm.refine_model 默认 = llm.quality_model。
        """
        return self.config.llm.refine_model

    # ---------------- 主流程 ----------------

    def refine(
        self,
        text: str,
        title: str = "",
    ) -> RefinementResult:
        """清理一段 transcript,返回 RefinementResult。

        流程:
        1. 长度检查:超过 refine_max_chars 直接跳过(避免爆 token)
        2. 调 LLM(system + user prompt)
        3. 解析 JSON → RefinementResult
        4. 任何环节失败 → 返回原 text + notes="失败回退"

        Raises:
            RuntimeError: 未注入 LLM 客户端(refine_enabled=True 时必须)
        """
        if self._llm is None:
            raise RuntimeError(
                "SubtitleRefiner 没有 LLM 客户端,请先 set_llm() 或构造时注入"
            )

        original_text = text
        max_chars = self.config.quality_check.refine_max_chars

        # 长度超限:跳过 LLM,直接返回
        if len(text) > max_chars:
            logger.warning(
                "📏 transcript 字符数 %d > refine_max_chars %d,跳过 LLM 清理",
                len(text), max_chars,
            )
            return RefinementResult(
                original_text=original_text,
                cleaned_text=original_text,
                corrections=[],
                notes=f"长度超限({len(text)} > {max_chars}),跳过 LLM 清理",
                model=self.model,
            )

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            title=title or "(无标题)",
            char_count=len(text),
            text=text,
        )
        # 把 system + user 拼成一段(LLMClientLike.complete 是单 prompt 接口)
        full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_prompt}"

        try:
            # 输出 token 上限:用户配置的 refine_max_output_tokens,
            # 但要保证 ≥ 输入 chars(LLM 至少能"还原"输入长度)。
            # reasoning model(M2 / R1)还会输出 <think> 块,实际更费。
            cfg_max = self.config.quality_check.refine_max_output_tokens
            output_max_tokens = max(cfg_max, len(text) + 1000)
            response = self._llm.complete(
                full_prompt,
                max_tokens=output_max_tokens,
                temperature=0.2,
            )
        except Exception as e:
            logger.warning("⚠️ LLM 清理调用失败,使用原始文本:%s", e)
            return RefinementResult(
                original_text=original_text,
                cleaned_text=original_text,
                corrections=[],
                notes=f"LLM 调用失败:{type(e).__name__}:{str(e)[:100]}",
                model=self.model,
            )

        try:
            from vla.llm.response import parse_json_response
            data = parse_json_response(response)
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("⚠️ LLM 清理响应解析失败:%s\n原始响应:%s", e, response[:200])
            return RefinementResult(
                original_text=original_text,
                cleaned_text=original_text,
                corrections=[],
                notes=f"LLM 响应解析失败:{e}",
                model=self.model,
            )

        cleaned_text = str(data.get("cleaned_text", "")).strip()
        if not cleaned_text:
            logger.warning("⚠️ LLM 清理返回空 cleaned_text,使用原始文本")
            return RefinementResult(
                original_text=original_text,
                cleaned_text=original_text,
                corrections=[],
                notes="LLM 返回空 cleaned_text,fallback 原始文本",
                model=self.model,
            )

        corrections: list[Correction] = []
        for c in data.get("corrections", []):
            if not isinstance(c, dict):
                continue
            try:
                corrections.append(Correction(
                    original=str(c.get("original", "")),
                    fixed=str(c.get("fixed", "")),
                    reason=str(c.get("reason", "")),
                ))
            except Exception:
                continue

        notes = str(data.get("notes", ""))

        logger.info(
            "✨ LLM 清理完成: %d → %d 字符, %d 条修正",
            len(original_text), len(cleaned_text), len(corrections),
        )
        return RefinementResult(
            original_text=original_text,
            cleaned_text=cleaned_text,
            corrections=corrections,
            notes=notes,
            model=self.model,
            # prompt_tokens / completion_tokens 留待后续接 openai SDK usage 时填
        )

# ---------------- 文件落盘 helper ----------------


def write_cleaned_transcript(
    cleaned_path: Path,
    result: RefinementResult,
) -> Path:
    """把 RefinementResult.cleaned_text 写到磁盘(FR-2.15c 同款落盘规则)。

    文件格式:
        {cleaned_text}

        ---
        cleaned_at: 2026-09-02T15:30:00
        model: gpt-4o-mini
        notes: 繁简统一 + 修正 5 个术语
        corrections (3):
          - Deep Sake → Deep Seek (同音字,根据视频标题推断)
          ...

    写入失败抛 OSError(调用方决定是否容错 — 与 FR-2.15b 同策略)。
    """
    from datetime import datetime

    lines: list[str] = [result.cleaned_text, "", "---", ""]
    lines.append(f"cleaned_at: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"model: {result.model}")
    if result.notes:
        lines.append(f"notes: {result.notes}")
    if result.corrections:
        lines.append(f"corrections ({len(result.corrections)}):")
        for c in result.corrections:
            lines.append(f"  - {c.original} → {c.fixed} ({c.reason})")
    cleaned_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("💾 cleaned transcript 已落盘:%s (%d 字符)", cleaned_path, len(result.cleaned_text))
    return cleaned_path
