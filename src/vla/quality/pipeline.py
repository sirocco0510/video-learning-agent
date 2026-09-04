"""SubtitlePipeline:Level 1 → Level 4 → QualityChecker 协调器(SSOT: spec §3.6)。

FR-2.15c / FR-3.9 字幕清理管线。
- Level 1 (本地, always on):clean_transcript(text) → (cleaned_text, post_stats)
- Level 4 (云端 LLM, optional):SubtitleRefiner.refine(refine_enabled=true 才走)
- QualityChecker:always runs on refined_text (or cleaned_text if no refine)

Rulings:
- clean_transcript 是模块级函数(不是 PostProcessor 类的方法)— 用 PostprocessorFn callable 类型
- PipelineResult 用 pydantic BaseModel,不用 @dataclass(pydantic v2 不支持两者叠加)
- QualityResult.passed 不是 pass_(无下划线后缀)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from vla.config import VLAConfig
from vla.quality.checker import QualityChecker, QualityResult
from vla.quality.refiner import (
    RefinementResult,
    SubtitleRefiner,
    write_cleaned_transcript,
)
from vla.transcribe.postprocess import PostprocessStats


class PostprocessorFn(Protocol):
    """clean_transcript 函数签名。Brief 误以为有 PostProcessor 类,实际是模块级函数。"""

    def __call__(self, text: str) -> tuple[str, PostprocessStats]: ...


class PipelineResult(BaseModel):
    """FR-2.15c 管线结果。"""

    cleaned_text: str
    refined_text: str
    quality: QualityResult
    post_stats: PostprocessStats
    refine_result: RefinementResult | None = None


class SubtitlePipeline:
    def __init__(
        self,
        config: VLAConfig,
        postprocessor: PostprocessorFn,
        refiner: SubtitleRefiner | None,
        checker: QualityChecker,
    ) -> None:
        self._config = config
        self._postprocessor = postprocessor
        self._refiner = refiner
        self._checker = checker
        self.log = logging.getLogger(__name__)

    async def run(
        self,
        text: str,
        title: str,
        duration_sec: int,
        model_size: str,
        output_dir: Path,
        stem: str,
    ) -> PipelineResult:
        """FR-2.15c 主入口。

        顺序:
          1. Level 1:postprocessor(text) → 写 <stem>.cleaned.txt
          2. Level 4 (可选):refiner.refine(只当 enabled 且 text ≤ refine_max_chars)
             → 写 <stem>.refined.txt(只在 cleaned_text != refined_text 时)
          3. QualityChecker:对 refined_text(或 cleaned_text)打分
        Refiner 失败兜底:用 cleaned_text 继续,refine_result=None。
        """
        # ---- Level 1: always on ----
        cleaned_text, post_stats = self._postprocessor(text)
        cleaned_path = output_dir / f"{stem}.cleaned.txt"
        cleaned_path.write_text(cleaned_text, encoding="utf-8")

        refined_text = cleaned_text
        refine_result: RefinementResult | None = None

        # ---- Level 4: optional ----
        max_chars = self._config.quality_check.refine_max_chars
        if self._refiner is not None and self._refiner.enabled and len(cleaned_text) <= max_chars:
            try:
                refine_result = self._refiner.refine(cleaned_text, title=title)
                if refine_result.cleaned_text != cleaned_text:
                    refined_text = refine_result.cleaned_text
                    refined_path = output_dir / f"{stem}.refined.txt"
                    write_cleaned_transcript(refined_path, refine_result)
            except Exception as e:
                self.log.warning("Level 4 refine 失败,使用 cleaned_text: %s", e)
                refine_result = None
                refined_text = cleaned_text
        elif self._refiner is not None and self._refiner.enabled:
            self.log.warning(
                "字幕文本 %d 字符 > refine_max_chars=%d,跳过 Level 4",
                len(cleaned_text), max_chars,
            )

        # ---- Quality gate ----
        quality = self._checker.check(refined_text, title, duration_sec, model_size)

        return PipelineResult(
            cleaned_text=cleaned_text,
            refined_text=refined_text,
            quality=quality,
            post_stats=post_stats,
            refine_result=refine_result,
        )
