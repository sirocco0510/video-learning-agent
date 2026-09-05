# F2-3 — Audio Queue + Whisper Worker Pool (FR-2.27)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Concurrent audio transcription with N workers pulling from a bounded asyncio.Queue (capacity 10), enabling throughput > 1× real-time when multiple audios are queued (FR-2.27).

**Architecture:**
- `src/vla/audio/queue.py` — `AudioTask` frozen dataclass + `AudioQueue` thin wrapper around `asyncio.Queue(maxsize=10)`
- `src/vla/audio/worker_pool.py` — `WhisperWorkerPool` spawns N workers that pull tasks, call `transcriber.transcribe()`, log exceptions, and continue
- `tests/test_audio_queue.py` — 3 tests (push/pull happy path, FIFO ordering, max_size blocks producer)
- `tests/test_audio_worker_pool.py` — 4 tests (workers pull, transcriber invoked, exception isolated, graceful stop)

**Tech Stack:** Python 3.12, asyncio (`asyncio.Queue`, `asyncio.create_task`, `asyncio.gather`), pytest-asyncio (`asyncio_mode = "auto"`), dataclasses (frozen), pathlib

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md` §3.3 (audio/queue.py) + §3.4 (audio/worker_pool.py)

## Global Constraints

- Queue capacity 10 (FR-2.27) — `max_size=10` is the literal default; any override must come from config
- Default worker concurrency 2 (FR-2.27 — Apple Silicon GPU single-card 上限)
- Config: `whisper.concurrent_workers` (already in `VLAConfig`)
- Concurrency safety: same `audio_id` never processed twice (asyncio.Queue FIFO + at-most-once consumption)
- `QuotaManager.add` atomic via `int +=` (CPython atomic, no lock needed)
- `probe_status` stateless, no lock
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"` (already in `pyproject.toml`)
- Bash commands MUST use `LANG=en_US.UTF-8` prefix
- TDD: write tests first, run → red, implement, run → green
- After each task: run `uv run pytest tests/test_audio_queue.py tests/test_audio_worker_pool.py -v`
- Final task: run `uv run vla doctor && uv run pytest -v`

---

### Task 1: Write failing tests for `AudioQueue` then implement

**Files:**
- Create: `src/vla/audio/__init__.py`
- Create: `src/vla/audio/queue.py`
- Create: `tests/test_audio_queue.py`

**Interfaces:**
- `AudioTask(audio_id: str, audio_path: Path, video_meta: dict)` — frozen dataclass
- `AudioQueue(max_size: int = 10)`
- `await queue.push(task: AudioTask) -> None`
- `await queue.pull() -> AudioTask`

- [ ] **Step 1: Create `src/vla/audio/__init__.py`**

```python
"""Audio transcription pipeline (SSOT: spec §FR-2.27)."""

from vla.audio.queue import AudioQueue, AudioTask

__all__ = ["AudioQueue", "AudioTask"]
```

- [ ] **Step 2: Write failing tests for `AudioQueue`**

Create `tests/test_audio_queue.py`:

