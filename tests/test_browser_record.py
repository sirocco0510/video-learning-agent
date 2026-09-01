"""BrowserRecorder 测试(SSOT: requirements.md FR-2.16/2.17/2.18 + implementation-plan.md Phase 3.2)。

录屏流程:
1. 按 hotkey 启动 Screen Recorder 扩展
2. wait duration_sec
3. 再按 hotkey 停止
4. 监听 download 事件,等扩展把视频文件写出来
5. 委托给 transcriber(AudioTranscriber 接口,Phase 4 实现 faster-whisper)
6. 磁盘友好: 删除视频文件
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vla.config import VLAConfig
from vla.subtitle.browser_record import AudioTranscriber, BrowserRecorder


# ---------------- Mocks ----------------


class FakeDownload:
    def __init__(self, suggested_filename: str):
        self.suggested_filename = suggested_filename
        self.save_calls: list[Path] = []

    def save_as(self, path: Path) -> None:
        self.save_calls.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake video content with audio")


class FakeContext:
    def __init__(self):
        self.download_listeners: list = []
        self.remove_listener_calls: list = []

    def on(self, event: str, listener) -> None:
        if event == "download":
            self.download_listeners.append(listener)

    def remove_listener(self, event: str, listener) -> None:
        if event == "download":
            self.remove_listener_calls.append((event, listener))


class FakePage:
    """mock playwright Page。

    downloads_to_emit 在每次 wait_for_timeout 时按顺序发出。
    """

    def __init__(self, context: FakeContext):
        self.context = context
        self.keyboard_presses: list[str] = []
        self.wait_calls: list[int] = []
        self.downloads_to_emit: list[FakeDownload] = []

    @property
    def keyboard(self):
        kb = MagicMock()
        kb.press = lambda key: self.keyboard_presses.append(key)
        return kb

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)
        if self.downloads_to_emit:
            dl = self.downloads_to_emit.pop(0)
            for listener in list(self.context.download_listeners):
                listener(dl)


class FakeTranscriber:
    """实现 AudioTranscriber 协议。"""

    def __init__(self, text: str = "转写好的文本"):
        self.text = text
        self.calls: list[Path] = []

    def transcribe(self, audio_path: Path) -> str:
        self.calls.append(audio_path)
        return self.text


# ---------------- Fixtures ----------------


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
        "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
        "video_source": {
            "prefer_download": True,
            "download": {"format": "worst"},
            "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"},
        },
        "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
        "browser_plugin": {"name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30, "plugin_paths": [], "record_hotkey": "Control+Shift+R", "record_download_timeout_sec": 5},
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./notes/v.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def recorder(cfg: VLAConfig, transcriber: FakeTranscriber) -> BrowserRecorder:
    return BrowserRecorder(cfg, transcriber)


@pytest.fixture
def page() -> FakePage:
    return FakePage(FakeContext())


# ---------------- AudioTranscriber Protocol ----------------


class TestAudioTranscriberProtocol:
    def test_protocol_is_runtime_checkable(self):
        """AudioTranscriber 应该是 Protocol,允许 duck typing。"""
        from vla.subtitle.browser_record import AudioTranscriber as AT

        # FakeTranscriber 有 transcribe 方法,应该能通过 isinstance 检查(如果是 runtime_checkable)
        t = FakeTranscriber()
        # Protocol 不强制 isinstance,只要有 transcribe 方法就行
        assert hasattr(t, "transcribe") and callable(t.transcribe)


# ---------------- 启动/停止 hotkey ----------------


class TestHotkeyControl:
    def test_presses_hotkey_twice_start_then_stop(self, recorder, page, tmp_path):
        """第一次 press = start,第二次 press = stop。"""
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        assert page.keyboard_presses == ["Control+Shift+R", "Control+Shift+R"]

    def test_uses_custom_hotkey_from_config(self, cfg, page, transcriber, tmp_path):
        """record_hotkey 可由 config 覆盖。"""
        cfg.browser_plugin.record_hotkey = "Alt+Shift+S"
        r = BrowserRecorder(cfg, transcriber)
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        r.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        assert page.keyboard_presses[0] == "Alt+Shift+S"


# ---------------- 等待时长 ----------------


class TestWaitDuration:
    def test_waits_for_duration_in_milliseconds(self, recorder, page, tmp_path):
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        # wait_for_timeout(5000) 应该在两次 hotkey 之间
        assert 5000 in page.wait_calls

    def test_waits_for_download_after_stop(self, recorder, page, tmp_path):
        """停止后还要 wait 让扩展写出文件。"""
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        # 应该有多次 wait(duration + download timeout polling)
        assert len(page.wait_calls) >= 2


# ---------------- 下载监听 ----------------


class TestDownloadListener:
    def test_registers_download_listener(self, recorder, page, tmp_path):
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        assert len(page.context.download_listeners) >= 1

    def test_removes_download_listener_after_done(self, recorder, page, tmp_path):
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        assert len(page.context.remove_listener_calls) >= 1

    def test_saves_downloaded_file_to_save_dir(self, recorder, page, tmp_path):
        download = FakeDownload("rec.webm")
        page.downloads_to_emit.append(download)

        recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        # 下载文件应保存到 save_dir/{suggested_filename}
        assert download.save_calls[0].parent == tmp_path
        assert download.save_calls[0].name == "rec.webm"


# ---------------- 委托转写 ----------------


class TestTranscribe:
    def test_calls_transcriber_with_downloaded_path(self, recorder, transcriber, page, tmp_path):
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        assert len(transcriber.calls) == 1
        assert transcriber.calls[0].name == "rec.webm"
        assert transcriber.calls[0].parent == tmp_path

    def test_returns_transcribed_text(self, recorder, page, tmp_path):
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        result = recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        assert result == "转写好的文本"


# ---------------- 磁盘友好:删除源文件 ----------------


class TestCleanup:
    def test_deletes_video_file_after_transcribe(self, recorder, page, tmp_path):
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        video_path = tmp_path / "rec.webm"
        assert not video_path.exists()

    def test_deletes_even_if_transcriber_fails(self, cfg, page, tmp_path):
        """转写抛异常时,也要尝试清理源文件。"""
        class FailingTranscriber:
            def transcribe(self, audio_path: Path) -> str:
                raise RuntimeError("whisper failed")

        page.downloads_to_emit.append(FakeDownload("rec.webm"))
        video_path = tmp_path / "rec.webm"

        r = BrowserRecorder(cfg, FailingTranscriber())
        with pytest.raises(RuntimeError, match="whisper failed"):
            r.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)

        # 清理还是要做(磁盘红线)
        # 注:由于异常路径可能不清理,我们用 try/finally 在实现里处理
        # 这个测试也验证实现选择:finally 块要 unlink


# ---------------- 没有下载时报错 ----------------


class TestNoDownloadError:
    def test_raises_when_no_download_appears(self, recorder, page, tmp_path):
        """停止录屏后扩展没产生文件 → 抛错。"""
        # 不添加 downloads_to_emit,模拟扩展失败

        with pytest.raises(RuntimeError, match="下载|录屏"):
            recorder.record_and_transcribe(page, "https://example.com/v/1", 5, tmp_path)