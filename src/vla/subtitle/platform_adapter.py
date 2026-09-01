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

    按 `register()` 顺序遍历,首个 `match(url)` 返回 True 的类被实例化返回。
    每次 `get_for_url` 调用都新建实例,避免状态污染。
    """

    def __init__(self) -> None:
        self._adapters: list[type] = []

    def register(self, adapter_cls: type) -> None:
        """注册一个 adapter 类;重复注册同一类会被忽略。"""
        if adapter_cls not in self._adapters:
            self._adapters.append(adapter_cls)

    def list_adapters(self) -> list[type]:
        """返回所有已注册的 adapter 类(注册顺序)。"""
        return list(self._adapters)

    def get_for_url(self, url: str) -> Any | None:
        """按注册顺序找首个匹配 URL 的 adapter,返回新实例。

        None 表示无匹配 → 调用方应降级或跳过策略 ①。
        """
        for cls in self._adapters:
            if cls.match(url):
                return cls()
        return None
