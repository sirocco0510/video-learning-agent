"""探针策略抽象(SSOT: docs/superpowers/plans/R-14-probe-strategy.md + spec §E Sub-3)。

把"预探测链"(head / referer / cookie warmup 等)换成一个可插拔的策略注册表:

- `ProbeStrategy` Protocol — duck typing 接口(name / match / run)
- `ProbeRegistry`         — 按注册顺序迭代,过滤掉 match(url) = False 的项
- `ProbeContext`          — 共享资源(session / page / cfg;至少一个非 None)
- `ProbeResult`           — ok + note + extra(给调用方足够上下文)

新增平台探针 = 一个新类 + 一个 `register()`,不动已有逻辑。
F2-8:此模块与 src/vla/subtitle/probes/ 一并保留,等 F2-14 接入
PlatformAdapter.prefetch_url(预探测 URL 是否能拿到 cookie / referer)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ProbeContext:
    """共享给所有探针的资源。

    Attributes:
        session: requests.Session 或 None(由调用方决定是否给)
        page:    playwright.Page 或 None
        cfg:     VLAConfig 或其子集(每个探针按需读自己的字段)
    """

    session: Any
    page: Any
    cfg: Any


@dataclass
class ProbeResult:
    """单个探针的运行结果。

    Attributes:
        ok:    True = 探针认为 url 可访问 / 业务可达;False = 否则
        note:  简短说明(主要用于调试 / 日志,不做业务判断)
        extra: 透传字段(给下游需要更详细信息的探针用,如最终重定向 URL)
    """

    ok: bool
    note: str = ""
    extra: dict = field(default_factory=dict)


@runtime_checkable
class ProbeStrategy(Protocol):
    """一个探针 = 决定是否对当前 url 生效 + 跑一次副作用检测。

    实现只需要提供同名属性/方法(结构子类型),不需要显式继承,
    方便各平台各自的探针放在不同文件里。
    """

    name: str

    def match(self, url: str) -> bool: ...

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult: ...


class ProbeRegistry:
    """按注册顺序迭代;适配层用这个代替硬编码 if/elif 链。

    - register(strategy): 追加;同名校验交由调用方(或用 register_unique)。
    - get_all_for(url):  保留**注册顺序**;只返回 match(url) == True 的探针。
    """

    def __init__(self) -> None:
        self._strategies: list[ProbeStrategy] = []

    def register(self, strategy: ProbeStrategy) -> None:
        self._strategies.append(strategy)

    def get_all_for(self, url: str) -> list[ProbeStrategy]:
        return [s for s in self._strategies if s.match(url)]
