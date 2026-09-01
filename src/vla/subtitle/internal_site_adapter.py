"""InternalSiteAdapter stub(SSOT: requirements.md FR-2.18 + implementation-plan.md Phase 3.4)。

公司内部视频网站 adapter 占位实现。

当前状态:
- 无 API 格式(等公司下发账号后接入)
- 无字幕提取逻辑(等拿到页面结构后实现)
- 三个 fetch 方法全部返回 None,让 strategy 优雅降级

匹配规则:`internal.example.com` / `video.corp.local` 等预定义内部域名集合。
后续可由配置驱动(platforms.internal.domains)。
"""

from __future__ import annotations

from typing import Any


# 预定义内部域名集合;后续可由 config.platforms.internal.domains 覆盖
_INTERNAL_DOMAINS: tuple[str, ...] = (
    "internal.example.com",
    "video.corp.local",
)


class InternalSiteAdapter:
    """公司内部视频网站 adapter stub(占位实现)。"""

    @classmethod
    def match(cls, url: str) -> bool:
        """匹配预定义的内部域名。"""
        return any(domain in url for domain in _INTERNAL_DOMAINS)

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """stub: 无 API,返回 None。"""
        return None

    def fetch_browser_subtitle(
        self, driver: Any, url: str
    ) -> tuple[str, dict] | None:
        """stub: 等拿到页面结构后实现。"""
        return None

    def fetch_via_recording(
        self, driver: Any, url: str, duration_sec: int
    ) -> tuple[str, dict] | None:
        """stub: 录屏兜底可后续接入,目前也返回 None。"""
        return None