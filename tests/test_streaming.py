"""StreamingTranscriber 测试(SSOT: requirements.md FR-3 + implementation-plan.md Phase 4)。

设计要点:
- AudioTranscriber Protocol duck typing(在 transcribe/streaming.py 定义)
- ffmpeg 抽音轨 → 删视频源 → faster-whisper 转写
- WhisperModel 懒加载,允许测试注入
- cleanup() 静态方法给质量检查通过后调用

测试策略:patch subprocess.run(ffmpeg)+ 注入 mock WhisperModel。
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
        "browser_plugin": {
            "name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30,
            "plugin_paths": [], "record_hotkey": "Alt+Shift+R",
            "record_download_timeout_sec": 5, "record_pre_grace_sec": 0, "record_post_buffer_sec": 0,
        },
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
def video_file(tmp_path: Path) -> Path:
    """假视频文件:实际上不需要真实内容,因为我们 patch 了 ffmpeg。"""
    p = tmp_path / "test.mp4"
    p.write_bytes(b"fake video bytes")
    return p


# ---------------- Protocol 兼容 ----------------


class TestProtocol:
    def test_satisfies_audio_transcriber_protocol(self, transcriber):
        """StreamingTranscriber 应该满足 AudioTranscriber Protocol(duck typing)。"""
        assert isinstance(transcriber, AudioTranscriber)


# ---------------- ffmpeg 抽音轨 ----------------


class TestExtractAudio:
    def test_calls_ffmpeg_with_correct_args(self, transcriber, video_file, tmp_path, mock_model):
        """ffmpeg 必须带 -ar 16000 -ac 1 -c:a pcm_s16le 抽 16kHz 单声道 PCM。"""
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            # 模拟 ffmpeg 生成了 wav 文件
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")

            transcriber.transcribe(video_file)

            args = mock_run.call_args.args[0]
            assert args[0] == "ffmpeg"
            assert "-i" in args
            assert str(video_file) in args
            assert "-ar" in args and "16000" in args
            assert "-ac" in args and "1" in args
            assert "-c:a" in args and "pcm_s16le" in args
            assert "-y" in args  # 强制覆盖

    def test_audio_path_has_wav_suffix(self, transcriber, video_file, mock_model, tmp_path):
        """输出文件应该是 .wav(同名换后缀)。"""
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")

            transcriber.transcribe(video_file)

            # model.transcribe 收到的第一个位置参数是 audio_path
            audio_arg = mock_model.transcribe.call_args.args[0]
            assert audio_arg.endswith(".wav")

    def test_ffmpeg_failure_raises_runtime_error(self, transcriber, video_file, mock_model):
        """ffmpeg 返回非 0 → RuntimeError(带 stderr 摘要)。"""
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(
                returncode=1, stderr="some ffmpeg error here"
            )

            with pytest.raises(RuntimeError, match="ffmpeg"):
                transcriber.transcribe(video_file)

            # 模型不应该被调用
            mock_model.transcribe.assert_not_called()

    def test_ffmpeg_no_output_file_raises(self, transcriber, video_file, mock_model):
        """ffmpeg 退出 0 但没生成文件 → RuntimeError(防御性)。"""
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            # 不创建 wav 文件

            with pytest.raises(RuntimeError, match="音频文件"):
                transcriber.transcribe(video_file)


# ---------------- 边转写边清理:删视频源 ----------------


class TestDeleteVideoSource:
    def test_video_deleted_after_audio_extract(self, transcriber, video_file, mock_model):
        """FR-3.3: 音频就绪后立即删视频源。"""
        assert video_file.exists()  # 起始存在

        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")

            transcriber.transcribe(video_file)

            assert not video_file.exists()

    def test_video_delete_does_not_block_if_already_gone(self, transcriber, video_file, mock_model):
        """极端情况: 视频已被其他进程删了(并发)→ 主流程仍继续。"""
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")
            # 先删视频
            video_file.unlink()

            # 不抛错
            text = transcriber.transcribe(video_file)
            assert text is not None


# ---------------- faster-whisper 转写 ----------------


class TestWhisperTranscribe:
    def test_uses_config_language(self, transcriber, video_file, mock_model):
        """language 来自 config.whisper.language。"""
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")

            transcriber.transcribe(video_file)

            kwargs = mock_model.transcribe.call_args.kwargs
            assert kwargs["language"] == "zh"

    def test_vad_filter_enabled(self, transcriber, video_file, mock_model):
        """FR-3.x: vad_filter=True 必须开(过滤静音段)。"""
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")

            transcriber.transcribe(video_file)

            kwargs = mock_model.transcribe.call_args.kwargs
            assert kwargs["vad_filter"] is True

    def test_segments_joined_with_newline(self, transcriber, video_file, mock_model):
        """segments 文本用 \\n 拼接返回。"""
        mock_model.transcribe.return_value = (
            make_fake_segments("你好", "这是", "一段测试"),
            MagicMock(language_probability=0.95),
        )

        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")

            text = transcriber.transcribe(video_file)

            assert text == "你好\n这是\n一段测试"

    def test_transcribe_failure_propagates(self, cfg, video_file, mock_model):
        """model.transcribe 抛错 → 异常向上传播(供 FR-3.5 记录)。"""
        mock_model.transcribe.side_effect = RuntimeError("whisper OOM")

        transcriber = StreamingTranscriber(cfg, model=mock_model)
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")

            with pytest.raises(RuntimeError, match="whisper OOM"):
                transcriber.transcribe(video_file)


class TestPostprocessWiring:
    """2026-09-02:StreamingTranscriber 串接 postprocess 的端到端测试。"""

    def test_postprocess_disabled_returns_raw_segments(self, cfg, video_file, mock_model):
        """postprocess_enabled=False → 返回原始 \\n 拼接(不合并碎片)。"""
        cfg.whisper.postprocess_enabled = False
        mock_model.transcribe.return_value = (
            make_fake_segments("你好", "这是", "一段测试"),
            MagicMock(language_probability=0.95),
        )
        transcriber = StreamingTranscriber(cfg, model=mock_model)
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")
            text = transcriber.transcribe(video_file)
        assert text == "你好\n这是\n一段测试"

    def test_postprocess_enabled_merges_short_lines(self, cfg, video_file, mock_model):
        """postprocess_enabled=True → 短碎片行被合并。"""
        cfg.whisper.postprocess_enabled = True
        mock_model.transcribe.return_value = (
            make_fake_segments("第一句長度已經八個字符", "這是另一句也是夠長", "了"),
            MagicMock(language_probability=0.95),
        )
        transcriber = StreamingTranscriber(cfg, model=mock_model)
        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            (video_file.with_suffix(".wav")).write_bytes(b"fake wav")
            text = transcriber.transcribe(video_file)
        # "了"(1 字符) → 并入上一行
        assert text == "第一句長度已經八個字符\n這是另一句也是夠長了"


# ---------------- 懒加载 WhisperModel ----------------


class TestLazyModelLoad:
    def test_model_not_loaded_on_construction(self, cfg):
        """构造函数不加载 WhisperModel(避免启动慢)。"""
        with patch("vla.transcribe.streaming.WhisperModel") as mock_cls:
            StreamingTranscriber(cfg, model=None)
            mock_cls.assert_not_called()

    def test_model_loaded_on_first_transcribe(self, cfg, video_file):
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

            with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
                mock_run.return_value = FakeCompletedProcess(returncode=0)
                (video_file.with_suffix(".wav")).write_bytes(b"fake wav")

                t.transcribe(video_file)

            # 现在加载了
            mock_cls.assert_called_once()
            # 用 config 里的 model size + compute_type
            args, kwargs = mock_cls.call_args
            assert args[0] == "small"  # cfg.whisper.model
            assert kwargs["compute_type"] == "int8"

    def test_injected_model_used_directly(self, cfg, video_file, mock_model):
        """构造函数注入 model → 不重新加载。"""
        with patch("vla.transcribe.streaming.WhisperModel") as mock_cls:
            t = StreamingTranscriber(cfg, model=mock_model)
            with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
                mock_run.return_value = FakeCompletedProcess(returncode=0)
                (video_file.with_suffix(".wav")).write_bytes(b"fake wav")

                t.transcribe(video_file)

            # 没有调用 WhisperModel 构造
            mock_cls.assert_not_called()
            # 但注入的 model 被调用了
            mock_model.transcribe.assert_called_once()


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


# ---------------- 音频文件保留到 cleanup ----------------


class TestAudioFileLifecycle:
    def test_audio_kept_after_successful_transcribe(self, transcriber, video_file, mock_model, tmp_path):
        """transcribe() 成功 → 音频文件保留(等 cleanup() / 质量检查通过后再删)。"""
        audio_path = video_file.with_suffix(".wav")

        with patch("vla.transcribe.streaming.subprocess.run") as mock_run:
            mock_run.return_value = FakeCompletedProcess(returncode=0)
            audio_path.write_bytes(b"fake wav")

            transcriber.transcribe(video_file)

            assert audio_path.exists()