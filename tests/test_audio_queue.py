"""AudioQueue SSOT: spec §FR-2.27."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vla.audio.queue import AudioQueue, AudioTask


def _make_task(audio_id: str) -> AudioTask:
    return AudioTask(
        audio_id=audio_id,
        audio_path=Path(f"/tmp/{audio_id}.wav"),
        video_meta={"bvid": audio_id, "duration_sec": 60},
    )


class TestAudioQueuePushPull:
    async def test_push_then_pull_returns_same_task(self) -> None:
        queue = AudioQueue(max_size=10)
        task = _make_task("bvid_001")

        await queue.push(task)
        pulled = await asyncio.wait_for(queue.pull(), timeout=1.0)

        assert pulled == task
        assert pulled.audio_id == "bvid_001"
        assert pulled.audio_path == Path("/tmp/bvid_001.wav")
        assert pulled.video_meta["bvid"] == "bvid_001"

    async def test_pull_blocks_until_push(self) -> None:
        queue = AudioQueue(max_size=10)

        async def delayed_push() -> None:
            await asyncio.sleep(0.05)
            await queue.push(_make_task("bvid_002"))

        asyncio.create_task(delayed_push())
        pulled = await asyncio.wait_for(queue.pull(), timeout=1.0)

        assert pulled.audio_id == "bvid_002"


class TestAudioQueueFifo:
    async def test_tasks_returned_in_fifo_order(self) -> None:
        queue = AudioQueue(max_size=10)
        ids = [f"bvid_{i:03d}" for i in range(5)]

        for audio_id in ids:
            await queue.push(_make_task(audio_id))

        pulled_ids = []
        for _ in range(5):
            task = await asyncio.wait_for(queue.pull(), timeout=1.0)
            pulled_ids.append(task.audio_id)

        assert pulled_ids == ids


class TestAudioQueueCapacity:
    async def test_push_blocks_when_full_then_unblocks_after_pull(self) -> None:
        queue = AudioQueue(max_size=2)

        await queue.push(_make_task("first"))
        await queue.push(_make_task("second"))

        push_done = asyncio.Event()

        async def producer() -> None:
            await queue.push(_make_task("third"))
            push_done.set()

        asyncio.create_task(producer())

        # Brief verbatim had `await asyncio.wait_for(... timeout=0.05)` followed by
        # `assert not push_done.is_set()` — but wait_for raises TimeoutError when
        # producer correctly blocks (the FR-2.27 behavior we're testing for),
        # which short-circuits the assert. Use pytest.raises to catch the expected
        # timeout, then verify push_done is still unset.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(push_done.wait(), timeout=0.05)
        assert not push_done.is_set(), "producer must block until a slot frees"

        first = await asyncio.wait_for(queue.pull(), timeout=1.0)
        assert first.audio_id == "first"

        await asyncio.wait_for(push_done.wait(), timeout=1.0)
        assert push_done.is_set(), "producer should unblock after pull"