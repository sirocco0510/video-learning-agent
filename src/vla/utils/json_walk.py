"""json_walk utilities(SSOT: R-13 plan,2026-09-03)。

两个递归 helper:
- `walk_strings(value, visit)` → 返回新值(纯函数)
- `walk_strings_inplace(value, visit)` → 原地变异容器,无返回

约束:
- 纯函数,无 I/O / 无 logging / 无 LLM 调用
- 递归处理 dict / list / tuple / str / primitives
- 深度嵌套(测试覆盖 ≥3 层)
- **只递归 value,不动 dict key**(key 原样保留)
- str leaf 调 `visit(s)`;int / float / bool / None 原样保留
"""

from __future__ import annotations

from typing import Any, Callable


# 字符串转换函数签名:接收一个 str,返回一个新 str
VisitFn = Callable[[str], str]


def walk_strings(value: Any, visit: VisitFn) -> Any:
    """递归遍历 value,把每个 str leaf 用 `visit(s)` 替换,返回新值。

    - dict → 新 dict,**只递归 value,key 原样保留**
    - list → 新 list,element 递归
    - tuple → 新 tuple,element 递归
    - str → `visit(value)`
    - int / float / bool / None → 原样保留

    Examples:
        >>> walk_strings("hi", lambda s: s.upper())
        'HI'
        >>> walk_strings({"a": "b"}, lambda s: s.upper())
        {'a': 'B'}
        >>> walk_strings([1, "two"], lambda s: s.upper())
        [1, 'TWO']
    """
    if isinstance(value, str):
        return visit(value)
    if isinstance(value, dict):
        return {k: walk_strings(v, visit) for k, v in value.items()}
    if isinstance(value, list):
        return [walk_strings(item, visit) for item in value]
    if isinstance(value, tuple):
        return tuple(walk_strings(item, visit) for item in value)
    # int / float / bool / None / 其他 primitive:passthrough
    return value


def walk_strings_inplace(value: Any, visit: VisitFn) -> None:
    """原地变异容器,把每个 str leaf 用 `visit(s)` 替换,无返回值。

    与 walk_strings 不同:
    - dict 和 list **就地修改**(调用方持有的对象被改变)
    - **dict key 不动**(只递归 value)
    - tuple 不可变 → no-op(无容器可改,递归终止)
    - str leaf 单独传入时也是 no-op(str 不可变)
    - 返回 None(签名即"无返回值")

    适用场景:
    - 已经构造好的 segment dict 列表(避免再次分配)
    - 大数据(避免 copy 开销)

    Examples:
        >>> d = {"a": "x"}
        >>> walk_strings_inplace(d, lambda s: s.upper())
        >>> d
        {'a': 'X'}
    """
    if isinstance(value, str):
        return  # str 不可变,原地无法改;调用方应使用 walk_strings
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, str):
                value[k] = visit(v)
            else:
                walk_strings_inplace(v, visit)
        return
    if isinstance(value, list):
        for i in range(len(value)):
            item = value[i]
            if isinstance(item, str):
                value[i] = visit(item)
            else:
                walk_strings_inplace(item, visit)
        return
    # int / float / bool / None / tuple:无容器可改,no-op
    return