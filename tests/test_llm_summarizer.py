"""LLMSummarizer 测试(SSOT: requirements.md FR-5/7.7 + implementation-plan.md Phase 7)。

设计:
- 从 logs/transcribed/*.txt 读所有字幕(按 mtime 升序),批量 LLM 总结 → 500-800 字
- 写盘路径由调用方提供(notes_file 在 config.summary)
- clear_after=True 时总结完后删除源文件(避免下次重复)
- 返回 Markdown 内容(不含 H1 头部,由主调度负责写 notes_file)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from vla.config import SummaryConfig, VLAConfig
from vla.log.transcribed_file import TranscribedItem
from vla.summary.llm_summarizer import LLMSummarizer


# ---------------- Fixtures ----------------


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
        "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
        "video_source": {"prefer_download": True, "download": {"format": "worst"}, "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"}},
        "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
        "browser_plugin": {"name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30, "plugin_paths": []},
        "summary": {
            "model": "gpt-4o-mini",
            "target_words_min": 500,
            "target_words_max": 800,
            "notes_file": str(tmp_path / "notes.md"),
            "cross_video_dedup": True,
            "trigger_mode": "quota",
            "notes_section_header": "## 学习总结",
        },
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


class FakeLLM:
    def __init__(self, response: str = "这是合并后的统一总结。" * 50):
        self.calls: list[dict[str, Any]] = []
        self.response = response

    def complete(self, prompt: str, max_tokens: int = 1000, temperature: float = 0.3) -> str:
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens, "temperature": temperature})
        return self.response


def write_transcribed(path: Path, title: str, text: str, *, source: str = "whisper", score: int = 85, duration_sec: int = 1800) -> Path:
    """写入一个符合 FR-7.7 格式的字幕文件。"""
    content = (
        f"# {title}\n"
        f"来源:{source} | 质量:{score}/100 | 时长:{duration_sec}s\n\n"
        f"{text}"
    )
    p = path / f"vid_{_safe_stem(title)}.txt"
    p.write_text(content, encoding="utf-8")
    return p


def _safe_stem(title: str) -> str:
    """简化版 safe_title 用于测试 fixture。"""
    out = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", title)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:30] or "untitled"


# ---------------- _load_items ----------------


class TestLoadItems:
    def test_loads_single_file(self, tmp_path: Path):
        """读单个 .txt → 1 个 TranscribedItem,字段正确。"""
        p = write_transcribed(tmp_path, "Python 教程", "正文内容。", source="whisper", score=85, duration_sec=1800)

        summarizer = LLMSummarizer(FakeLLM(), tmp_path)
        items = summarizer._load_items(tmp_path)

        assert len(items) == 1
        item = items[0]
        assert item.title == "Python 教程"
        assert item.source == "whisper"
        assert item.quality_score == 85
        assert item.duration_sec == 1800
        assert item.text == "正文内容。"
        assert item.path == p

    def test_loads_multiple_files_sorted_by_mtime(self, tmp_path: Path):
        """多个文件 → 按 mtime 升序返回。"""
        p1 = write_transcribed(tmp_path, "视频A", "A")
        # 故意写一个 1s 后的文件,确保 mtime 不同
        import time
        time.sleep(1.1)
        p2 = write_transcribed(tmp_path, "视频B", "B")

        summarizer = LLMSummarizer(FakeLLM(), tmp_path)
        items = summarizer._load_items(tmp_path)

        assert len(items) == 2
        # 先写的应该在前面(mtime 升序)
        assert items[0].title == "视频A"
        assert items[1].title == "视频B"

    def test_empty_dir_returns_empty_list(self, tmp_path: Path):
        """空目录 → []。"""
        summarizer = LLMSummarizer(FakeLLM(), tmp_path)
        items = summarizer._load_items(tmp_path)
        assert items == []

    def test_handles_malformed_files_gracefully(self, tmp_path: Path):
        """格式异常的文件不抛,用 stem 作 title + 默认元数据兜底。"""
        (tmp_path / "bad.txt").write_text("这是无效内容,没有 header", encoding="utf-8")
        write_transcribed(tmp_path, "好的", "OK")

        summarizer = LLMSummarizer(FakeLLM(), tmp_path)
        items = summarizer._load_items(tmp_path)

        # 两个文件都加载,不抛
        assert len(items) == 2
        titles = {it.title for it in items}
        assert "好的" in titles
        assert "bad" in titles  # 异常的用 stem 兜底

    def test_handles_completely_garbage_file_without_crashing(self, tmp_path: Path):
        """真正的 garbage(如空白文件)也不抛。"""
        (tmp_path / "empty.txt").write_text("", encoding="utf-8")
        (tmp_path / "binary.txt").write_text("\x00\x01\x02", encoding="utf-8")
        write_transcribed(tmp_path, "OK", "content")

        summarizer = LLMSummarizer(FakeLLM(), tmp_path)
        # 不抛
        items = summarizer._load_items(tmp_path)
        # "OK" 必然加载;garbage 文件以 stem 兜底
        assert any(it.title == "OK" for it in items)

    def test_text_strips_leading_header_blank_line(self, tmp_path: Path):
        """text 字段不含 header,只含正文。"""
        write_transcribed(tmp_path, "T", "这是正文。", source="whisper", score=85, duration_sec=100)
        summarizer = LLMSummarizer(FakeLLM(), tmp_path)
        items = summarizer._load_items(tmp_path)
        # 不含 "# T" 或 "来源:"
        assert "# T" not in items[0].text
        assert "来源:" not in items[0].text
        assert items[0].text == "这是正文。"


# ---------------- summarize_batch ----------------


class TestSummarizeBatch:
    def test_returns_llm_response(self, cfg, tmp_path: Path):
        """summarize_batch → 返回 LLM 响应(去掉 ## 头部)。"""
        write_transcribed(tmp_path, "T", "content")
        llm = FakeLLM(response="合并总结内容。" * 100)
        summarizer = LLMSummarizer(llm, cfg.summary.notes_file)
        summarizer.cfg = cfg  # 注入测试 cfg

        result = summarizer.summarize_batch(tmp_path, group_title=None)

        assert "合并总结内容" in result
        assert len(llm.calls) == 1

    def test_includes_group_header_when_provided(self, cfg, tmp_path: Path):
        """group_title 非 None → 输出以 `## group_title — 累计 X 分钟(N 个视频)` 开头。"""
        write_transcribed(tmp_path, "V1", "c1", duration_sec=1800)
        write_transcribed(tmp_path, "V2", "c2", duration_sec=2400)
        llm = FakeLLM(response="ok")
        summarizer = LLMSummarizer(llm, cfg.summary.notes_file)
        summarizer.cfg = cfg

        result = summarizer.summarize_batch(tmp_path, group_title="Python 基础")

        assert result.startswith("## Python 基础 — 累计 ")
        assert "分钟" in result
        assert "(2 个视频)" in result

    def test_prompt_contains_video_metadata(self, cfg, tmp_path: Path):
        """PROMPT 应包含视频清单 + 各视频 metadata。"""
        write_transcribed(tmp_path, "Python 列表推导式", "列表推导式文本", duration_sec=1800)
        write_transcribed(tmp_path, "Python 装饰器", "装饰器文本", duration_sec=2400)
        llm = FakeLLM(response="ok")
        summarizer = LLMSummarizer(llm, cfg.summary.notes_file)
        summarizer.cfg = cfg

        summarizer.summarize_batch(tmp_path, group_title=None)

        prompt = llm.calls[0]["prompt"]
        assert "Python 列表推导式" in prompt
        assert "Python 装饰器" in prompt
        assert "1800" in prompt
        assert "2400" in prompt
        assert "500" in prompt  # min_words
        assert "800" in prompt  # max_words

    def test_clears_source_dir_after_success(self, cfg, tmp_path: Path):
        """clear_after=True(默认)→ 总结完删源文件。"""
        for i in range(3):
            write_transcribed(tmp_path, f"V{i}", "c")
        assert len(list(tmp_path.glob("*.txt"))) == 3

        llm = FakeLLM(response="ok")
        summarizer = LLMSummarizer(llm, cfg.summary.notes_file)
        summarizer.cfg = cfg
        summarizer.summarize_batch(tmp_path, group_title="测试", clear_after=True)

        assert list(tmp_path.glob("*.txt")) == []

    def test_keeps_files_when_clear_after_false(self, cfg, tmp_path: Path):
        """clear_after=False → 保留源文件。"""
        for i in range(2):
            write_transcribed(tmp_path, f"V{i}", "c")
        llm = FakeLLM(response="ok")
        summarizer = LLMSummarizer(llm, cfg.summary.notes_file)
        summarizer.cfg = cfg

        summarizer.summarize_batch(tmp_path, group_title="X", clear_after=False)

        assert len(list(tmp_path.glob("*.txt"))) == 2

    def test_empty_dir_returns_empty_string(self, cfg, tmp_path: Path):
        """空目录 → 返回空串,不调 LLM。"""
        llm = FakeLLM(response="should not call")
        summarizer = LLMSummarizer(llm, cfg.summary.notes_file)
        summarizer.cfg = cfg

        result = summarizer.summarize_batch(tmp_path, group_title=None)

        assert result == ""
        assert len(llm.calls) == 0


# ---------------- 写盘 ----------------


class TestWriteToNotes:
    def test_append_to_notes_file(self, cfg, tmp_path: Path):
        """write_to_notes(notes_file, content) → 追加到 notes_file(不存在则创建)。"""
        notes_file = cfg.summary.notes_file
        llm = FakeLLM()
        summarizer = LLMSummarizer(llm, notes_file)
        summarizer.cfg = cfg

        summarizer.write_to_notes("## 第一批\n内容1")
        summarizer.write_to_notes("## 第二批\n内容2")

        content = notes_file.read_text(encoding="utf-8")
        assert "## 第一批" in content
        assert "## 第二批" in content

    def test_creates_notes_file_parent_dirs(self, cfg, tmp_path: Path):
        """notes_file 父目录不存在 → 自动创建。"""
        notes_file = tmp_path / "deep" / "nested" / "notes.md"
        assert not notes_file.parent.exists()

        llm = FakeLLM()
        summarizer = LLMSummarizer(llm, notes_file)
        summarizer.cfg = cfg

        summarizer.write_to_notes("test")
        assert notes_file.exists()


# ---------------- 端到端:整批处理 ----------------


class TestEndToEnd:
    def test_full_flow(self, cfg, tmp_path: Path):
        """端到端:读盘 → 总结 → 写 notes_file → 清理源目录。"""
        transcribed_dir = tmp_path / "logs" / "transcribed"
        transcribed_dir.mkdir(parents=True)
        notes_file = cfg.summary.notes_file

        for i, t in enumerate(["Python 列表推导式", "Python 装饰器", "Python 生成器"]):
            write_transcribed(transcribed_dir, t, f"内容{i}", duration_sec=1800 * (i + 1))

        llm = FakeLLM(response="综合总结内容。" * 100)
        summarizer = LLMSummarizer(llm, notes_file)
        summarizer.cfg = cfg

        result = summarizer.summarize_batch(transcribed_dir, group_title="Python 基础")
        summarizer.write_to_notes(result)

        # 写盘
        notes_content = notes_file.read_text(encoding="utf-8")
        assert "Python 基础" in notes_content
        assert "综合总结内容" in notes_content
        # 清理
        assert list(transcribed_dir.glob("*.txt")) == []
        # LLM 被调一次
        assert len(llm.calls) == 1