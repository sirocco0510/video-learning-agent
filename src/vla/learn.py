"""课程目录批量转写入口(SSOT: requirements.md FR-11,2026-09-10)。

与 `vla batch` 的分工:
- `vla batch` 吃**手写的任务列表文件**,URL 由调用方自己提供。
- `vla learn` 自己从 bill-jc **课程目录页**翻页取任务(catalogId + collegeId),
  边取边转,直到累计配额用尽或目录翻完。

为什么单独一个模块,而不是改 main.py:
- 要求是"批量转写不影响单条转写逻辑",所以单条链路
  (`fetch_asset` / `process_asset` / `VideoLearningAgent.run`)**语义零改动**。
- 本模块只做两件事,且都通过既有接口缝合:
  ① 翻页取任务 —— 复用 spider.list_tasks(offset 版,见 FR-11);
  ② 回填真实时长 —— **包一层 fetch_asset**,asset 到手后、process_asset
     看到它之前改 task.expected_duration(main.py:_process_one 的调用顺序
     Step 1 → Step 2 保证了这一点)。

时长回填为什么可行且免费:
- bill-jc 路径 ② 必须先抽音(m3u8 → wav)才有得转写,所以包装器运行时
  wav **一定已经在磁盘上** —— ffprobe 是纯读,不产生额外下载。
- VideoTask.expected_duration 由 list_tasks 填 3600 占位(目录 API 不返时长),
  这个值参与两处计算:quota 累加(6h 触发)和 quality 的 char_per_second 门控。
  占位值会让 quota 明显高估、cps 明显低估 —— 回填是修正,不是优化。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from vla.audio.source_factory import probe_duration
from vla.models import Asset, VideoTask


logger = logging.getLogger(__name__)


FetchAssetFn = Callable[[VideoTask], Awaitable[Asset | None]]

# agent.run 返回的统计键(与 VideoLearningAgent.run 对齐)
_STAT_KEYS = ("processed", "passed", "failed", "skipped", "summarized")


class _SpiderLike(Protocol):
    async def list_tasks(
        self, *, catalog_id: str, limit: int = 10, offset: int = 0
    ) -> list[VideoTask]: ...


class _AgentLike(Protocol):
    quota: Any

    async def run(self, tasks: list[VideoTask]) -> dict[str, int]: ...


# ---------------- 时长回填 ----------------


def with_duration_resolution(
    fetch_asset: FetchAssetFn,
    probe: Callable[[Any], int] | None = None,
) -> FetchAssetFn:
    """包一层 fetch_asset:asset 到手后把 ffprobe 实测时长写回 task。

    只改 `task.expected_duration`,不动 asset 本身、不构造新对象 ——
    下游 process_asset 看到的一切照旧。

    探测失败(probe 返 0)时**保留原值**:覆盖成 0 会让
    char_per_second = 字符数 / 0 → inf,踩爆 max_char_per_second 上限,
    把正常视频误判成幻觉。宁可留着占位值。

    Args:
        fetch_asset: 真实输入链(通常是 build_text_provider 的返回值之一)
        probe: 时长探测函数;None = 模块级 probe_duration(ffprobe 那支)。
            默认值在**调用时**解析而非 def 时绑定,否则 monkeypatch
            `vla.learn.probe_duration` 对这层包装无效。

    Returns:
        同签名的新 fetch_asset
    """
    resolve_probe = probe if probe is not None else probe_duration

    async def wrapped(task: VideoTask) -> Asset | None:
        asset = await fetch_asset(task)
        if asset is None or asset.audio_path is None:
            return asset
        real = resolve_probe(asset.audio_path)
        if real > 0:
            logger.info(
                "⏱️ %s 时长实测 %ds(替换占位 %ds)", task.id, real, task.expected_duration
            )
            task.expected_duration = real
        else:
            logger.warning(
                "⏱️ %s 时长探测失败,保留占位 %ds", task.id, task.expected_duration
            )
        return asset

    return wrapped


# ---------------- 翻页循环 ----------------


async def iter_course_tasks(
    spider: _SpiderLike,
    catalog_id: str,
    limit: int = 10,
) -> AsyncIterator[list[VideoTask]]:
    """按页产出目录下的 VideoTask 列表,直到空页 —— 翻页代数的唯一定义处。

    代数(2026-09-10 定稿):offset 每次 += limit,即**页大小 == 步长**。
    两者必须相等,否则每页会漏掉 (页大小 - limit) 条 —— 目录 API 的
    limit 参数就是页大小,所以 list_tasks 内部也用 limit 当页大小
    (见 internal_site_spider.list_tasks)。

    页内条数 < limit **不**当作翻完:服务端若把页大小上限压到 limit 以下
    (如只给 16 而 limit=30),"不满页 = 最后一页"这个常见假设会静默截断
    整个课程。**只认空页**。

    Yields:
        list[VideoTask],每次一页(保证非空 —— 空页直接结束迭代)
    """
    offset = 0
    page_no = 0
    while True:
        page = await spider.list_tasks(catalog_id=catalog_id, limit=limit, offset=offset)
        if not page:
            logger.info("🏁 翻页结束:offset=%d 无更多视频(共 %d 页)", offset, page_no)
            return
        page_no += 1
        logger.info(
            "📄 第 %d 页:offset=%d,取到 %d 条(limit=%d)", page_no, offset, len(page), limit
        )
        yield page
        offset += limit


async def run_course_batch(
    *,
    spider: _SpiderLike,
    agent: _AgentLike,
    catalog_id: str,
    limit: int = 10,
) -> dict[str, int]:
    """按目录翻页取任务 → 逐页交给 agent.run,直到配额用尽或翻完。

    停法只有两条:
      ① 翻完(spider 返回空页)→ iter_course_tasks 自然结束
      ② agent.quota.should_continue() 为 False —— 累计配额到且 stop_session

    "这一页视频都已经转写过"**不需要特判**:agent.run 内部按
    logs/transcribed_history.jsonl 去重,返回 processed=0,循环照常翻页
    (这正是要的行为 —— 已转写不是"停",是"跳过继续")。

    Args:
        spider: InternalSiteSpider(或同签名 stub)
        agent: VideoLearningAgent(fetch_asset 应已套 with_duration_resolution)
        catalog_id: 目录 ID(从课程目录页 URL 的 catalogId 参数拿)
        limit: 每页取多少条 + 翻页步长(默认 10)

    Returns:
        {"pages": 翻了几页, processed/passed/failed/skipped/summarized: 各页累加}
    """
    stats: dict[str, int] = {"pages": 0, **{k: 0 for k in _STAT_KEYS}}

    async for page in iter_course_tasks(spider, catalog_id, limit):
        stats["pages"] += 1

        page_stats = await agent.run(page)
        for k in _STAT_KEYS:
            stats[k] += page_stats.get(k, 0)

        if not agent.quota.should_continue():
            logger.info(
                "🛑 累计配额已到(%.1fh)且 on_exhausted=stop_session → 停止翻页",
                agent.quota.current / 3600,
            )
            break

    return stats
