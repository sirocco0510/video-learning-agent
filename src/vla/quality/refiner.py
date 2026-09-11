"""SubtitleRefiner(SSOT: requirements.md FR-2.15c / Level 4,2026-09-02)。

职责:
- 接 faster-whisper + 本地 postprocess 之后的 transcript
- 调云端 LLM 做语义整理:繁简统一 + 同音字错字修正 + 碎片重组
- 输出 RefinementResult:cleaned_text + corrections + notes

为什么不直接覆盖 .transcript.txt?
- 用户/审计需要保留原 Whisper 输出(可能是模型对比、回放)
- Level 4 结果是"二次加工",写成独立文件可追溯
- 与 FR-2.15b (transcript 落盘) 模式一致

落盘文件名(2026-09-10 厘清):活跃路径是 `streaming.py::_maybe_refine` **内联**
写 `<stem>.refined.txt`(Level 4 产物),不经过下面的 `write_cleaned_transcript()`
helper。那个 helper 目前只剩两个调用方:`quality/pipeline.py`(写
`<stem>.cleaned.txt`,但该模块**只被 `__init__` re-export,全仓无实例化**)
和 `scripts/spike_refiner_integration.py`(spike)。Level 1(`clean_transcript`)
结果自 2026-09-10 起**不落盘**。

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

from vla.config import VLAConfig
from vla.llm.client import LLMClient, LLMClientLike
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


# 分块精修的块数上限(FR-2.15c 2026-09-10)。
# 每块 ≤ refine_max_chars(默认 6000)⇒ 上限 ≈ 36000 字 ≈ 2 小时语音。
# 超过就只精修前 _MAX_CHUNKS 块、其余保原文 —— 明确退化,不静默(打 warning + 记 notes)。
_MAX_CHUNKS = 6


def _split_by_lines(text: str, max_chars: int) -> list[str]:
    """把 `text` 按**行边界**切成 ≤`max_chars` 的块(FR-2.15c 2026-09-10)。

    为什么按行切:转写产物是段落行结构(实测 252 行 / 平均 36 字每行),
    按行切**不会切断句子**;按固定字数切会把半句话分给两块,LLM 各自补全
    就会产生两倍的重复内容。

    无损保证:`"".join(_split_by_lines(t, n)) == t` —— 任何一块都不丢。
    返回的文本会被 `process_asset` 当成 canonical 转写落盘,丢一块就是内容
    从正式产物里静默消失。

    单行本身超过 `max_chars` 时没得选,只能硬切(实际语料下罕见)。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        # 单行超长:先把攒着的吐掉,再把这一行硬切成整块
        if len(line) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            while len(line) > max_chars:
                chunks.append(line[:max_chars])
                line = line[max_chars:]
            current = line
            continue

        if current and len(current) + len(line) > max_chars:
            chunks.append(current)
            current = ""
        current += line

    if current:
        chunks.append(current)
    return chunks


# ---------------- 主类 ----------------


