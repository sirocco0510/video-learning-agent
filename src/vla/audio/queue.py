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