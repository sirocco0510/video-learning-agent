"""StreamingTranscriber(SSOT: requirements.md FR-3 + implementation-plan.md Phase 4)。

职责:
- 用 faster-whisper 把视频/音频文件转写成字幕文本
- 边转写边清理: 音频就绪后立即删视频源(FR-3.3)
- 音频文件保留(由 cleanup() / 调用方在质量检查通过后清理;FR-3.5 失败路径也会保留)

设计:
- AudioTranscriber Protocol(本模块定义,F2-4 设计)
- WhisperModel 懒加载(首次 transcribe 时才加载;FR-3.x:启动快)
- ffmpeg 子进程抽音轨(16kHz mono PCM s16le)
- faster-whisper VAD 过滤静音段(提速)
- 允许注入 model(测试用)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from faster_whisper import WhisperModel

from vla.config import VLAConfig
from vla.transcribe.postprocess import (
    DEFAULT_MIN_LINE_CHARS,
    DEFAULT_MIN_OVERLAP_CHARS,
    DEFAULT_MAX_LINE_CHARS,
    clean_transcript,
)


logger = logging.getLogger(__name__)


@runtime_checkable
class AudioTranscriber(Protocol):
    """音频转写器协议(F2-4 设计;F2-8 由 PlatformAdapter.fetch_via_recording 消费)。

    实现类需提供 `transcribe(audio_path) -> str` 和 `cleanup(audio_path) -> None`。
    isinstance 检查 duck typing — Python 只验证方法名存在,不严格匹配参数名,
    所以 StreamingTranscriber.transcribe(video_path) 仍 isinstance(this, AudioTranscriber)。
    """

    def transcribe(self, audio_path: Path) -> str: ...
    def cleanup(self, audio_path: Path) -> None: ...


class StreamingTranscriber:
    """faster-whisper 流式转写器(FR-3.1/3.2/3.3/3.4/3.5)。

    用法:
        transcriber = StreamingTranscriber(config)
        text = transcriber.transcribe(video_path)  # 返回字幕文本
        # ... 质量检查 ...
        StreamingTranscriber.cleanup(audio_path)   # 通过后再删
    """

    def __init__(
        self,
        config: VLAConfig,
        model: WhisperModel | None = None,
    ) -> None:
        self.config = config
        self._model = model  # None = 懒加载
        self._model_size = config.whisper.model
        self._compute_type = config.whisper.compute_type

    @property
    def model(self) -> WhisperModel:
        """懒加载 WhisperModel(首次访问时才构造,避免启动慢)。"""
        if self._model is None:
            logger.info(
                "加载 WhisperModel(size=%s, compute_type=%s)",
                self._model_size, self._compute_type,
            )
            self._model = WhisperModel(
                self._model_size,
                compute_type=self._compute_type,
            )
        return self._model

    # ---------------- AudioTranscriber Protocol 实现 ----------------

    def transcribe(self, video_path: Path) -> str:
        """把视频文件转写成字幕文本。

        流程:
        1. ffmpeg 抽音轨(16kHz 单声道 PCM s16le)
        2. 删除视频源(FR-3.3 边转写边清理 — 音频已就绪,原片冗余)
        3. faster-whisper 转写(beam_size=5, vad_filter=True)
        4. 返回 \\n 拼接的 segments 文本

        音频文件保留(由 cleanup() 在质量检查通过后删除;失败路径 FR-3.5
        也保留供排查)。

        Raises:
            RuntimeError: ffmpeg 抽音轨失败 / 未生成 wav 文件
            Exception: faster-whisper 转写异常(向上传播,FR-3.5 记录失败)
        """
        audio_path = video_path.with_suffix(".wav")
        self._extract_audio(video_path, audio_path)

        # FR-3.3: 音频就绪后立即删视频源(必须,不能等转写完才删)
        if video_path.exists():
            video_path.unlink()
            logger.info("🗑️ 删除视频源(FR-3.3): %s", video_path)

        # 转写(FR-3.1 + 3.2)
        segments, info = self.model.transcribe(
            str(audio_path),
            language=self.config.whisper.language,
            beam_size=5,
            vad_filter=True,
        )
        text = "\n".join(seg.text for seg in segments)
        logger.info(
            "转写完成: audio=%s language_prob=%.2f segments_chars=%d",
            audio_path.name,
            getattr(info, "language_probability", 0.0),
            len(text),
        )

        # 2026-09-02 Level 3 步骤 1:本地后处理(碎片合并 + 重复段去重)
        if self.config.whisper.postprocess_enabled:
            text, stats = clean_transcript(text)
            logger.info(
                "🧹 后处理生效: %d→%d 字符 (压缩 %.0f%%), %d→%d 行",
                stats.original_chars, stats.final_chars,
                stats.char_reduction_ratio * 100,
                stats.original_lines, stats.final_lines,
            )
        return text

    # ---------------- ffmpeg helper ----------------

    @staticmethod
    def _extract_audio(video_path: Path, audio_path: Path) -> None:
        """ffmpeg 抽音轨 → 16kHz 单声道 PCM s16le wav。

        检查:
        - returncode != 0 → RuntimeError(带 stderr 摘要)
        - 输出文件不存在 → RuntimeError(防御性,ffmpeg 偶尔 rc=0 但没产物)
        """
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            str(audio_path),
        ]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            stderr_tail = (result.stderr or "")[-500:]
            raise RuntimeError(
                f"ffmpeg 抽音轨失败: {video_path}\n"
                f"  stderr (tail): {stderr_tail}"
            )
        if not audio_path.exists():
            raise RuntimeError(
                f"ffmpeg 未生成音频文件: {audio_path}(rc=0 但文件不存在)"
            )

    # ---------------- cleanup helper ----------------

    @staticmethod
    def cleanup(*paths: Path) -> None:
        """清理音频文件(try/except 容错 — 幂等)。

        调用场景: 质量检查通过后,删除 .wav 中间产物。
        FileNotFoundError 静默;OSError log warning 继续。
        """
        for p in paths:
            try:
                if p.exists():
                    p.unlink()
                    logger.info("🗑️ 清理音频: %s", p)
            except OSError as e:
                logger.warning("清理音频失败: %s %s", p, e)