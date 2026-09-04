"""Audio transcription pipeline (SSOT: spec §FR-2.27)."""

from vla.audio.queue import AudioQueue, AudioTask
from vla.audio.worker_pool import WhisperWorkerPool

__all__ = ["AudioQueue", "AudioTask", "WhisperWorkerPool"]
