"""InternalSiteAdapter(SSOT: requirements.md FR-2.18 + implementation-plan.md Phase 3.4 + F2-7 + Phase 9.6)。

公司内部视频网站 adapter。

Phase 9.6 (2026-09-09) 实装 bill-jc.com 域:`fetch_via_spider` 委托给
`InternalSiteSpider.fetch_m3u8(kng_id)`,返回 `(None, {"video_url": m3u8_url,
"via": "internal_spider"})`。`strategy.get_subtitle` 在 ① 与 ② 之间探测这个
方法,命中后 fetch_asset 路径 ② 走 `extract_m3u8_audio`。

2026-09-10 轻量化:tab_recorder 依赖已删除(原 F2-7 4 deps 缩为 3 deps)。
signature 仍允许 transcriber/screenshot_controller 是因为 fetch_via_recording
的兜底路径(虽然 bill-jc 实际不触发)可能用到 transcriber,而测试 fixture
习惯性注入 audio_factory + transcriber。

匹配规则:`b-learning.bill-jc.com` / `internal.example.com` / `video.corp.local`。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from vla.subtitle.platform_adapter import PlatformAdapter

if TYPE_CHECKING:
    from vla.audio.source_factory import AudioSourceFactory
    from vla.capture.screenshot_phase_controller import ScreenshotPhaseController
    from vla.subtitle.internal_site_spider import InternalSiteSpider
    from vla.transcribe.streaming import AudioTranscriber


logger = logging.getLogger(__name__)


# 预定义内部域名集合;后续可由 config.platforms.internal.domains 覆盖
_INTERNAL_DOMAINS: tuple[str, ...] = (
    "b-learning.bill-jc.com",   # Phase 9.6:bill-jc 内部学习平台
    "internal.example.com",
    "video.corp.local",
)

# bill-jc 视频 URL 模板:https://b-learning.bill-jc.com/learn/<kng_id>
_BILL_JC_LEARN_RE = re.compile(r"/learn/([^/?#]+)")


class InternalSiteAdapter(PlatformAdapter):
    """公司内部视频网站 adapter(Phase 9.6:bill-jc.com 域接 InternalSiteSpider)。"""

    def __init__(
        self,
        *,
        audio_factory: "AudioSourceFactory | None" = None,
        transcriber: "AudioTranscriber | None" = None,
        screenshot_controller: "ScreenshotPhaseController | None" = None,
        spider: "InternalSiteSpider | None" = None,  # Phase 9.6:bill-jc 用
    ) -> None:
        # Round 3 修复:所有 deps 默认 None — 让 `register(InternalSiteAdapter)`
        # 类注册 fallback 在 internal_spider=None 时仍能实例化,避免生产 CLI
        # 走 bill-jc URL 时 fetch_asset 的兜底 except 把 TypeError 静默吃掉。
        # 真实 fetch_via_spider 不依赖这些 deps(只调 spider.fetch_m3u8)。
        self._audio_factory = audio_factory
        self._transcriber = transcriber
        self._screenshot_controller = screenshot_controller
        # Phase 9.6:可空 — 兼容 _stub_deps() 测试 fixture(无 spider 仍能实例化)
        self._spider = spider

    @classmethod
    def match(cls, url: str) -> bool:
        """匹配预定义的内部域名。"""
        return any(domain in url for domain in _INTERNAL_DOMAINS)

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """无平台 API → 返回 None(让 strategy 走 ② 浏览器探测)。"""
        return None

    def fetch_browser_subtitle(
        self, driver: Any, url: str
    ) -> tuple[str, dict] | None:
        """bill-jc 无 DOM 字幕;返回 None。"""
        return None

    def fetch_via_spider(
        self, url: str
    ) -> tuple[None, dict] | None:
        """Phase 9.6:解析 URL → kng_id → 调 InternalSiteSpider.fetch_m3u8。

        Returns:
            (None, {"video_url": m3u8_url, "via": "internal_spider"}):
            strategy.get_subtitle 把这包成 SubtitleResult(source="internal_spider"),
            fetch_asset 路径 ② 看到 source.startswith("internal") 接管抽音。
            None:spider 未注入 / URL 不含 /learn/<id> / spider 抛错(已 log)。
        """
        if self._spider is None:
            logger.debug("fetch_via_spider 跳过:_spider 未注入")
            return None
        m = _BILL_JC_LEARN_RE.search(url)
        if not m:
            logger.debug("fetch_via_spider 跳过:URL 不含 /learn/<kng_id>: %s", url)
            return None
        kng_id = m.group(1)
        try:
            m3u8_url = self._run_spider_fetch(self._spider, kng_id)
        except Exception as e:
            logger.warning("fetch_via_spider 失败 kng=%s:%s", kng_id, e)
            return None
        return None, {"video_url": m3u8_url, "via": "internal_spider"}

    @staticmethod
    def _run_spider_fetch(spider: "InternalSiteSpider", kng_id: str) -> str:
        """隔离的同步→异步桥:让 fetch_via_spider 保持同步签名,
        内部用 asyncio.run 跑 spider.fetch_m3u8(coroutine)。

        strategy 在 await get_subtitle 里通过 to_thread 调本方法,本方法在新
        event loop 里跑 spider — 避免 greenlet / 当前 loop 嵌套的报错。
        """
        import asyncio
        return asyncio.run(spider.fetch_m3u8(kng_id))

    def fetch_via_recording(
        self, driver: Any, url: str, duration_sec: int
    ) -> tuple[str, dict] | None:
        """bill-jc 已走 spider 路径,无需录屏兜底;返回 None。"""
        return None
