"""VideoSummarizer — 单视频 200-300 字摘要(SSOT: requirements.md FR-2.15d,2026-09-10)。

职责:
- 接 cleaned_text(Refiner 输出后) → 调云端 LLM 生成 200-300 字摘要
- 触发条件(2026-09-10 修正):**无条件** —— 摘要是关键路径,不设长度门控。
  是否走摘要只看"视频是否通过质量门控",不看字符多少
- 超长输入(> ~12000 字)送 LLM 前**三明治抽样**(头/中/尾各 4000 字),
  避免纯截断丢后半段,同时覆盖视频开场 / 主体 / 收尾关键位置
- 输出 SummaryResult;失败 fallback(不抛错,主流程不中断)
- 落盘 helper:`summaries/<id>_<safe_title>.summary.txt`(与 transcripts/ 分目录)

LLM 注入契约(2026-09-10):
- `VideoSummarizer(cfg)` 构造期**零依赖**(不需要 api_key),同 QualityChecker /
  SubtitleRefiner —— 装配方(如 build_text_provider)可以无凭据地建对象
- LLM 走延迟路径:`set_llm()` 显式注入优先,没注入则首次 summarize_one 时
  按 `cfg.llm_client` **惰性构造**(见 `_resolve_llm`)
- 为什么兜底:调用方漏注入时,旧实现抛 RuntimeError → 被 process_asset 的
  宽 except 吞掉 → 摘要静默不产出。自给自足才能保证 FR-2.15d 真的落地。

为什么不在 Refiner 里做:
- Refiner 是"清理"(preserve original length),与"压缩"语义不同
- Refiner 输入是 transcript,摘要输入是 cleaned_text(更干净,压缩效果更好)
- 长视频同时保留 refined.txt(全文本)+ .summary.txt(摘要),职责清晰

配额归类:
- 项目 SSOT:"云端 LLM 限定两件事: ① 字幕质量检查 ② 6h 批量总结"
- 本类归入 ①(字幕质量相关,单视频精炼版)
- 调用方负责 QuotaManager 控制;本类**不**自己加限流

失败 fallback:
- LLM 调用失败 / 解析失败 / quota 用完 → 返回空 SummaryResult + notes 说明
- 不抛错(主流程不因摘要失败中断)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from vla.config import VLAConfig
from vla.llm.client import LLMClient, LLMClientLike
from vla.models import SummaryResult


logger = logging.getLogger(__name__)


# ---------------- Prompt 设计 ----------------

_SYSTEM_PROMPT = """你是视频内容摘要助手,任务是把一段较长的视频字幕压缩成精华摘要。

【输入】一段已经过云端清理(Refiner)的中文视频字幕。

【任务】
1. 提取核心知识点(概念 / 方法 / 结论 / 关键案例)
2. 按逻辑顺序组织(开场 → 主体 → 收尾)
3. 保留可操作的信息(代码片段 / 步骤 / 数字)
4. 跳过寒暄 / 重复 / 口水话
5. 用简洁的中文,不要用 Markdown 标题 / 列表(连续段落即可)

【字数】约 250 字(可接受 200-300 范围)。**不要逐字计数** — 按自然段落
长度直接写即可,差别不超过 50 字都没关系。

【输出格式 — 严格 JSON,只输出 JSON】
{
  "summary_text": "摘要正文(连续段落)"
}
"""


_USER_PROMPT_TEMPLATE = """【视频标题】
{title}

【字幕内容(共 {char_count} 字符,以下为摘要输入)
{text}

