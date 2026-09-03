"""R-15 page lifecycle 测试:验证 BrowserRecorder 不再拥有 page 生命周期。

SSOT:R-15 — 录屏器 should NOT call page.close();caller(SubtitleStrategy /
BilibiliAdapter / FallbackAdapter)owns page lifecycle.

为什么 caller own:
- 同一 page 可能在策略 ② miss 后复用给 ③ 录屏 → 不能让 recorder 关
- 录屏抛错 / 超时时 caller 决定降级(ffmpeg / 标 unavailable)→ 必须保留 page
  给后续流程(或 caller 自己关)
- 跨策略重用避免"录屏完关 page → ③ 又开新 page → Screen Recorder 漏录"

核心断言:
1. 成功路径:`record_and_transcribe` 返回时,page.close() 未被调用
2. 异常路径:transcriber 抛错时,page.close() 仍未被调用
"""

from pathlib import Path

import pytest

from vla.config import VLAConfig
from vla.subtitle.browser_record import BrowserRecorder


# ---------------- Fixtures & Mocks ----------------


class FakeDownload:
    def __init__(self, suggested_filename: str):
        self.suggested_filename = suggested_filename

    def save_as(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake video bytes")


class FakeContext:
    def __init__(self):
        self.download_listeners: list = []

    def on(self, event: str, listener) -> None:
        if event == "download":
            self.download_listeners.append(listener)

    def remove_listener(self, event: str, listener) -> None:
        if event == "download":
            pass


class FakePage:
    """Mock playwright Page — track close() calls so we can assert."""

    def __init__(self, context: FakeContext):
        self.context = context
        self.keyboard_presses: list[str] = []
        self.wait_calls: list[int] = []
        self.downloads_to_emit: list[FakeDownload] = []
        self.close_calls: list[None] = []

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

    def evaluate(self, js: str):
        return None

    def close(self) -> None:
        """page.close() 应由 caller 调用,recorder 不应触发。"""
        self.close_calls.append(None)


class FakeTranscriber:
    """成功路径:返回固定转写文本。"""

    def __init__(self, text: str = "ok"):
        self.text = text

    def transcribe(self, audio_path: Path) -> str:
        return self.text


class FailingTranscriber:
    """失败路径:转写时抛错,触发 recorder 异常分支。"""

    def transcribe(self, audio_path: Path) -> str:
        raise RuntimeError("whisper boom")


# 避免在测试里 import MagicMock 的同时污染:用 importlib-like 写法
from unittest.mock import MagicMock  # noqa: E402


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
        "browser_plugin": {
            "name": "VideoTrans",
            "enabled": True,
            "remind_timeout_sec": 30,
            "plugin_paths": [],
            "record_hotkey": "Control+Shift+R",
            "record_download_timeout_sec": 5,
            "record_pre_grace_sec": 0,
            "record_post_buffer_sec": 0,
        },
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./notes/v.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


@pytest.fixture
def page() -> FakePage:
    return FakePage(FakeContext())


# ---------------- Tests ----------------


class TestPageLifecycle:
    """R-15:BrowserRecorder.record_and_transcribe() 不调 page.close()。"""

    def test_no_page_close_on_success(self, cfg, page, tmp_path):
        """成功路径:recorder 返回 transcript_path 时,page.close() 未被触发。

        契约(R-15):
        - page 由 caller(SubtitleStrategy / BilibiliAdapter / FallbackAdapter)拥有
        - recorder 只负责录屏 + 转写,不参与 page 生命周期
        - 同一 page 可能被策略 ② 探测后复用给 ③ 录屏 → 必须保持存活
        """
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        recorder = BrowserRecorder(cfg, FakeTranscriber())
        result = recorder.record_and_transcribe(
            page, "https://example.com/v/1", 5, tmp_path,
        )

        # sanity:录屏本身成功(没有跳过 download)
        assert result.exists()
        assert result.read_text(encoding="utf-8") == "ok"
        # 核心断言:page.close() 没被调过
        assert page.close_calls == [], (
            f"BrowserRecorder.record_and_transcribe() 调用了 page.close() "
            f"{len(page.close_calls)} 次 — caller 应 own page lifecycle"
        )

    def test_no_page_close_on_exception(self, cfg, page, tmp_path):
        """异常路径:transcriber 抛错时,page.close() 仍未被调用。

        契约(R-15):
        - transcriber 异常 → recorder finally 只清理 listener + 视频源文件
        - page 必须保持存活给 caller(让 caller 决定降级到 ffmpeg / 自己关)
        """
        page.downloads_to_emit.append(FakeDownload("rec.webm"))

        recorder = BrowserRecorder(cfg, FailingTranscriber())

        with pytest.raises(RuntimeError, match="whisper boom"):
            recorder.record_and_transcribe(
                page, "https://example.com/v/1", 5, tmp_path,
            )

        # 核心断言:即使内部抛错,page.close() 仍未被触发
        assert page.close_calls == [], (
            f"BrowserRecorder.record_and_transcribe() 在异常路径调用了 "
            f"page.close() {len(page.close_calls)} 次 — caller 应 own page lifecycle"
        )