```python
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

        await asyncio.wait_for(push_done.wait(), timeout=0.05)
        assert not push_done.is_set(), "producer must block until a slot frees"

        first = await asyncio.wait_for(queue.pull(), timeout=1.0)
        assert first.audio_id == "first"

        await asyncio.wait_for(push_done.wait(), timeout=1.0)
        assert push_done.is_set(), "producer should unblock after pull"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `LANG=en_US.UTF-8 uv run pytest tests/test_audio_queue.py -v`
Expected: `ModuleNotFoundError: No module named 'vla.audio'` (or `AttributeError` on missing `AudioQueue`)

- [ ] **Step 4: Implement `AudioQueue`**

Create `src/vla/audio/queue.py`:

```python
"""asyncio.Queue wrapper, 容量 10 (SSOT: spec §FR-2.27).

满则阻塞避免内存爆;FIFO + at-most-once 消费保证同一 audio_id 不会被两个 worker 同时处理。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioTask:
    """音频转写任务单元.

    Attributes:
        audio_id: 唯一标识(yt-dlp 路径用 bvid,Tab Audio Recorder 路径用 extension-assigned id)。
        audio_path: 已落盘的音频文件路径(.wav / .webm)。
        video_meta: 透传给下游 quality / summarize 的元数据(bvid / title / duration_sec / group_id)。
    """

    audio_id: str
    audio_path: Path
    video_meta: dict


class AudioQueue:
    """asyncio.Queue 包装,容量上限避免内存爆(FR-2.27)。

    Args:
        max_size: 队列容量上限。默认 10(FR-2.27)。满了 `push` 自动 await。
    """

    def __init__(self, max_size: int = 10) -> None:
        self._q: asyncio.Queue[AudioTask] = asyncio.Queue(maxsize=max_size)

    async def push(self, task: AudioTask) -> None:
        """入队。满了自动 await,直到有 worker `pull` 出队腾出空间。"""
        await self._q.put(task)

    async def pull(self) -> AudioTask:
        """出队。空时自动 await,直到有 producer `push`。"""
        return await self._q.get()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `LANG=en_US.UTF-8 uv run pytest tests/test_audio_queue.py -v`
Expected: 3 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/vla/audio/__init__.py src/vla/audio/queue.py tests/test_audio_queue.py
git commit -m "feat(audio): AudioQueue with capacity 10 (FR-2.27)"
```

---

### Task 2: Write failing tests for `WhisperWorkerPool` then implement

**Files:**
- Create: `src/vla/audio/worker_pool.py`
- Create: `tests/test_audio_worker_pool.py`
- Modify: `src/vla/audio/__init__.py` (export `WhisperWorkerPool`)

**Interfaces:**
- `WhisperWorkerPool(queue: AudioQueue, transcriber: Any, concurrency: int = 2)`
- `await pool.run() -> None` — blocks until external cancellation signal (caller cancels the task)
- `await pool._worker_loop() -> None` — internal, one worker iteration

- [ ] **Step 1: Write failing tests for `WhisperWorkerPool`**

Create `tests/test_audio_worker_pool.py`:

```python
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

        t0 = time.monotonic()
        await _run_pool_for(pool, duration_sec=0.8)
        elapsed = time.monotonic() - t0

        assert len(transcriber.calls) == 2
        # 两个并发 worker,每个 sleep 0.3s,期望总耗时 ~0.3-0.5s;串行则需要 ~0.6s
        assert elapsed < 0.55, f"workers did not run in parallel (elapsed={elapsed:.3f}s)"


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `LANG=en_US.UTF-8 uv run pytest tests/test_audio_worker_pool.py -v`
Expected: `ModuleNotFoundError: No module named 'vla.audio.worker_pool'`

- [ ] **Step 3: Implement `WhisperWorkerPool`**

Create `src/vla/audio/worker_pool.py`:

```python
"""N 个 worker 并发从 AudioQueue 拉任务,调 transcriber (SSOT: spec §FR-2.27).

Default 2 workers(Apple Silicon GPU single-card 上限)。
配置项: whisper.concurrent_workers。

异常隔离: 单个 task transcribe 抛异常不影响其他 worker / 后续 task。
停止语义: `run()` 由调用方通过 asyncio.cancel() 触发;worker 在当前 transcribe
完成(或异常)后、下一次 pull 前检查 cancellation。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from vla.audio.queue import AudioQueue, AudioTask


logger = logging.getLogger(__name__)


