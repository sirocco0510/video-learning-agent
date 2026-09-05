"""PlatformAdapter 抽象 + Registry(SSOT: requirements.md FR-2.0 + implementation-plan.md Phase 3.0)。

每个视频平台(B站、内部网站、未来 YouTube 等)实现 PlatformAdapter,
提供 3 个 fetch 方法(API / 浏览器 / 录屏);Registry 按注册顺序匹配 URL 域名。

注:Protocol 只描述方法签名,不强制类型,允许 duck typing。
具体实现类不需要显式继承 Protocol,只需方法签名匹配即可。
"""

from __future__ import annotations

from typing import Any, Protocol


class PlatformAdapter(Protocol):
    """视频平台字幕适配器契约。

    实现类建议用 `@classmethod` 装饰 `match`,并提供 3 个实例方法。
    不强制继承本 Protocol,只要方法签名匹配即可(duck typing)。
    """

    @classmethod
    def match(cls, url: str) -> bool:
        """该 adapter 能否处理此 URL。"""
        ...

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """策略 ①:平台公开 API(httpx)。"""
        ...

    def fetch_browser_subtitle(
        self, driver: Any, url: str
    ) -> tuple[str, dict] | None:
        """策略 ②:Puppeteer 通用 JS 探测。driver 是 playwright Browser 实例。"""
        ...

    def fetch_via_recording(
        self, driver: Any, url: str, duration_sec: int
    ) -> tuple[str, dict] | None:
        """策略 ③:Puppeteer 录屏扩展 + Whisper。"""
        ...


class PlatformAdapterRegistry:
    """平台适配器注册表。

    支持两种注册方式(2026-09-02 扩展):
    - `register(adapter_cls)` — 无依赖 adapter,用 class 注册;每次 get_for_url
      返回新实例(状态隔离,测试友好)。
    - `register_instance(adapter)` — 带依赖 adapter(如 BilibiliAdapter 需要
      `official` 和 `recorder`),用 pre-built 实例注册,get_for_url 直接返回
      这个实例(共享 deps,每次同一对象)。

    实例优先于类匹配(同一 URL 实例先命中,再 fallback 到 class)。
    """

    def __init__(self) -> None:
        self._classes: list[type] = []
        self._instances: list[Any] = []

    def register(self, adapter_cls: type) -> None:
        """注册一个 adapter 类;重复注册同一类会被忽略。"""
        if adapter_cls not in self._classes:
            self._classes.append(adapter_cls)

    def register_instance(self, adapter: Any) -> None:
        """注册一个 pre-built adapter 实例(带 deps 的 adapter 用这个)。"""
        # 不去重 — 调用方应自己保证不重复;实例共享 deps 是预期行为
        self._instances.append(adapter)

    def list_adapters(self) -> list[type]:
        """返回所有已注册的 adapter 类(注册顺序)。

        注:实例不暴露在这里(legacy 兼容 — 旧测试只看 class list)。
        """
        return list(self._classes)

    def list_instances(self) -> list[Any]:
        """返回所有已注册的 adapter 实例(注册顺序,2026-09-02 新增)。"""
        return list(self._instances)

    def get_for_url(self, url: str) -> Any | None:
        """按注册顺序找首个匹配 URL 的 adapter。

        实例优先匹配;类匹配命中时返回新实例(避免状态污染)。

        None 表示无匹配 → 调用方应降级或跳过策略 ①。
        """
        # 1. 实例优先(pre-built,带 deps)
        for inst in self._instances:
            if inst.match(url):
                return inst
        # 2. 类(无 deps,每次新建)
        for cls in self._classes:
            if cls.match(url):
                return cls()
        return None
