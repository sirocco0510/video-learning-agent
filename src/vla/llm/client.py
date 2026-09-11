"""LLMClient(SSOT: requirements.md FR-4.1 + implementation-plan.md Phase 5)。

职责:
- 统一 OpenAI 兼容协议,适配 OpenAI / Qwen / DeepSeek / Minimax 等
- chat.completions.create 薄包装,取 message.content
- 配置走 LLMClientConfig(api_key_env / base_url_env 是 env 名)

设计:
- 用 openai SDK(>=1.0),异步用 .chat.completions.create
- temperature=0.3 + max_tokens=1000 默认值,适合稳定的短文本评估
- 异常向上传播(FR-3.5 风格:调用方负责重试 / 记录失败)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, runtime_checkable

import openai

from vla.config import LLMClientConfig


logger = logging.getLogger(__name__)


# OpenAI 官方默认 base_url(BASE_URL env 未设置时回退)
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class LLMEmptyResponseError(RuntimeError):
    """LLM 返回空 content(2026-09-10)。

    最常见成因:reasoning model 的推理 token 把 max_tokens 吃光 —— 此时
    `finish_reason == "length"`,`message.content` 为 None/空。实测 deepseek-flash
    处理 2507 字输入要烧 16232 reasoning tokens,而调用方只给了 4000。

    以前这里是 `return ...message.content or ""`,把这种情况静默降级成空串,
    调用方只能看到下游的"JSON 解析失败",看不到真因。
    """

    def __init__(self, model: str, finish_reason: str | None) -> None:
        self.model = model
        self.finish_reason = finish_reason
        msg = f"LLM({model}) 返回空 content,finish_reason={finish_reason}"
        if finish_reason == "length":
            msg += (
                "  —— 响应被 max_tokens 截断。若为 reasoning model,推理 token"
                ' 会先吃满预算;可用 reasoning_effort="none" 关闭推理。'
            )
        super().__init__(msg)


@runtime_checkable
class LLMClientLike(Protocol):
    """LLM 客户端 duck typing 接口(SSOT — 唯一来源在 llm/client.py)。

    所有调用模块(checker / refiner / summarizer)用 `from vla.llm.client import LLMClientLike`,
    不要再各自定义(SSOT: spec §A #2)。
    """

    def complete(
        self,
        prompt: str,
        max_tokens: int = 1000,
        temperature: float = 0.3,
        reasoning_effort: str | None = None,
    ) -> str: ...


class LLMClient:
    """OpenAI 兼容协议的 LLM 客户端。"""

    def __init__(
        self,
        config: LLMClientConfig,
        model: str,
    ) -> None:
        """构造:读 env 变量(api_key_env / base_url_env 是**变量名**,值实时读)。

        Args:
            config: LLMClientConfig(provider / api_key_env / base_url_env)
            model: 模型名(如 "gpt-4o-mini");调用方决定(quality_check / summary
                各自有自己的 model 配置)
        """
        self._config = config
        self.model = model

        api_key = os.environ.get(config.api_key_env, "")
        base_url = os.environ.get(config.base_url_env) or _DEFAULT_OPENAI_BASE_URL

        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        logger.info(
            "LLMClient 初始化: model=%s base_url=%s api_key_env=%s",
            self.model, base_url, config.api_key_env,
        )

    def complete(
        self,
        prompt: str,
        max_tokens: int = 1000,
        temperature: float = 0.3,
        reasoning_effort: str | None = None,
    ) -> str:
        """chat completion,返回字符串响应。

        Args:
            prompt: 用户 prompt
            max_tokens: 最大生成 token 数(默认 1000 — 短文本评估够用)。
                ⚠️ reasoning model 下**推理 token 也计入这个预算**,给太小会
                导致 content 为空(见 LLMEmptyResponseError)。
            temperature: 采样温度(默认 0.3 — 稳定 + 不完全 deterministic)
            reasoning_effort: 覆盖 config 的 reasoning_effort。
                "none" 实测可完全关闭 deepseek-flash 的推理;
                None → 用 self._config.reasoning_effort(未配置则不传该参数)。

        Returns:
            模型返回的 message.content(保证非空白)

        Raises:
            openai.OpenAIError: 网络 / API 错误 / 限流等
            LLMEmptyResponseError: content 为空(通常 = max_tokens 被推理吃光)
        """
        effort = (
            reasoning_effort
            if reasoning_effort is not None
            else self._config.reasoning_effort
        )
        # 只在显式配置时带上,避免给不认识该参数的端点/模型传未知字段
        extra: dict[str, Any] = {}
        if effort is not None:
            extra["reasoning_effort"] = effort

        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            **extra,
        )
        choice = resp.choices[0]
        content = choice.message.content or ""
        finish_reason = getattr(choice, "finish_reason", None)

        # 2026-09-10:空 content 不再静默返回 "" —— 见 LLMEmptyResponseError
        if not content.strip():
            raise LLMEmptyResponseError(self.model, finish_reason)

        # 有内容但被截断:交给调用方的容错解析(_try_parse_truncated_json)处理,
        # 但要留下痕迹,不然"内容不完整"永远不可见。
        if finish_reason == "length":
            logger.warning(
                "⚠️ LLM(%s) 响应被 max_tokens=%d 截断(finish_reason=length),"
                "内容可能不完整。",
                self.model, max_tokens,
            )
        return content