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
