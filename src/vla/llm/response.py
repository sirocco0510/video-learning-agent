"""LLM JSON response parser(SSOT: spec §B #6,2026-09-03)。

封装 brace-counting + think-block stripping + code-block scanning,作为所有
LLM 调用方(quality_checker / refiner)的统一解析入口。

为什么不用更简单的 regex:
- thinking model(MiniMax M2 / DeepSeek R1)会在输出前先输出 <think>...</think>,
  内含示例 JSON 会干扰普通 regex
- LLM 输出可能含未转义的引号 / nested 嵌套,brace-counting 配合 string-boundary
  处理比 regex 更稳
"""

from __future__ import annotations

import json
import re
from typing import Any


def parse_json_response(
    text: str,
    *,
    strip_think: bool = True,
    try_code_blocks: bool = True,
) -> dict[str, Any]:
    """从 LLM 响应中提取 JSON dict。

    策略顺序:
    1. 剥 `<think>...</think>` 块(strip_think=True 时)
    2. 扫 ```...``` 代码块,brace-counting 解析(try_code_blocks=True 时)
    3. 扫所有 `{` 起点,brace-counting 找 outermost {...}(跳过 think / code block 区域)

    Raises:
        ValueError: 没找到任何合法 JSON 对象
    """
    if strip_think:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Collect skip regions: think blocks always, code blocks always
    # so brace scan doesn't pick up JSON inside them.
    skip_regions: list[tuple[int, int]] = []
    for m in re.finditer(r"<think>.*?</think>", text, re.DOTALL):
        skip_regions.append((m.start(), m.end()))
    for m in re.finditer(r"```(?:json)?\s*\n?.*?\n?```", text, re.DOTALL):
        skip_regions.append((m.start(), m.end()))

    def _in_skip(idx: int) -> bool:
        return any(s <= idx < e for s, e in skip_regions)

    if try_code_blocks:
        for m in re.finditer(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL):
            inner = m.group(1).strip()
            data = _try_parse_balanced_object(inner)
            if data is not None:
                return data

    for match in re.finditer(r"\{", text):
        if _in_skip(match.start()):
            continue
        data = _try_parse_balanced_object(text, start=match.start())
        if data is not None:
            return data

    raise ValueError(f"LLM 响应中没有找到 JSON: {text[:200]}")


def _try_parse_balanced_object(
    text: str, start: int = 0,
) -> dict[str, Any] | None:
    """从 text[start] 开始 brace-counting(尊重字符串边界),parse outermost {...}。

    Returns:
        解析成功 → dict
        解析失败 → None(调用方继续尝试下一个起点)
    """
    if not text or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None