请按系统指令输出 JSON。"""


# 送 LLM 前的最大 prompt chars(粗估)。
# 12000 字是 ~3000-4000 tokens,加上 system + user prompt 模板约 ~5000 tokens,
# 留出余量给 200-300 字输出。**每条视频都走摘要** —— 短于 12000 字直接整段送,
# 超过才抽样(见下)。
#
# 三明治抽样常量(SSOT: 2026-09-10):头/中/尾各 4000 字,总预算 12000。
# 视频字幕信息密度不均:开头导入 / 中间口水话 + 代码演示 / 结尾结论。
# 三明治覆盖关键位置,一次 LLM call,避免纯截断丢失后半段内容。
_SANDWICH_HEAD = 4000
_SANDWICH_MID = 4000
_SANDWICH_TAIL = 4000
_PROMPT_MAX_CHARS = _SANDWICH_HEAD + _SANDWICH_MID + _SANDWICH_TAIL  # = 12000


def sandwich_sample(text: str) -> str:
    """三明治抽样:头 + 中 + 尾,覆盖关键位置,丢掉中间冗余口水话。

    短文本(≤ _PROMPT_MAX_CHARS)→ 原样返回,不切。
    长文本 → 取头 4000 + 中 4000 + 尾 4000,中间用省略标记连接。

    为什么不用 Map-Reduce:
    - 切片独立摘要再合并 → 3-5x token 成本,失去段落间上下文连贯
    - 视频字幕中间常是"那我们看一下代码啊"等口水话,独立摘要反而稀释
    - 三明治抽样对单视频总摘要性价比最高(2026-09-10 设计决定)

    Args:
        text: 原始文本(中文视频字幕)

    Returns:
        抽样后的文本(总长 ≈ head + mid + tail + 2 个省略标记)
    """
    if len(text) <= _PROMPT_MAX_CHARS:
        return text

    head = text[:_SANDWICH_HEAD]
    n = len(text)
    mid_start = n // 2 - _SANDWICH_MID // 2
    mid_end = mid_start + _SANDWICH_MID
    mid = text[mid_start:mid_end]
    tail = text[-_SANDWICH_TAIL:]

    omitted = n - _SANDWICH_HEAD - _SANDWICH_MID - _SANDWICH_TAIL
    separator = f"\n\n[...中间省略 {omitted} 字...]\n\n"

    return f"{head}{separator}{mid}{separator}{tail}"


# ---------------- 主类 ----------------


class VideoSummarizer:
    """单视频 200-300 字摘要(2026-09-10)。

    用法:
        summarizer = VideoSummarizer(config, llm_client)
        result = summarizer.summarize_one(cleaned_text, title="xxx")
        if result.summary_text:
            summarizer.write_summary(out_path, result)
    """

    def __init__(
        self,
        config: VLAConfig,
        llm: LLMClientLike | None = None,
    ) -> None:
        self.config = config
        self._llm = llm

    def set_llm(self, llm: LLMClientLike) -> None:
        """延迟注入 LLM 客户端(同 QualityChecker / SubtitleRefiner 模式)。"""
        self._llm = llm

    def _resolve_llm(self) -> LLMClientLike:
        """取 LLM 客户端;没注入就按 config **惰性构造**一个。

        为什么不在 __init__ 里构造(2026-09-10):
        构造期就要凭据 → `build_text_provider(cfg)` 在没加载 .env 的环境(CI /
        单测)直接炸 `OpenAIError: Missing credentials`,而装配路径本身与
        凭据无关(同 QualityChecker / SubtitleRefiner:`Xxx(cfg)` 构造期零依赖)。

        为什么要兜底而不是"没注入就报错"(2026-09-10 修复):
        调用方 `build_text_provider` 忘注入 → 旧实现抛 RuntimeError →
        被 `process_asset` 的 `except Exception` 吞成一条 warning →
        **FR-2.15d 静默不产出摘要**。自给自足才能让"忘注入"不再等于"没摘要"。
        """
        if self._llm is None:
            self._llm = LLMClient(self.config.llm_client, model=self.model)
        return self._llm

    @property
    def enabled(self) -> bool:
        """是否启用 — 永远启用(FR-2.15d 关键路径,无长度门控,不需要开关)。"""
        return True

    @property
    def model(self) -> str:
        """R-10:统一从 cfg.llm.refine_model 取值(同 Refiner,复用模型)。

        历史语义在 VLAConfig._migrate_legacy_llm_keys 的 pre-validator 里实现。
        """
        return self.config.llm.refine_model

    # ---------------- 主流程 ----------------

    def summarize_one(
        self,
        text: str,
        title: str = "",
    ) -> SummaryResult:
        """生成单视频 200-300 字摘要。

        流程:
        1. 无条件生成 —— FR-2.15d(2026-09-10):摘要是**关键路径**,不设长度门控,
           短视频同样必须产出摘要。是否调用本方法由调用方按"质量门控通过"决定。
        2. 超长输入走三明治抽样(头 4000 + 中 4000 + 尾 4000),覆盖关键位置
        3. 调 LLM(system + user prompt)
        4. 解析 JSON → SummaryResult
        5. 任何环节失败 → 返回空 SummaryResult + notes 记录

        历史:2026-09-10 之前这里有一道 `len(text) <= refine_max_chars → 返回空`
        的门控,已删除。原因:① 摘要产出与否不该取决于字符长度;② 该长度会被
        Refiner 压缩影响,导致摘要**静默不产出**。

        Raises:
            Exception: LLM 客户端惰性构造失败(如环境变量缺 api_key)——
                这是配置错误,故意不吞,让它带明确原因往上抛而非伪装成"摘要为空"。
                单次 LLM 调用失败仍走 fallback(返回空 SummaryResult + notes)。
        """
        # 惰性构造(未注入时按 config 自建)——
        # 放在 try 之外:构造失败是配置错误,不该被下面的"调用失败"fallback 吞掉。
        llm = self._resolve_llm()

        # 超长输入走三明治抽样(头/中/尾),避免纯截断丢后半段(SSOT: 2026-09-10)
        input_text = sandwich_sample(text)
        truncated = input_text != text

        if truncated:
            logger.info(
                "📏 transcript %d 字符 > _PROMPT_MAX_CHARS %d,三明治抽样后送 LLM",
                len(text), _PROMPT_MAX_CHARS,
            )

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            title=title or "(无标题)",
            char_count=len(text),  # 报告原始长度(让 LLM 知道全文多长)
            text=input_text,  # sandwich 已自带"[...中间省略 N 字...]"标记
        )
        full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_prompt}"

        # 调 LLM — max_tokens=4000 兼顾 reasoning model 的  think 块 + 200-300 字 JSON 输出。
        # reasoning model(M2.7 / R1)会在 think 块里数中文字符 / 规划段落,
        # 实际消耗 ~3000+ tokens,然后才输出 JSON。需要更大窗口。
        try:
            response = llm.complete(full_prompt, max_tokens=4000, temperature=0.3)
        except Exception as e:
            logger.warning("⚠️ LLM 摘要调用失败,返回空 SummaryResult:%s", e)
            return SummaryResult(
                summary_text="",
                notes=f"LLM 调用失败:{type(e).__name__}:{str(e)[:100]}",
                model=self.model,
            )

        # 解析 JSON
        try:
            from vla.llm.response import parse_json_response
            data = parse_json_response(response)
        except (ValueError, Exception) as e:
            logger.warning("⚠️ LLM 摘要响应解析失败:%s", e)
            return SummaryResult(
                summary_text="",
                notes=f"LLM 响应解析失败:{e}",
                model=self.model,
            )

        summary_text = str(data.get("summary_text", "")).strip()
        if not summary_text:
            logger.warning("⚠️ LLM 摘要返回空 summary_text")
            return SummaryResult(
                summary_text="",
                notes="LLM 返回空 summary_text",
                model=self.model,
            )

        # 字数统计(中文字符 + 英文单词混合)
        word_count = len(summary_text)
        logger.info(
            "✨ 单视频摘要完成:%d 字 → %d 字(原 transcript %d 字符)",
            word_count, word_count, len(text),
        )

        return SummaryResult(
            summary_text=summary_text,
            notes="",
            model=self.model,
        )

    # ---------------- 文件落盘 helper ----------------

    def write_summary(
        self,
        summary_path: Path,
        result: SummaryResult,
    ) -> Path | None:
        """把 SummaryResult 落盘到 `<id>.summary.txt`。

        文件格式:
            {summary_text}

            ---
            generated_at: 2026-09-10T15:30:00
            model: MiniMax-M2.7-highspeed
            source_chars: 12345

        空 SummaryResult → 不写文件,返回 None(让调用方根据 None 判断跳过)。

        Returns:
            写入的路径,或 None(没生成摘要时)。
        """
        if not result.summary_text:
            # 调用方应根据 None 判断"该视频没生成摘要"
            return None

        summary_path = Path(summary_path)
        lines: list[str] = [result.summary_text, "", "---", ""]
        lines.append(f"generated_at: {datetime.now().isoformat(timespec='seconds')}")
        lines.append(f"model: {result.model}")
        if result.notes:
            lines.append(f"notes: {result.notes}")

        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("💾 摘要已落盘:%s (%d 字)", summary_path, len(result.summary_text))
        return summary_path
