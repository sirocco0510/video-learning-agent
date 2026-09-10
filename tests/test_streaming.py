"""StreamingTranscriber 测试(SSOT: requirements.md FR-3 + implementation-plan.md Phase 4)。

设计要点:
- AudioTranscriber Protocol duck typing(在 transcribe/streaming.py 定义)
- transcribe(audio_path) 只做 Whisper;ffmpeg 抽音已迁到 extract.extract_audio(2026-09-09 T2)
- FR-3.3 删视频源已迁到 fetch_asset(2026-09-09 T7);transcribe 不再 unlink
- WhisperModel 懒加载,允许测试注入
- cleanup() 静态方法供调用方在**转写成功后**删音频(2026-09-10 FR-3.7:
  不再等质量门控)

测试策略:注入 mock WhisperModel;不再 patch subprocess(ffmpeg 已不在此模块)。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vla.config import VLAConfig
from vla.transcribe.streaming import AudioTranscriber, StreamingTranscriber


# ---------------- Mocks ----------------


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def make_fake_segments(*texts: str):
    """构造 mock faster-whisper segments(每个有 .text 属性)。"""
    segs = []
    for t in texts:
        s = MagicMock()
        s.text = t
        segs.append(s)
    return segs


# ---------------- Fixtures ----------------


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
        "whisper": {
            "model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8",
            # 测试默认关 postprocess — 各 test 自己控制;专门测 postprocess 用专属 cfg
            "postprocess_enabled": False,
        },
        "video_source": {
            "prefer_download": True,
            "download": {"format": "worst"},
            "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"},
        },
        "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./notes/v.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


@pytest.fixture
def mock_model() -> MagicMock:
    """mock faster_whisper.WhisperModel(注入到 StreamingTranscriber)。"""
    m = MagicMock()
    # model.transcribe(...) 返回 (segments_iter, info)
    info = MagicMock()
    info.language_probability = 0.99
    m.transcribe.return_value = (make_fake_segments("你好", "世界"), info)
    return m


@pytest.fixture
def transcriber(cfg: VLAConfig, mock_model: MagicMock) -> StreamingTranscriber:
    return StreamingTranscriber(cfg, model=mock_model)


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """假 wav 文件:transcribe() 只吃 wav(2026-09-09 T3 起)。"""
    p = tmp_path / "test.wav"
    p.write_bytes(b"fake wav bytes")
    return p


# ---------------- Protocol 兼容 ----------------


class TestProtocol:
    def test_satisfies_audio_transcriber_protocol(self, transcriber):
        """StreamingTranscriber 应该满足 AudioTranscriber Protocol(duck typing)。"""
        assert isinstance(transcriber, AudioTranscriber)


# ---------------- 契约测试(2026-09-09 T3) ----------------


class TestTranscribeContract:
    """T3 新契约:transcribe() 只接 wav,且不调 ffmpeg/不删源。"""

    def test_transcribe_rejects_mp4(self, transcriber, tmp_path):
        """传 mp4 → ValueError(期望 wav,spec §4.6 契约)。"""
        mp4 = tmp_path / "video.mp4"
        mp4.write_bytes(b"fake video bytes")
        with pytest.raises(ValueError, match="wav"):
            transcriber.transcribe(mp4)

    def test_transcribe_rejects_webm(self, transcriber, tmp_path):
        """传 webm → ValueError(典型 F2-10 scan_today_dir 输入,但 transcribe 不认)。"""
        webm = tmp_path / "video.webm"
        webm.write_bytes(b"fake video bytes")
        with pytest.raises(ValueError, match="wav"):
            transcriber.transcribe(webm)

    def test_transcribe_does_not_call_subprocess(self, transcriber, audio_file):
        """transcribe 只做 Whisper,ffmpeg 抽音已迁到 extract.extract_audio(§4.6)。"""
        with patch("subprocess.run") as mock_run:
            transcriber.transcribe(audio_file)
            assert not mock_run.called, "transcribe 不应调 subprocess.run(ffmpeg 已迁出)"

    def test_transcribe_does_not_unlink_source(self, transcriber, audio_file):
        """FR-3.3 删视频源逻辑已迁到 fetch_asset(T7);transcribe 不再 unlink。"""
        assert audio_file.exists()
        transcriber.transcribe(audio_file)
        assert audio_file.exists(), "transcribe 不应删 wav"


# ---------------- faster-whisper 转写 ----------------


class TestWhisperTranscribe:
    def test_uses_config_language(self, transcriber, audio_file, mock_model):
        """language 来自 config.whisper.language。"""
        transcriber.transcribe(audio_file)
        kwargs = mock_model.transcribe.call_args.kwargs
        assert kwargs["language"] == "zh"

    def test_vad_filter_enabled(self, transcriber, audio_file, mock_model):
        """FR-3.x: vad_filter=True 必须开(过滤静音段)。"""
        transcriber.transcribe(audio_file)
        kwargs = mock_model.transcribe.call_args.kwargs
        assert kwargs["vad_filter"] is True

    def test_segments_joined_with_newline(self, transcriber, audio_file, mock_model):
        """segments 文本用 \\n 拼接返回。"""
        mock_model.transcribe.return_value = (
            make_fake_segments("你好", "这是", "一段测试"),
            MagicMock(language_probability=0.95),
        )
        text = transcriber.transcribe(audio_file)
        assert text == "你好\n这是\n一段测试"

    def test_transcribe_failure_propagates(self, cfg, audio_file, mock_model):
        """model.transcribe 抛错 → 异常向上传播(供 FR-3.5 记录)。"""
        mock_model.transcribe.side_effect = RuntimeError("whisper OOM")
        transcriber = StreamingTranscriber(cfg, model=mock_model)
        with pytest.raises(RuntimeError, match="whisper OOM"):
            transcriber.transcribe(audio_file)


class TestPostprocessWiring:
    """2026-09-02:StreamingTranscriber 串接 postprocess 的端到端测试。"""

    def test_postprocess_disabled_returns_raw_segments(self, cfg, audio_file, mock_model):
        """postprocess_enabled=False → 返回原始 \\n 拼接(不合并碎片)。"""
        cfg.whisper.postprocess_enabled = False
        mock_model.transcribe.return_value = (
            make_fake_segments("你好", "这是", "一段测试"),
            MagicMock(language_probability=0.95),
        )
        transcriber = StreamingTranscriber(cfg, model=mock_model)
        text = transcriber.transcribe(audio_file)
        assert text == "你好\n这是\n一段测试"

    def test_postprocess_enabled_merges_short_lines(self, cfg, audio_file, mock_model):
        """postprocess_enabled=True → 短碎片行被合并。"""
        cfg.whisper.postprocess_enabled = True
        mock_model.transcribe.return_value = (
            make_fake_segments("第一句長度已經八個字符", "這是另一句也是夠長", "了"),
            MagicMock(language_probability=0.95),
        )
        transcriber = StreamingTranscriber(cfg, model=mock_model)
        text = transcriber.transcribe(audio_file)
        # "了"(1 字符) → 并入上一行
        assert text == "第一句長度已經八個字符\n這是另一句也是夠長了"


# ---------------- 懒加载 WhisperModel ----------------


class TestLazyModelLoad:
    def test_model_not_loaded_on_construction(self, cfg):
        """构造函数不加载 WhisperModel(避免启动慢)。"""
        with patch("vla.transcribe.streaming.WhisperModel") as mock_cls:
            StreamingTranscriber(cfg, model=None)
            mock_cls.assert_not_called()

    def test_model_loaded_on_first_transcribe(self, cfg, audio_file):
        """首次调用 transcribe 才加载模型。"""
        with patch("vla.transcribe.streaming.WhisperModel") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.transcribe.return_value = (
                make_fake_segments("x"),
                MagicMock(language_probability=1.0),
            )
            mock_cls.return_value = mock_instance

            t = StreamingTranscriber(cfg, model=None)

            # 此时还没加载
            mock_cls.assert_not_called()

            t.transcribe(audio_file)

            # 现在加载了
            mock_cls.assert_called_once()
            # 用 config 里的 model size + compute_type
            args, kwargs = mock_cls.call_args
            assert args[0] == "small"  # cfg.whisper.model
            assert kwargs["compute_type"] == "int8"

    def test_injected_model_used_directly(self, cfg, audio_file, mock_model):
        """构造函数注入 model → 不重新加载。"""
        with patch("vla.transcribe.streaming.WhisperModel") as mock_cls:
            t = StreamingTranscriber(cfg, model=mock_model)
            t.transcribe(audio_file)

            # 没有调用 WhisperModel 构造
            mock_cls.assert_not_called()
            # 但注入的 model 被调用了
            mock_model.transcribe.assert_called_once()


class TestLocalOnlyModelLoad:
    """2026-09-10:加载必须先 `local_files_only=True`。

    实测 `huggingface_hub` 会向 huggingface.co 做一次 metadata 校验请求,
    网络不可达时在超时重试里干等 **150s**(`small` 冷加载 150.6s → 本地
    0.5s,300×),而实际转写只要 24s。这与红线「字幕永远本地」同向。
    """

    @staticmethod
    def _fake_model() -> MagicMock:
        inst = MagicMock()
        inst.transcribe.return_value = (
            make_fake_segments("x"),
            MagicMock(language_probability=1.0),
        )
        return inst

    def test_local_files_only_true_on_first_attempt(self, cfg, audio_file):
        """首次加载带 local_files_only=True(不碰网络)。"""
        with patch("vla.transcribe.streaming.WhisperModel") as mock_cls:
            mock_cls.return_value = self._fake_model()

            StreamingTranscriber(cfg, model=None).transcribe(audio_file)

            mock_cls.assert_called_once()
            assert mock_cls.call_args.kwargs["local_files_only"] is True

    def test_falls_back_to_download_when_local_missing(self, cfg, audio_file):
        """本地没有该模型 → 回落到允许下载(仅此一次)。"""
        with patch("vla.transcribe.streaming.WhisperModel") as mock_cls:
            mock_cls.side_effect = [
                RuntimeError("model not found in local cache"),
                self._fake_model(),
            ]

            StreamingTranscriber(cfg, model=None).transcribe(audio_file)

            assert mock_cls.call_count == 2
            # 第一次:只认本地
            assert mock_cls.call_args_list[0].kwargs["local_files_only"] is True
            # 第二次:允许下载
            assert mock_cls.call_args_list[1].kwargs.get("local_files_only") is not True
            # 两次都用同一份 model/compute_type
            for call in mock_cls.call_args_list:
                assert call.args[0] == "small"
                assert call.kwargs["compute_type"] == "int8"

    def test_download_failure_propagates(self, cfg, audio_file):
        """本地没有 + 下载也失败 → 异常上抛(不静默吞)。"""
        with patch("vla.transcribe.streaming.WhisperModel") as mock_cls:
            mock_cls.side_effect = RuntimeError("offline")

            with pytest.raises(RuntimeError, match="offline"):
                StreamingTranscriber(cfg, model=None).transcribe(audio_file)


# ---------------- cleanup 静态方法 ----------------


class TestCleanup:
    def test_unlinks_existing_paths(self, tmp_path):
        f1 = tmp_path / "a.wav"
        f2 = tmp_path / "b.wav"
        f1.write_bytes(b"x")
        f2.write_bytes(b"y")

        StreamingTranscriber.cleanup(f1, f2)

        assert not f1.exists()
        assert not f2.exists()

    def test_ignores_missing_paths(self, tmp_path):
        """FileNotFoundError 不抛(幂等)。"""
        # 没创建文件
        StreamingTranscriber.cleanup(tmp_path / "missing.wav")  # 不抛

    def test_ignores_oserror(self, tmp_path):
        """OSError(权限等)被 log warning,继续。"""
        with patch("vla.transcribe.streaming.logger") as mock_logger:
            bad_path = MagicMock()
            bad_path.exists.return_value = True
            bad_path.unlink.side_effect = OSError("permission denied")

            StreamingTranscriber.cleanup(bad_path)

            mock_logger.warning.assert_called()
            assert "permission denied" in str(mock_logger.warning.call_args)

    def test_accepts_zero_paths(self):
        """cleanup() 0 个参数不抛。"""
        StreamingTranscriber.cleanup()


# ---------------- 音频文件保留到 cleanup(本模块不删,由调用方删) ----------------


class TestAudioFileLifecycle:
    def test_audio_kept_after_successful_transcribe(self, transcriber, audio_file):
        """transcribe() 成功 → 本模块**不删**音频(FR-3.7 2026-09-10:删的时机
        是"转写成功",但执行者是调用方 main_provider,不是这里)。"""
        transcriber.transcribe(audio_file)
        assert audio_file.exists()


# ---------------- FR-3.8 / FR-2.15c:落盘 transcript.txt(+ 可选 refined.txt) ----------------


class TestTranscriptAndCleanedWrite:
    """FR-3.8:转写后写 logs/transcripts/<stem>.transcript.txt。

    .cleaned.txt 自 2026-09-10 起不再落盘(见下面 postprocess 那条测试)。
    """

    def test_transcribe_writes_transcript_txt(
        self, tmp_path, mock_model
    ) -> None:
        """原始 whisper 输出 → <stem>.transcript.txt(总写,不管 postprocess)。"""
        cfg = VLAConfig.model_validate({
            "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
            "whisper": {
                "model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8",
                "postprocess_enabled": False,
            },
            "video_source": {
                "prefer_download": True, "download": {"format": "worst"},
                "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"},
            },
            "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0, "refine_enabled": False},
            "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./n.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
            "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
            "history": {"file": str(tmp_path / "h.jsonl")},
            "logging": {"log_dir": str(tmp_path / "logs"), "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
            "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL", "refine_model": "x"},
        })
        cfg.logging.log_dir = tmp_path / "logs"
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"x")
        transcriber = StreamingTranscriber(cfg, model=mock_model)

        transcriber.transcribe(audio_file)

        transcript_path = tmp_path / "logs" / "transcripts" / f"{audio_file.stem}.transcript.txt"
        assert transcript_path.exists()
        assert "你好" in transcript_path.read_text(encoding="utf-8")
        assert "世界" in transcript_path.read_text(encoding="utf-8")

    def test_transcribe_with_postprocess_does_not_write_cleaned_txt(
        self, tmp_path, mock_model
    ) -> None:
        """postprocess_enabled=True → **不再**写 .cleaned.txt(2026-09-10)。

        cleaned.txt 是只写不读的中间产物(全仓无读取方);Level 1 结果
        只留在内存里,作质量门控输入 + refiner 输入。
        """
        cfg = VLAConfig.model_validate({
            "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
            "whisper": {
                "model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8",
                "postprocess_enabled": True,
                "postprocess_min_line_chars": 8,
                "postprocess_min_overlap_chars": 6,
            },
            "video_source": {
                "prefer_download": True, "download": {"format": "worst"},
                "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"},
            },
            "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0, "refine_enabled": False},
            "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./n.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
            "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
            "history": {"file": str(tmp_path / "h.jsonl")},
            "logging": {"log_dir": str(tmp_path / "logs"), "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
            "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL", "refine_model": "x"},
        })
        cfg.logging.log_dir = tmp_path / "logs"
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"x")
        transcriber = StreamingTranscriber(cfg, model=mock_model)

        returned = transcriber.transcribe(audio_file)

        transcripts_dir = tmp_path / "logs" / "transcripts"
        assert (transcripts_dir / "test.transcript.txt").exists()
        assert not (transcripts_dir / "test.cleaned.txt").exists()
        # 清理确实跑了,只是结果留在内存里:mock 两行「你好」「世界」都短于
        # postprocess_min_line_chars=8 → 被合并成一行;而落盘的原始
        # transcript.txt 仍保留换行。返回的是清理后的文本 = 下游拿得到。
        assert "\n" not in returned, "返回值应是 Level 1 清理后的(已合并碎片)"
        assert "你好" in returned and "世界" in returned
        assert "\n" in (transcripts_dir / "test.transcript.txt").read_text(encoding="utf-8")


# ---------------- FR-3.9 / FR-2.15c Level 4:SubtitleRefiner 串接 ----------------


class TestRefinerIntegration:
    """FR-3.9:可选云端 LLM 字幕语义清理(refine_enabled=true 时启用)。"""

    def _make_cfg_with_refiner(
        self, tmp_path: Path, refine_enabled: bool
    ) -> VLAConfig:
        return VLAConfig.model_validate({
            "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
            "whisper": {
                "model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8",
                "postprocess_enabled": True,
            },
            "video_source": {
                "prefer_download": True, "download": {"format": "worst"},
                "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"},
            },
            "quality_check": {
                "enabled": True, "model": "x", "min_score_to_pass": 70,
                "min_char_per_second": 1.0, "max_char_per_second": 15.0,
                "refine_enabled": refine_enabled,
                "refine_max_chars": 6000,
            },
            "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./n.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
            "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
            "history": {"file": str(tmp_path / "h.jsonl")},
            "logging": {"log_dir": str(tmp_path / "logs"), "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
            "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL", "refine_model": "refine-x"},
        })

    def _fake_segments(self, *texts: str):
        segs = []
        for t in texts:
            s = MagicMock()
            s.text = t
            segs.append(s)
        return segs

    def test_refine_disabled_no_refined_txt(self, tmp_path, mock_model) -> None:
        """refine_enabled=False → 不写 .refined.txt,返回 cleaned_text。"""
        cfg = self._make_cfg_with_refiner(tmp_path, refine_enabled=False)
        cfg.logging.log_dir = tmp_path / "logs"
        audio_file = tmp_path / "BV1disabled.wav"
        audio_file.write_bytes(b"x")
        # mock_model 默认 "你好" + "世界",clean_transcript 后是 "你好 世界" 这种

        transcriber = StreamingTranscriber(cfg, model=mock_model)
        result = transcriber.transcribe(audio_file)

        transcripts_dir = tmp_path / "logs" / "transcripts"
        assert not (transcripts_dir / "BV1disabled.refined.txt").exists()
        # 返回的是 cleaned_text(非 refined)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_refine_enabled_writes_refined_txt_and_returns_refined(
        self, tmp_path, mock_model
    ) -> None:
        """refine_enabled=True + 注入 refiner → 写 .refined.txt + 返回 refined_text。"""
        from vla.quality.refiner import RefinementResult, SubtitleRefiner

        cfg = self._make_cfg_with_refiner(tmp_path, refine_enabled=True)
        cfg.logging.log_dir = tmp_path / "logs"
        audio_file = tmp_path / "BV1refine.wav"
        audio_file.write_bytes(b"x")

        # mock SubtitleRefiner.refine → 返回固定 cleaned_text
        refined_text = "你好世界。(refined)"
        fake_result = RefinementResult(
            original_text="raw cleaned",
            cleaned_text=refined_text,
            corrections=[],
            notes="ok",
            model="refine-x",
        )
        mock_refiner = MagicMock(spec=SubtitleRefiner)
        mock_refiner.enabled = True
        mock_refiner.refine.return_value = fake_result

        transcriber = StreamingTranscriber(cfg, model=mock_model, refiner=mock_refiner)
        result = transcriber.transcribe(audio_file)

        transcripts_dir = tmp_path / "logs" / "transcripts"
        # refined.txt 写了
        refined_path = transcripts_dir / "BV1refine.refined.txt"
        assert refined_path.exists()
        assert refined_text in refined_path.read_text(encoding="utf-8")
        # cleaned.txt 不再落盘(2026-09-10)
        assert not (transcripts_dir / "BV1refine.cleaned.txt").exists()
        # transcript.txt 也保留
        assert (transcripts_dir / "BV1refine.transcript.txt").exists()
        # 返回 refined_text
        assert result == refined_text
        # refiner.refine 被调
        mock_refiner.refine.assert_called_once()

    def test_refine_failure_falls_back_to_cleaned(self, tmp_path, mock_model) -> None:
        """refiner 抛异常 → 退化用 cleaned_text,不抛。"""
        from vla.quality.refiner import RefinementResult, SubtitleRefiner

        cfg = self._make_cfg_with_refiner(tmp_path, refine_enabled=True)
        cfg.logging.log_dir = tmp_path / "logs"
        audio_file = tmp_path / "BV1fail.wav"
        audio_file.write_bytes(b"x")

        # refiner 返回 cleaned_text == original → 视为 fallback
        fallback_result = RefinementResult(
            original_text="cleaned原文本",
            cleaned_text="cleaned原文本",
            corrections=[],
            notes="LLM 调用失败:RuntimeError",
            model="refine-x",
        )
        mock_refiner = MagicMock(spec=SubtitleRefiner)
        mock_refiner.enabled = True
        mock_refiner.refine.return_value = fallback_result

        transcriber = StreamingTranscriber(cfg, model=mock_model, refiner=mock_refiner)

        # 不抛
        result = transcriber.transcribe(audio_file)

        transcripts_dir = tmp_path / "logs" / "transcripts"
        # refined.txt 仍写(fallback 也落盘供审计)
        assert (transcripts_dir / "BV1fail.refined.txt").exists()
        # 返回的是 cleaned_text(fallback 后)
        assert "cleaned原文本" in result
        # refined.txt 内容含 notes 标记失败
        refined_content = (transcripts_dir / "BV1fail.refined.txt").read_text(encoding="utf-8")
        assert "LLM 调用失败" in refined_content

    def test_refine_enabled_but_no_refiner_injected_skips_gracefully(
        self, tmp_path, mock_model
    ) -> None:
        """refine_enabled=True 但 refiner=None → 跳过(不抛),退化用 cleaned。"""
        cfg = self._make_cfg_with_refiner(tmp_path, refine_enabled=True)
        cfg.logging.log_dir = tmp_path / "logs"
        audio_file = tmp_path / "BV1norfnr.wav"
        audio_file.write_bytes(b"x")

        transcriber = StreamingTranscriber(cfg, model=mock_model, refiner=None)

        # 不抛
        result = transcriber.transcribe(audio_file)

        transcripts_dir = tmp_path / "logs" / "transcripts"
        # refined.txt 不写(没注入 refiner,跳过整段)
        assert not (transcripts_dir / "BV1norfnr.refined.txt").exists()
        # 返回 cleaned 文本
        assert isinstance(result, str)
        assert len(result) > 0
