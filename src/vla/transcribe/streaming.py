"""StreamingTranscriber(SSOT: requirements.md FR-3 + implementation-plan.md Phase 4)。

职责:
- 用 faster-whisper 把 wav 文件转写成字幕文本(2026-09-09 T3 起:ffmpeg 抽音迁到 extract.extract_audio)
- 音频文件保留(由 cleanup() / 调用方在质量检查通过后清理;FR-3.5 失败路径也会保留)

设计:
- AudioTranscriber Protocol(本模块定义,F2-4 设计)
- WhisperModel 懒加载(首次 transcribe 时才加载;FR-3.x:启动快)
- faster-whisper VAD 过滤静音段(提速)
- 允许注入 model(测试用)
- 契约:只接 .wav,其余后缀抛 ValueError(spec §4.6)
- 落盘三份(FR-3.8/3.9):
  * <stem>.transcript.txt — Whisper 原始(总写)
  * <stem>.cleaned.txt   — Level 1 本地清理后
  * <stem>.refined.txt   — Level 4 云端 LLM 后(可选,refine_enabled=true 时)
  返回 refined_text(refined 优先,fallback cleaned,再 fallback transcript)
"""

from __future__ import annotations

import logging
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
    isinstance 检查 duck typing — Python 只验证方法名存在,不严格匹配参数名。
    """

    def transcribe(self, audio_path: Path) -> str: ...
    def cleanup(self, audio_path: Path) -> None: ...


class StreamingTranscriber:
    """faster-whisper 流式转写器(FR-3.1/3.2/3.4/3.5/3.8/3.9)。

    用法:
        transcriber = StreamingTranscriber(config, refiner=refiner)
        wav = extract_audio(video_or_audio_path, output_wav_path)  # T2 模块
        text = transcriber.transcribe(wav)  # 返回 refined 或 cleaned 文本
        # ... 质量检查 ...
        StreamingTranscriber.cleanup(wav)   # 通过后再删
    """

    def __init__(
        self,
        config: VLAConfig,
        model: WhisperModel | None = None,
        refiner: "SubtitleRefinerLike | None" = None,
    ) -> None:
        self.config = config
        self._model = model  # None = 懒加载
        self._model_size = config.whisper.model
        self._compute_type = config.whisper.compute_type
        # FR-3.9:可选 SubtitleRefiner(默认 None → skip Level 4)
        self.refiner = refiner

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

    def transcribe(self, audio_path: Path) -> str:
        """把 wav 文件转写成字幕文本(2026-09-09 T3 起:纯 Whisper,无 ffmpeg/无 unlink)。

        流程(FR-3.1/3.2/3.8/3.9):
        1. 契约校验:audio_path 后缀必须 .wav,否则 ValueError(spec §4.6)
        2. faster-whisper 转写(beam_size=5, vad_filter=True)
        3. 写 <stem>.transcript.txt(Whisper 原始,FR-3.8)→ log_dir/transcripts/
        4. 本地后处理(若 enabled)→ 写 <stem>.cleaned.txt(FR-3.8)
        5. 云端 LLM 整理(若 refine_enabled 且 refiner 注入)→ 写 <stem>.refined.txt(FR-3.9)
        6. 当作 fallback 链返回:refined > cleaned > transcript

        音频文件保留(由 cleanup() 在质量检查通过后删除;失败路径 FR-3.5
        也保留供排查)。

        Args:
            audio_path: 必须是 .wav 路径(由 extract.extract_audio 提前生成)。
                非 wav 后缀 → ValueError。调用方负责 ffmpeg 抽音(2026-09-09 T2)
                和视频源删除(FR-3.3 已迁到 fetch_asset / T7)。

        Raises:
            ValueError: audio_path.suffix 不是 .wav(spec §4.6 契约)
            Exception: faster-whisper 转写异常(向上传播,FR-3.5 记录失败)
        """
        # 契约校验:只接 wav(spec §4.6)
        if audio_path.suffix.lower() != ".wav":
            raise ValueError(f"transcribe 期望 wav,得到 {audio_path}")

        # 转写(FR-3.1 + 3.2)
        segments, info = self.model.transcribe(
            str(audio_path),
            language=self.config.whisper.language,
            beam_size=5,
            vad_filter=True,
        )
        raw_text = "\n".join(seg.text for seg in segments)
        logger.info(
            "转写完成: audio=%s language_prob=%.2f segments_chars=%d",
            audio_path.name,
            getattr(info, "language_probability", 0.0),
            len(raw_text),
        )

        # FR-3.8: 写 transcript.txt(原始,总写)→ log_dir/transcripts/
        transcripts_dir = self.config.logging.log_dir / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        stem = audio_path.stem
        transcript_path = transcripts_dir / f"{stem}.transcript.txt"
        transcript_path.write_text(raw_text, encoding="utf-8")
        logger.info("📄 写 transcript.txt: %s", transcript_path)

        # FR-3.8: Level 1 本地清理 → cleaned.txt
        cleaned_text = raw_text
        if self.config.whisper.postprocess_enabled:
            cleaned_text, stats = clean_transcript(raw_text)
            logger.info(
                "🧹 后处理生效: %d→%d 字符 (压缩 %.0f%%), %d→%d 行",
                stats.original_chars, stats.final_chars,
                stats.char_reduction_ratio * 100,
                stats.original_lines, stats.final_lines,
            )
            cleaned_path = transcripts_dir / f"{stem}.cleaned.txt"
            cleaned_path.write_text(cleaned_text, encoding="utf-8")
            logger.info("📄 写 cleaned.txt: %s", cleaned_path)

        # FR-3.9: Level 4 云端 LLM 可选 → refined.txt + 失败 fallback
        refined_text = self._maybe_refine(cleaned_text, transcripts_dir, stem)
        if refined_text is not None:
            return refined_text
        # fallback 链:refined 缺失 → cleaned → transcript(理论上 cleaned 已含)
        return cleaned_text or raw_text

    def _maybe_refine(
        self,
        cleaned_text: str,
        transcripts_dir: Path,
        stem: str,
    ) -> str | None:
        """FR-3.9:可选 Level 4 云端 LLM 字幕语义整理。

        Returns:
            refined_text (成功 → 含 cleaned_text 或修正后),或 None(未启用 / 失败)
        """
        if not self.config.quality_check.refine_enabled:
            return None
        if self.refiner is None:
            logger.warning(
                "⚠️ refine_enabled=true 但未注入 SubtitleRefiner,跳过 Level 4 云端清理"
            )
            return None

        try:
            result = self.refiner.refine(cleaned_text, title=stem)
        except Exception as e:
            logger.warning("⚠️ SubtitleRefiner.refine 抛异常,fallback cleaned_text: %s", e)
            return None

        refined_path = transcripts_dir / f"{stem}.refined.txt"
        # 失败 fallback 标记(result.notes 含失败原因时)
        try:
            refined_path.write_text(
                result.cleaned_text
                + (f"\n\n# notes: {result.notes}" if result.notes else ""),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("⚠️ 写 refined.txt 失败:%s", e)
            return None

        # 若 LLM 返回了原 cleaned_text(无变化),仍算成功(走 refined 路径)
        logger.info(
            "📄 写 refined.txt: %s (%d 字符, model=%s)",
            refined_path, len(result.cleaned_text), result.model,
        )
        return result.cleaned_text

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


# ---------------- Protocol(类型注解 + duck typing) ----------------


class SubtitleRefinerLike(Protocol):
    """SubtitleRefiner 子集(避免 streaming.py 反向依赖具体类)。

    streaming.py 只用 .refine(text, title=) → RefinementResult.cleaned_text,
    所以 Protocol 描述这一段接口就够。
    """

    enabled: bool

    def refine(self, text: str, title: str = "") -> "RefinementResultLike": ...


class RefinementResultLike(Protocol):
    """RefinementResult 子集(同上)。"""

    @property
    def cleaned_text(self) -> str: ...

    @property
    def notes(self) -> str: ...

    @property
    def model(self) -> str: ...