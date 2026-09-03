"""LLM prompt 工具(SSOT: spec §B #7,2026-09-03)。

集中 system + user 拼接 + JSON-only 约束追加,避免每个 LLM 调用模块
重复硬编码。
"""

from __future__ import annotations


def build_chat_prompt(system: str, user: str) -> str:
    """拼接 system + user prompt。

    LLMClientLike.complete() 只接受单字符串,所以调用方需要预先 join。

    规则:
    - system 与 user 都非空:用 ``\\n\\n`` 分隔
    - system 为空:省略前导空行,只保留单个 ``\\n`` 防止丢上下文
    - user 为空:保留尾部 ``\\n\\n``,LLM 容易把"无 user"识别为 system 延续
    """
    if not system:
        return f"\n{user}"
    return f"{system}\n\n{user}"


def enforce_json_response(system: str, *, extra: str = "只输出 JSON") -> str:
    """在 system prompt 末尾追加"只输出 JSON"约束。

    - 默认追加"只输出 JSON"
    - 已包含则跳过(避免 prompt 膨胀)
    """
    if extra in system:
        return system
    return f"{system}\n\n{extra}"
