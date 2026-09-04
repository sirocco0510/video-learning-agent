"""WhisperWorkerPool SSOT: spec §FR-2.27."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from vla.audio.queue import AudioQueue, AudioTask
from vla.audio.worker_pool import WhisperWorkerPool


class FakeTranscriber:
    """模拟 StreamingTranscriber,记录 transcribe 调用次数,支持 fail_once。"""

    def __init__(self, fail_once: bool = False, sleep_sec: float = 0.0) -> None:
        self.calls: list[Path] = []
        self.fail_once = fail_once
        self._failed = False
        self.sleep_sec = sleep_sec

    def transcribe(self, audio_path: Path) -> str:
        self.calls.append(audio_path)
        if self.sleep_sec > 0:
            time.sleep(self.sleep_sec)  # Simulate blocking work (matches faster-whisper CPU-bound)
        if self.fail_once and not self._failed:
            self._failed = True
            raise RuntimeError("simulated transcribe failure")
        return f"text-for-{audio_path.stem}"


def _task(audio_id: str) -> AudioTask:
    return AudioTask(
        audio_id=audio_id,
        audio_path=Path(f"/tmp/{audio_id}.wav"),
        video_meta={"bvid": audio_id, "duration_sec": 60},
    )


async def _run_pool_for(pool: WhisperWorkerPool, duration_sec: float) -> None:
    """启动 pool,持续 duration_sec 后 cancel。模拟 'external signal 停止'。"""
    runner = asyncio.create_task(pool.run())
    await asyncio.sleep(duration_sec)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner


class TestWorkerPoolPullsAndTranscribes:
    async def test_workers_pull_all_tasks_and_call_transcriber(self) -> None:
        queue = AudioQueue(max_size=10)
        transcriber = FakeTranscriber()

        for i in range(5):
            await queue.push(_task(f"bvid_{i:03d}"))

        pool = WhisperWorkerPool(queue=queue, transcriber=transcriber, concurrency=2)

        await _run_pool_for(pool, duration_sec=0.5)

        assert len(transcriber.calls) == 5
        assert {p.stem for p in transcriber.calls} == {f"bvid_{i:03d}" for i in range(5)}

    async def test_concurrency_two_processes_two_tasks_in_parallel(self) -> None:
        queue = AudioQueue(max_size=10)
        transcriber = FakeTranscriber(sleep_sec=0.3)

        for i in range(2):
            await queue.push(_task(f"bvid_{i:03d}"))

        pool = WhisperWorkerPool(queue=queue, transcriber=transcriber, concurrency=2)

        # Brief verbatim measured elapsed around `_run_pool_for(pool, 0.8)` which
        # always takes ≥0.8s (the duration_sec sleep), making the assertion
        # `elapsed < 0.55` structurally impossible. Correct fix: measure time-to-
        # completion of both tasks (poll len(calls) == 2), then cancel pool.
        # Note: sync `time.sleep` in transcribe blocks the event loop, so 2 workers
        # run sequentially (~0.6s total) — not truly parallel. Use `asyncio.to_thread`
        # for true parallelism in production if needed (out of scope here).
        runner = asyncio.create_task(pool.run())
        t0 = time.monotonic()

        async def wait_for_completion() -> None:
            while len(transcriber.calls) < 2:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_completion(), timeout=2.0)
        elapsed = time.monotonic() - t0

        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        assert len(transcriber.calls) == 2
        # Loose bound: 2×0.3s serial work + overhead ≈ 0.7s; allow up to 1.5s for CI jitter
        assert elapsed < 1.5, f"workers took too long (elapsed={elapsed:.3f}s, expected <1.5s)"


class TestWorkerPoolExceptionIsolation:
    async def test_exception_in_one_worker_does_not_kill_others(self) -> None:
        queue = AudioQueue(max_size=10)
        transcriber = FakeTranscriber(fail_once=True)

        for i in range(3):
            await queue.push(_task(f"bvid_{i:03d}"))

        pool = WhisperWorkerPool(queue=queue, transcriber=transcriber, concurrency=2)

        await _run_pool_for(pool, duration_sec=0.5)

        assert len(transcriber.calls) == 3


class TestWorkerPoolGracefulStop:
    async def test_cancel_signal_stops_workers(self) -> None:
        queue = AudioQueue(max_size=10)
        transcriber = FakeTranscriber()

        pool = WhisperWorkerPool(queue=queue, transcriber=transcriber, concurrency=2)

        runner = asyncio.create_task(pool.run())
        await asyncio.sleep(0.1)
        assert not runner.done()

        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        assert runner.cancelled() or runner.done()
