"""InternalSiteSpider 占位 stub — 实现走单独 PR。

完整设计见 spec §4.7:
  4 个 yunxuetang API:
    1. POST /kng/kngCatalog/student/tree     (拿 catalog 树)
    2. POST /kng/knowledge/pagelist           (子目录视频列表)
    3. POST /kng/study/submit/preinit         (开 study session,等价"点开始学习")
    4. POST /kng/study/kngPlay                (拿 m3u8 URL)
  认证:Chrome CDP 借 SSO cookie(连 localhost:9222 → context.cookies())

本模块只占位,fetch_asset 在 source.startswith("internal") 分支接住
SubtitleResult(metadata={"video_url": "<m3u8>"}) 即可。Spider 实装不影响
fetch_asset 主流程。
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class InternalSiteSpider:
    """API-driven spider for b-learning.bill-jc.com (stub)。"""

    def __init__(self, cdp_url: str, college_id: str, resolution: str = "720p") -> None:
        self.cdp_url = cdp_url
        self.college_id = college_id
        self.resolution = resolution

    async def list_tasks(self) -> list:  # 实际返回 list[VideoTask]
        raise NotImplementedError(
            "InternalSiteSpider.list_tasks 实装走单独 PR(spec §4.7)"
        )

    async def fetch_m3u8(self, kng_id: str) -> str:
        raise NotImplementedError(
            "InternalSiteSpider.fetch_m3u8 实装走单独 PR(spec §4.7)"
        )
