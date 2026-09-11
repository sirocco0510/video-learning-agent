"""课程目录批量转写入口(SSOT: requirements.md FR-9.7,2026-09-10)。

覆盖两块:
1. `run_course_batch` 翻页循环的停法(空页 / 配额用尽 / 全页已转写仍要翻页)
2. `with_duration_resolution` 的时长回填分支(含"探测失败不覆盖"这条)

设计前提:agent.run / list_tasks 都是**既有接口**,本模块不碰单条转写语义。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vla.learn import iter_course_tasks, run_course_batch, with_duration_resolution
from vla.models import Asset, VideoTask


# ---------------- Stub ----------------


def _task(n: int) -> VideoTask:
    return VideoTask(
        id=f"kng-{n}",
        title=f"视频{n}",
        url=f"https://b-learning.bill-jc.com/kng/#/video/play?kngId=kng-{n}",
        expected_duration=3600,  # 占位值 —— 真实值靠 with_duration_resolution 回填
    )


class FakeSpider:
    """按 offset 返回预置页;越界返回 []。记录每次调用的 (catalog_id, limit, offset)。"""

    def __init__(self, pages: dict[int, list[VideoTask]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, int, int]] = []

    async def list_tasks(
        self, *, catalog_id: str, limit: int = 10, offset: int = 0
    ) -> list[VideoTask]:
        self.calls.append((catalog_id, limit, offset))
        return self.pages.get(offset, [])


class FakeQuota:
    """最小 quota stub —— 只模拟 run_course_batch 用到的 QuotaManager 接口。

    current 忠实照搬 QuotaManager.current(公开属性,停止日志会读它)。
    continue_after = 跑完第 N 页之后 should_continue() 变 False。
    """

    def __init__(self, continue_after: int | None = None) -> None:
        self.continue_after = continue_after
        self.pages_seen = 0
        self.current = 0  # 秒;真实 QuotaManager 同名字段

    def should_continue(self) -> bool:
        if self.continue_after is None:
            return True
        return self.pages_seen < self.continue_after


class FakeAgent:
    """记录每次 run() 拿到的任务;返回预置统计。"""

    def __init__(self, stats_per_page: list[dict[str, int]] | None = None) -> None:
        self.quota = FakeQuota()
        self.seen: list[list[VideoTask]] = []
        self._stats = stats_per_page or []
        self._n = 0

    async def run(self, tasks: list[VideoTask]) -> dict[str, int]:
        self.seen.append(list(tasks))
        self.quota.pages_seen += 1
        if self._n < len(self._stats):
            s = self._stats[self._n]
        else:
            s = {"processed": len(tasks), "passed": len(tasks),
                 "failed": 0, "skipped": 0, "summarized": 0}
        self._n += 1
        return dict(s)


# ---------------- 翻页世代器 ----------------


class TestIterCourseTasks:
    """翻页代数的唯一定义处 —— run_course_batch 与 --dry-run 都走它。"""

    async def test_yields_pages_until_empty(self) -> None:
        spider = FakeSpider({0: [_task(1)], 10: [_task(2), _task(3)], 20: []})

        pages = [p async for p in iter_course_tasks(spider, "cat-1", limit=10)]

        assert [[t.id for t in p] for p in pages] == [["kng-1"], ["kng-2", "kng-3"]]
        assert [c[2] for c in spider.calls] == [0, 10, 20]

    async def test_never_yields_empty_page(self) -> None:
        """空页只用于终止,不作为页产出 —— 否则调用方要多判一次。"""
        spider = FakeSpider({0: []})

        pages = [p async for p in iter_course_tasks(spider, "cat-1")]

        assert pages == []

    async def test_early_break_stops_requests(self) -> None:
        """调用方 break(如配额到)→ 不再发后续请求(生成器惰性求值)。"""
        spider = FakeSpider({0: [_task(1)], 10: [_task(2)], 20: []})

        seen: list[str] = []
        async for page in iter_course_tasks(spider, "cat-1", limit=10):
            seen.extend(t.id for t in page)
            break

        assert seen == ["kng-1"]
        assert [c[2] for c in spider.calls] == [0], "break 之后不该再拉第二页"


# ---------------- 翻页循环 ----------------


class TestRunCourseBatch:
    async def test_stops_when_page_empty(self) -> None:
        """翻到空页 → 停(这是唯一的"翻完了"信号)。"""
        spider = FakeSpider({0: [_task(1), _task(2)], 10: [], 20: []})
        agent = FakeAgent()

        stats = await run_course_batch(
            spider=spider, agent=agent, catalog_id="cat-1", limit=10
        )

        assert len(agent.seen) == 1, "空页不该再进 agent"
        assert spider.calls == [("cat-1", 10, 0), ("cat-1", 10, 10)]
        assert stats["processed"] == 2
        assert stats["pages"] == 1

    async def test_advances_offset_by_limit(self) -> None:
        """offset 每次递增 limit —— 步长必须等于页大小,否则漏视频。"""
        spider = FakeSpider({0: [_task(1)], 6: [_task(2)], 12: []})
        agent = FakeAgent()

        await run_course_batch(spider=spider, agent=agent, catalog_id="cat-1", limit=6)

        assert [c[2] for c in spider.calls] == [0, 6, 12]
        assert all(c[1] == 6 for c in spider.calls), "limit 应作为页大小透传"

    async def test_catalog_id_forwarded(self) -> None:
        """catalog_id 透传(用户从目录页 URL 拿,见 SKILL.md 批量模式)。"""
        spider = FakeSpider({0: []})
        agent = FakeAgent()

        await run_course_batch(
            spider=spider, agent=agent, catalog_id="4ff8c024-219c-4e49-91d0-ec869bcf859f"
        )

        assert spider.calls[0][0] == "4ff8c024-219c-4e49-91d0-ec869bcf859f"

    async def test_stops_when_quota_exhausted(self) -> None:
        """累计配额到 + on_exhausted=stop_session → 不再翻页。"""
        spider = FakeSpider({0: [_task(1)], 10: [_task(2)], 20: []})
        agent = FakeAgent()
        agent.quota.continue_after = 1  # 第一页之后 should_continue() 变 False

        await run_course_batch(spider=spider, agent=agent, catalog_id="cat-1", limit=10)

        assert len(agent.seen) == 1
        assert [c[2] for c in spider.calls] == [0], "配额用尽后不该再发 API 请求"

    async def test_all_deduped_page_still_advances(self) -> None:
        """整页都已转写(processed=0)→ **照常翻页**,不是停。

        这是用户要的行为:"如果没达到累计配额或者已经该页视频都已经转写过
        需要翻页"。现成 agent.run 去重后 processed=0,循环无需特判。
        """
        spider = FakeSpider({
            0: [_task(1), _task(2)],
            10: [_task(3)],
            20: [],
        })
        agent = FakeAgent(stats_per_page=[
            {"processed": 0, "passed": 0, "failed": 0, "skipped": 2, "summarized": 0},
            {"processed": 1, "passed": 1, "failed": 0, "skipped": 0, "summarized": 0},
        ])

        stats = await run_course_batch(
            spider=spider, agent=agent, catalog_id="cat-1", limit=10
        )

        assert [c[2] for c in spider.calls] == [0, 10, 20], "全已转写的页必须继续翻"
        assert stats["skipped"] == 2
        assert stats["processed"] == 1

    async def test_merges_stats_across_pages(self) -> None:
        """多页统计累加,不是覆盖。"""
        spider = FakeSpider({0: [_task(1)], 10: [_task(2)], 20: []})
        agent = FakeAgent(stats_per_page=[
            {"processed": 1, "passed": 1, "failed": 0, "skipped": 0, "summarized": 0},
            {"processed": 1, "passed": 0, "failed": 1, "skipped": 0, "summarized": 1},
        ])

        stats = await run_course_batch(
            spider=spider, agent=agent, catalog_id="cat-1", limit=10
        )

        assert stats["processed"] == 2
        assert stats["passed"] == 1
        assert stats["failed"] == 1
        assert stats["summarized"] == 1
        assert stats["pages"] == 2

    async def test_partial_page_does_not_stop_paging(self) -> None:
        """页内条数 < limit **不**当作翻完 —— 继续翻。

        服务端若把页大小上限压到 limit 以下(如只给 16 而 limit=30),
        "不满页 = 最后一页"这个常见假设会静默截断整个课程。只认空页。
        """
        spider = FakeSpider({0: [_task(1)], 30: [], 60: []})
        agent = FakeAgent()

        await run_course_batch(spider=spider, agent=agent, catalog_id="cat-1", limit=30)

        assert [c[2] for c in spider.calls] == [0, 30], "不满页仍要再翻一次确认"


# ---------------- 时长回填 ----------------


class TestWithDurationResolution:
    async def test_writes_real_duration_into_task(self, tmp_path: Path) -> None:
        """asset 到手后,把 ffprobe 测到的真实时长写回 task.expected_duration。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")
        task = _task(1)

        async def fake_fetch(t: VideoTask) -> Asset | None:
            return Asset(text=None, source="whisper_internal_download",
                         audio_path=wav, deletable=True)

        wrapped = with_duration_resolution(fake_fetch, probe=lambda p: 1234)
        asset = await wrapped(task)

        assert asset is not None
        assert task.expected_duration == 1234

    async def test_returns_same_asset_object(self, tmp_path: Path) -> None:
        """透传 —— 包装器不构造新 Asset、不改 asset 字段。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")
        task = _task(1)
        original = Asset(text=None, source="whisper_internal_download",
                         audio_path=wav, deletable=True)

        async def fake_fetch(t: VideoTask) -> Asset | None:
            return original

        wrapped = with_duration_resolution(fake_fetch, probe=lambda p: 60)
        assert await wrapped(task) is original

    async def test_probe_failure_does_not_overwrite(self, tmp_path: Path) -> None:
        """probe 返 0(ffprobe 缺失/超时/解析失败)→ **保留原值**。

        覆盖成 0 会让 char_per_second = chars/0 → inf,直接踩爆
        max_char_per_second 上限,把好视频判成幻觉。宁可留着占位值。
        """
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")
        task = _task(1)

        async def fake_fetch(t: VideoTask) -> Asset | None:
            return Asset(text=None, source="whisper_internal_download",
                         audio_path=wav, deletable=True)

        wrapped = with_duration_resolution(fake_fetch, probe=lambda p: 0)
        await wrapped(task)

        assert task.expected_duration == 3600

    async def test_text_only_asset_leaves_task_alone(self) -> None:
        """字幕已就绪(API/Browser 命中)→ 没有 wav 可探,不动 task。"""
        task = _task(1)
        called: list[Path] = []

        async def fake_fetch(t: VideoTask) -> Asset | None:
            return Asset(text="已有字幕", source="api", audio_path=None)

        wrapped = with_duration_resolution(
            fake_fetch, probe=lambda p: called.append(p) or 999
        )
        await wrapped(task)

        assert called == [], "无 wav 时不该调 ffprobe"
        assert task.expected_duration == 3600

    async def test_none_asset_passes_through(self) -> None:
        """全路径失败(fetch 返 None)→ 原样返回,不炸。"""
        task = _task(1)

        async def fake_fetch(t: VideoTask) -> Asset | None:
            return None

        wrapped = with_duration_resolution(fake_fetch, probe=lambda p: 999)
        assert await wrapped(task) is None
        assert task.expected_duration == 3600

    async def test_uses_module_probe_duration_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不传 probe → 用 vla.learn.probe_duration(真实 ffprobe 那支)。"""
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....")
        task = _task(1)
        seen: list[Any] = []

        def fake_probe(p: Path) -> int:
            seen.append(p)
            return 77

        monkeypatch.setattr("vla.learn.probe_duration", fake_probe)

        async def fake_fetch(t: VideoTask) -> Asset | None:
            return Asset(text=None, source="whisper_internal_download",
                         audio_path=wav, deletable=True)

        await with_duration_resolution(fake_fetch)(task)

        assert seen == [wav]
        assert task.expected_duration == 77
