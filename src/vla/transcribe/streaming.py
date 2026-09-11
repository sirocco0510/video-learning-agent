"""StreamingTranscriber(SSOT: requirements.md FR-3 + implementation-plan.md Phase 4)。

职责:
- 用 faster-whisper 把 wav 文件转写成字幕文本(2026-09-09 T3 起:ffmpeg 抽音迁到 extract.extract_audio)
- 音频文件**转写成功后即可删**(2026-09-10 FR-3.7 修正:不再等质量门控;
  由调用方删,本模块不删。转写失败仍保留供排查)

设计:
- AudioTranscriber Protocol(本模块定义,F2-4 设计)
- WhisperModel 懒加载(首次 transcribe 时才加载;FR-3.x:启动快)
  加载**优先只认本地缓存**(local_files_only=True,2026-09-10:HF 联网校验
  在网络不可达时干等 150s,是实际转写的 6 倍)
- faster-whisper VAD 过滤静音段(提速)
- 允许注入 model(测试用)
- 契约:只接 .wav,其余后缀抛 ValueError(spec §4.6)
- 落盘两份(FR-3.8/3.9;2026-09-10 起 cleaned.txt 不再落盘):
  * <stem>.transcript.txt — Whisper 原始(总写)
  * <stem>.refined.txt   — Level 4 云端 LLM 后(可选,refine_enabled=true 时)
  返回 refined_text(refined 优先,fallback 内存中的 cleaned,再 fallback transcript)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from faster_whisper import WhisperModel

from vla.config import VLAConfig
from vla.log.transcription_log import transcripts_dir_for
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
        StreamingTranscriber.cleanup(wav)   # 转写成功即删(2026-09-10:不再等质量门控)
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
            self._model = self._load_model()
        return self._model

    def _load_model(self) -> WhisperModel:
        """构造 WhisperModel —— **优先只认本地缓存**(2026-09-10)。

        为什么第一次就 `local_files_only=True`:
        `huggingface_hub` 解析模型名时会向 huggingface.co 发一次 metadata
        校验请求;网络不可达(HF 被墙 / 离线)时,这 150s 就耗在那次调用的
        超时重试里。实测同一台机器:
            - 默认(联网校验)      small 冷加载 **150.6s**
            - local_files_only   small 冷加载 **0.5s**(300×)
        而那个视频实际转写只要 24s —— 加载是转写的 6 倍。
        排查过程中已排除:磁盘读取(461MB `cat` 仅 0.035s)、线程数
        (`cpu_threads=4` 仍 150.5s)、模型体积(`tiny` 同样 float16→int8 只要 0.6s)。
        此举与红线「字幕永远本地」同向,顺带去掉对 HF 的网络依赖。

        本地没有(首跑 / 换模型)→ 回落允许下载一次,不让首跑直接失败。
        """
        try:
            return WhisperModel(
                self._model_size,
                compute_type=self._compute_type,
                local_files_only=True,
            )
        except Exception as e:
            logger.info(
                "模型 %s 本地缓存不可用(%s: %s),回落到允许下载",
                self._model_size, type(e).__name__, e,
            )
            return WhisperModel(
                self._model_size,
                compute_type=self._compute_type,
            )

    # ---------------- AudioTranscriber Protocol 实现 ----------------

    def transcribe(self, audio_path: Path) -> str:
        """把 wav 文件转写成字幕文本(2026-09-09 T3 起:纯 Whisper,无 ffmpeg/无 unlink)。

        流程(FR-3.1/3.2/3.8/3.9):
        1. 契约校验:audio_path 后缀必须 .wav,否则 ValueError(spec §4.6)
        2. faster-whisper 转写(beam_size=5, vad_filter=True)
        3. 写 <stem>.transcript.txt(Whisper 原始,FR-3.8)→ log_dir/transcribed/<date>/transcripts/
        4. 本地后处理(若 enabled)→ cleaned_text **只留内存,不落盘**(2026-09-10)
        5. 云端 LLM 整理(若 refine_enabled 且 refiner 注入)→ 写 <stem>.refined.txt(FR-3.9)
        6. 当作 fallback 链返回:refined > cleaned(内存)> transcript

        音频文件**转写成功即可删**(2026-09-10 FR-3.7 修正:不再等质量门控);
        转写失败(本方法抛错)仍保留供排查。

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
        # FR-3.10(2026-09-10):initial_prompt 强制简体。空串 → None(干净关闭,
        # 空串在某些 faster-whisper 版本下仍会作为 prompt 参与解码)。
        segments, info = self.model.transcribe(
            str(audio_path),
            language=self.config.whisper.language,
            beam_size=5,
            vad_filter=True,
            initial_prompt=self.config.whisper.initial_prompt or None,
        )
        raw_text = "\n".join(seg.text for seg in segments)
        logger.info(
            "转写完成: audio=%s language_prob=%.2f segments_chars=%d",
            audio_path.name,
            getattr(info, "language_probability", 0.0),
            len(raw_text),
        )

        # FR-3.8: 写 transcript.txt(原始,总写)→ 与正式产物同一棵日期树
        # (2026-09-10:`logs/transcribed/<date>/transcripts/` —— 旧实现写扁平的
        #  `logs/transcripts/`,导致原始/精修产物与 `<id>_<title>.txt` 分家;
        #  日期分组逻辑的唯一来源见 `vla.log.transcription_log.transcripts_dir_for`)
        transcripts_dir = transcripts_dir_for(self.config.logging.log_dir)
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        stem = audio_path.stem
        transcript_path = transcripts_dir / f"{stem}.transcript.txt"
        transcript_path.write_text(raw_text, encoding="utf-8")
        logger.info("📄 写 transcript.txt: %s", transcript_path)

        # FR-3.8: Level 1 本地清理(只留内存,不落盘 —— 2026-09-10)
        cleaned_text = raw_text
        if self.config.whisper.postprocess_enabled:
            cleaned_text, stats = clean_transcript(raw_text)
            logger.info(
                "🧹 后处理生效: %d→%d 字符 (压缩 %.0f%%), %d→%d 行",
                stats.original_chars, stats.final_chars,
                stats.char_reduction_ratio * 100,
                stats.original_lines, stats.final_lines,
            )
            # 2026-09-10:不再写 <stem>.cleaned.txt。它是只写不读的中间产物
            # (全仓无任何读取方);cleaned_text 留在内存里照常流向 refiner 与
            # 质量门控,功能零损失。每视频落盘文件因此从 5 个减到 4 个。

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

        调用场景: **转写成功后**立即删除 .wav 中间产物(2026-09-10 FR-3.7
        修正:不再等质量门控)。
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