class WhisperWorkerPool:
    """并发 Whisper 转写 worker 池.

    Args:
        queue: 共享 AudioQueue 实例。
        transcriber: 任意具有 `transcribe(audio_path: Path) -> str` 的对象
                     (duck-type StreamingTranscriber,避免循环 import)。
        concurrency: worker 数。默认 2(FR-2.27)。
    """

    def __init__(
        self,
        queue: AudioQueue,
        transcriber: Any,
        concurrency: int = 2,
    ) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self.queue = queue
        self.transcriber = transcriber
        self.concurrency = concurrency

    async def run(self) -> None:
        """启动 N 个 worker,阻塞直到外部 cancel()."""
        tasks = [
            asyncio.create_task(self._worker_loop(), name=f"whisper-worker-{i}")
            for i in range(self.concurrency)
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _worker_loop(self) -> None:
        """单 worker 循环:pull → transcribe → 异常隔离."""
        while True:
            task = await self.queue.pull()
            try:
                text = self.transcriber.transcribe(task.audio_path)
                logger.info(
                    "whisper transcribe ok: audio_id=%s len=%d",
                    task.audio_id,
                    len(text),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 隔离单个任务失败
                logger.error(
                    "whisper worker failed: audio_id=%s err=%s",
                    task.audio_id,
                    e,
                    exc_info=True,
                )
```

- [ ] **Step 4: Update `src/vla/audio/__init__.py`**

Replace contents:

```python
"""Audio transcription pipeline (SSOT: spec §FR-2.27)."""

from vla.audio.queue import AudioQueue, AudioTask
from vla.audio.worker_pool import WhisperWorkerPool

__all__ = ["AudioQueue", "AudioTask", "WhisperWorkerPool"]
```

- [ ] **Step 5: Run worker pool tests to verify they pass**

Run: `LANG=en_US.UTF-8 uv run pytest tests/test_audio_worker_pool.py -v`
Expected: All 4 tests pass.

- [ ] **Step 6: Run full audio test suite**

Run: `LANG=en_US.UTF-8 uv run pytest tests/test_audio_queue.py tests/test_audio_worker_pool.py -v`
Expected: 7 tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/vla/audio/__init__.py src/vla/audio/worker_pool.py tests/test_audio_worker_pool.py
git commit -m "feat(audio): WhisperWorkerPool with concurrency 2 (FR-2.27)"
```

---

### Task 3: Integration smoke + regression + commit

**Files:**
- Read-only: `src/vla/audio/queue.py`, `src/vla/audio/worker_pool.py`, both test files

**Interfaces:**
- All audio queue + worker pool tests passing
- `vla doctor` green
- Existing test suite still green

- [ ] **Step 1: Run integration smoke test inline**

Run: `LANG=en_US.UTF-8 uv run python -c "
import asyncio
from pathlib import Path
from vla.audio.queue import AudioQueue, AudioTask
from vla.audio.worker_pool import WhisperWorkerPool

class T:
    def __init__(self): self.calls = []
    def transcribe(self, p): self.calls.append(p.stem); return f'ok-{p.stem}'

async def main():
    q = AudioQueue(max_size=10)
    t = T()
    pool = WhisperWorkerPool(queue=q, transcriber=t, concurrency=2)
    runner = asyncio.create_task(pool.run())
    for i in range(5):
        await q.push(AudioTask(audio_id=f'bvid_{i}', audio_path=Path(f'/tmp/bvid_{i}.wav'), video_meta={}))
    await asyncio.sleep(0.5)
    runner.cancel()
    try: await runner
    except asyncio.CancelledError: pass
    print('calls:', sorted(t.calls))

asyncio.run(main())
"`

Expected: `calls: ['bvid_0', 'bvid_1', 'bvid_2', 'bvid_3', 'bvid_4']`

- [ ] **Step 2: Run full pytest suite**

Run: `LANG=en_US.UTF-8 uv run pytest -v`
Expected: All existing tests still pass + 7 new audio tests pass.

- [ ] **Step 3: Run `vla doctor`**

Run: `LANG=en_US.UTF-8 uv run vla doctor`
Expected: All checks OK (or pre-existing warnings only, no new failures).

- [ ] **Step 4: Verify no regressions via audit grep**

Run:
```bash
grep -rn "class BrowserRecorder" src/vla/ | wc -l
grep -rn "WhisperWorkerPool\|AudioQueue" src/vla/audio/ | wc -l
ls src/vla/audio/queue.py src/vla/audio/worker_pool.py
ls tests/test_audio_queue.py tests/test_audio_worker_pool.py
```

Expected:
- `class BrowserRecorder` count: 0 (not our scope; sanity check)
- `WhisperWorkerPool\|AudioQueue` count: ≥ 3 (definition + 2 test references)
- All 4 files exist

- [ ] **Step 5: Final commit (only if anything changed in prior steps)**

```bash
git status
```

If clean → no commit. If dirty → review and commit with `git add <files> && git commit -m "chore(audio): verify F2-3 integration smoke passes"`.
