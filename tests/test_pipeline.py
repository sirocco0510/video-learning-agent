"""SubtitlePipeline 测试(SSOT: spec 2026-09-03-fr2-fr3 §3.6 / §4.4)。

FR-2.15c / FR-3.9:Level 1 (本地) → Level 4 (云端 LLM,可选) → QualityChecker。
默认 refine_enabled=false → 只跑 Level 1 + QualityChecker。

Task 1: TestPipelineLevel1Only — 2 tests(refine_enabled=False 路径)
Task 2: TestPipelineLevel4 — 2 tests(refine_enabled=True + 长文本跳过)
Task 3: TestPipelineFailure + TestPipelineFileOutput — 2 tests(refiner 失败兜底 + 3 文件契约)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vla.config import VLAConfig
from vla.models import RefinementResult
from vla.quality.checker import QualityChecker, QualityResult
from vla.quality.pipeline import PipelineResult, PostprocessorFn, SubtitlePipeline
from vla.quality.refiner import SubtitleRefiner
from vla.transcribe.postprocess import PostprocessStats


@pytest.fixture
def config() -> VLAConfig:
    """refine_enabled=False (默认,省钱)。"""
    return VLAConfig.from_yaml("config/vla.yaml")


@pytest.fixture
def mock_postprocessor() -> MagicMock:
    """PostprocessorFn mock — MagicMock(spec=PostprocessorFn)。

    Bug 1 fix:Brief 以为有 PostProcessor 类,实际是模块级 clean_transcript 函数。
    """
    fn = MagicMock(spec=PostprocessorFn)
    fn.return_value = (
        "cleaned text",
        PostprocessStats(
            original_chars=10, original_lines=1,
            final_chars=10, final_lines=1,
            merged_short_lines=0, deduped_repeated_segments=0,
        ),
    )
    return fn


@pytest.fixture
def mock_refiner_disabled() -> MagicMock:
    """refine_enabled=False refiner。"""
    r = MagicMock(spec=SubtitleRefiner)
    r.enabled = False
    return r


@pytest.fixture
def mock_refiner_enabled() -> MagicMock:
    r = MagicMock(spec=SubtitleRefiner)
    r.enabled = True
    r.refine = MagicMock(
        return_value=RefinementResult(
            original_text="cleaned text",
            cleaned_text="refined text",
            corrections=[],
            notes=[],
            model="claude-fable-5",
            prompt_tokens=100,
            completion_tokens=50,
        )
    )
    return r


@pytest.fixture
def mock_checker() -> MagicMock:
    c = MagicMock(spec=QualityChecker)
    c.check = MagicMock(
        return_value=QualityResult(
            passed=True, score=85, issues=[], suggestion="", char_count=10,
        )
    )
    return c


class TestPipelineLevel1Only:
    def test_default_run_calls_level1_only(
        self,
        config: VLAConfig,
        mock_postprocessor: MagicMock,
        mock_refiner_disabled: MagicMock,
        mock_checker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """refine_enabled=False → 只调 postprocessor + checker,refiner.refine 不调。"""
        pipeline = SubtitlePipeline(
            config, mock_postprocessor, mock_refiner_disabled, mock_checker
        )
        result = asyncio.run(
            pipeline.run(
                text="raw transcript",
                title="Test Video",
                duration_sec=300,
                model_size="small",
                output_dir=tmp_path,
                stem="Bv1_test",
            )
        )
        assert isinstance(result, PipelineResult)
        assert result.cleaned_text == "cleaned text"
        assert result.refined_text == "cleaned text"  # no refine → same as cleaned
        assert result.refine_result is None
        assert result.quality.passed is True  # 不是 pass_(fix bug 4)
        assert result.quality.score == 85
        mock_postprocessor.assert_called_once_with("raw transcript")
        mock_refiner_disabled.refine.assert_not_called()
        mock_checker.check.assert_called_once_with(
            "cleaned text", "Test Video", 300, "small",
        )

    def test_pipeline_writes_cleaned_file(
        self,
        config: VLAConfig,
        mock_postprocessor: MagicMock,
        mock_refiner_disabled: MagicMock,
        mock_checker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Level 1 → 写 <stem>.cleaned.txt。"""
        pipeline = SubtitlePipeline(
            config, mock_postprocessor, mock_refiner_disabled, mock_checker
        )
        asyncio.run(
            pipeline.run(
                "raw", "title", 100, "small", tmp_path, "Bv1_clean",
            )
        )
        cleaned_path = tmp_path / "Bv1_clean.cleaned.txt"
        assert cleaned_path.exists()
        assert cleaned_path.read_text(encoding="utf-8") == "cleaned text"
        assert not (tmp_path / "Bv1_clean.refined.txt").exists()