class SubtitleRefiner:
    """字幕语义清理(2026-09-02 Level 4)。

    用法:
        refiner = SubtitleRefiner(config, llm_client)
        result = refiner.refine(text, title="xxx")
        # result.cleaned_text → 活跃路径写 <stem>.refined.txt(见 streaming.py)
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

    def _resolve_llm(self) -> LLMClientLike:
        """取 LLM 客户端;没注入就按 config **惰性构造**一个。

        为什么不在 __init__ 里构造(2026-09-10):
        构造期就要凭据 → `build_text_provider(cfg)` 在没加载 .env 的环境(CI /
        单测)直接炸 `OpenAIError: Missing credentials`,而装配路径本身与
        凭据无关(`SubtitleRefiner(cfg)` 构造期零依赖)。

        为什么要兜底而不是"没注入就报错"(2026-09-10 契约变更):
        `build_text_provider` 在 `refine_enabled=true` 且未注入时自动建
        `SubtitleRefiner(cfg)`(**不带 LLM**)→ `vla process --real-provider` 必崩。
        装配方只保证 `Xxx(cfg)`,不保证 `set_llm`;自给自足才能让"忘注入"
        不再等于"功能消失"。与 `VideoSummarizer._resolve_llm` / `QualityChecker`
        同一契约:构造期零依赖 + 类内惰性构造。

        注意:本方法在 `refine()` 的 `try` **之外**调用 —— 凭据真缺失时照常上抛
        (不重蹈 FR-2.15d 那次"异常被宽 except 吞成静默不产出")。
        """
        if self._llm is None:
            self._llm = LLMClient(self.config.llm_client, model=self.model)
        return self._llm

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
        1. 分块:未超 `refine_max_chars` → 单块(与旧实现逐字节等价);
           超限 → 按**行边界**切块(FR-2.15c 2026-09-10 二次修正)
        2. 逐块调 LLM(system + user prompt)
        3. 解析 JSON,按原顺序拼回 → RefinementResult
        4. 任何环节失败 → **该块**保留原文 + notes 记原因(不抛错)

        为什么分块,而不是被回退的那版"截断精修前段 + 尾部原文接回":
        那版只精修前 `refine_max_chars` 字,尾部原样接回 —— 但系统提示第 1 条
        要求"繁简统一",于是**头部被转成简体、尾部仍是 Whisper 原生繁体**。
        质量 LLM 据此报"后半段繁简混杂"而 fail,而那个混杂**是精修自己造的**。
        真机 30 分钟 Python 课就是这么被挡下的。

        为什么块失败时"保该块原文"而不是"整体回退原文":
        已成功块的 token 已经花掉,整体回退等于白丢;而门控看到该块的混杂是
        **真实的**(它确实没被清理过)。

        LLM 客户端:显式 `set_llm()` 注入优先;没注入则按 cfg 惰性构造
        (见 `_resolve_llm`)。凭据缺失时构造抛错照常上抛。

        Raises:
            Exception: LLM 客户端惰性构造失败(如凭据缺失)。调用失败本身
                **不抛错** —— 走 fallback 保留原文(见下)。
        """
        llm = self._resolve_llm()
        max_chars = self.config.quality_check.refine_max_chars

        # 单块:与旧实现同一条路径,不动任何语义
        if len(text) <= max_chars:
            result, ok = self._refine_once(llm, text, title)
            logger.info(
                "✨ LLM 清理完成: %d → %d 字符, %d 条修正%s",
                len(text), len(result.cleaned_text), len(result.corrections),
                "" if ok else "(fallback)",
            )
            return result

        chunks = _split_by_lines(text, max_chars)

        # 护栏:块数过多 → 只精修前 _MAX_CHUNKS 块,其余保原文。
        # 明确退化,不静默 —— 既打 warning,也在 notes 里留痕。
        raw_remainder = ""
        guard_note = ""
        if len(chunks) > _MAX_CHUNKS:
            raw_remainder = "".join(chunks[_MAX_CHUNKS:])
            chunks = chunks[:_MAX_CHUNKS]
            guard_note = (
                f"分块数超出上限 {_MAX_CHUNKS},仅精修前 {_MAX_CHUNKS} 块,"
                f"其余 {len(raw_remainder)} 字保留原文"
            )
            logger.warning("📏 %s", guard_note)

        parts: list[str] = []
        corrections: list[Correction] = []
        chunk_notes: list[str] = []
        failed: list[int] = []

        for idx, chunk in enumerate(chunks, start=1):
            sub, ok = self._refine_once(llm, chunk, title)
            parts.append(sub.cleaned_text)
            corrections.extend(sub.corrections)
            if not ok:
                failed.append(idx)
            if sub.notes:
                chunk_notes.append(f"块{idx}:{sub.notes}")

        cleaned_text = "".join(parts) + raw_remainder

        summary = f"分块精修 {len(chunks)} 块"
        if failed:
            summary += f",第 {'/'.join(str(i) for i in failed)} 块失败,保留原文"
        if guard_note:
            summary += f";{guard_note}"
        notes = f"{summary};{' | '.join(chunk_notes)}" if chunk_notes else summary

        logger.info(
            "✨ LLM 分块清理完成: %d 块,%d → %d 字符, %d 条修正%s",
            len(chunks), len(text), len(cleaned_text), len(corrections),
            f"({len(failed)} 块失败)" if failed else "",
        )
        return RefinementResult(
            original_text=text,
            cleaned_text=cleaned_text,
            corrections=corrections,
            notes=notes,
            model=self.model,
        )

    def _refine_once(
        self,
        llm: LLMClientLike,
        chunk: str,
        title: str,
    ) -> tuple[RefinementResult, bool]:
        """精修**一块** —— 分块路径的基本单元。

        Returns:
            (result, ok)。`ok=False` 表示走了失败 fallback;调用方据此在
            `notes` 里点名哪一块没精修过。
        """
        original_text = chunk

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            title=title or "(无标题)",
            char_count=len(chunk),
            text=chunk,
        )
        # 把 system + user 拼成一段(LLMClientLike.complete 是单 prompt 接口)
        full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_prompt}"

        try:
            # 输出 token 上限:用户配置的 refine_max_output_tokens,并按输入长度放大。
            #
            # 2026-09-10 修正:旧公式 `len(text) + 1000` 是按 MiniMax 的内联
            # `<think>` 标定的,**不够**。实测 deepseek-flash 处理 2507 字输入
            # 需要 3268 completion tokens(≈1.3 token/字 —— 输出是 JSON 转义后
            # 的文本 + corrections,比输入更长),旧公式只给 3507,余量仅 7%,
            # 输入再长一点就被 max_tokens 截断 → content 空 → 静默回退原文。
            # 改成 ×2 留余量。
            #
            # 另一半防线在 LLMClientConfig.reasoning_effort:默认 "none" 关掉
            # 推理(deepseek-flash 默认会为一句话的任务烧 16000+ reasoning tokens)。
            cfg_max = self.config.quality_check.refine_max_output_tokens
            output_max_tokens = max(cfg_max, len(chunk) * 2 + 1000)
            response = llm.complete(
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
            ), False

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
            ), False

        cleaned_text = str(data.get("cleaned_text", "")).strip()
        if not cleaned_text:
            logger.warning("⚠️ LLM 清理返回空 cleaned_text,使用原始文本")
            return RefinementResult(
                original_text=original_text,
                cleaned_text=original_text,
                corrections=[],
                notes="LLM 返回空 cleaned_text,fallback 原始文本",
                model=self.model,
            ), False

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

        return RefinementResult(
            original_text=original_text,
            cleaned_text=cleaned_text,
            corrections=corrections,
            notes=str(data.get("notes", "")),
            model=self.model,
            # prompt_tokens / completion_tokens 留待后续接 openai SDK usage 时填
        ), True

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
