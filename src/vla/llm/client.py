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
from typing import Protocol, runtime_checkable

import openai

from vla.config import LLMClientConfig


logger = logging.getLogger(__name__)


# OpenAI 官方默认 base_url(BASE_URL env 未设置时回退)
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


@runtime_checkable
class LLMClientLike(Protocol):
    """LLM 客户端 duck typing 接口(SSOT — 唯一来源在 llm/client.py)。

    所有调用模块(checker / refiner / summarizer)用 `from vla.llm.client import LLMClientLike`,
    不要再各自定义(SSOT: spec §A #2)。
    """

    def complete(
        self, prompt: str, max_tokens: int = 1000, temperature: float = 0.3,
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
    ) -> str:
        """chat completion,返回字符串响应。

        Args:
            prompt: 用户 prompt
            max_tokens: 最大生成 token 数(默认 1000 — 短文本评估够用)
            temperature: 采样温度(默认 0.3 — 稳定 + 不完全 deterministic)

        Returns:
            模型返回的 message.content

        Raises:
            openai.OpenAIError: 网络 / API 错误 / 限流等
        """
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""