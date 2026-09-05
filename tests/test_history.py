"""HistoryManager 测试(SSOT: requirements.md FR-9.5/9.6 + FR-10.2/10.6 + Phase 7.5)。

设计:
- 启动时从 JSONL 读已有 url_key,填充 _urls 集合(去重查询走内存)
- record_success() 追加一行 + 同步更新 _urls
- make_url_key(group_id, bvid) → "bilibili://group/<group_id>/<bvid>"
- 容错:解析失败的行跳过(不抛)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vla.state.history import HistoryManager


@pytest.fixture
def history_file(tmp_path: Path) -> Path:
    return tmp_path / "transcribed_history.jsonl"


# ---------------- 构造 + 启动加载 ----------------


class TestConstruct:
    def test_empty_file_starts_with_empty_set(self, history_file):
        """history 文件不存在 → _urls 空。"""
        assert not history_file.exists()
        h = HistoryManager(history_file)
        assert h.count == 0

    def test_loads_existing_entries(self, history_file):
        """启动时读已有 entries → 填 _urls。"""
        history_file.write_text(
            json.dumps({"url": "u1", "title": "t1", "duration_sec": 100}) + "\n"
            + json.dumps({"url": "u2", "title": "t2", "duration_sec": 200}) + "\n",
            encoding="utf-8",
        )
        h = HistoryManager(history_file)
        assert h.count == 2
        assert h.is_already_done("u1")
        assert h.is_already_done("u2")
        assert not h.is_already_done("u3")

    def test_skips_malformed_lines(self, history_file):
        """JSON 解析失败或缺 url → 跳过,不抛。"""
        history_file.write_text(
            "not valid json\n"
            + json.dumps({"title": "no url field"}) + "\n"
            + json.dumps({"url": "u1"}) + "\n",
            encoding="utf-8",
        )
        h = HistoryManager(history_file)
        assert h.count == 1
        assert h.is_already_done("u1")


# ---------------- is_already_done ----------------


class TestDedup:
    def test_fresh_url_returns_false(self, history_file):
        h = HistoryManager(history_file)
        assert h.is_already_done("u_new") is False

    def test_recorded_url_returns_true(self, history_file):
        h = HistoryManager(history_file)
        h.record_success("u1", title="t1", duration_sec=100, group_id="g1", source="whisper")
        # 新实例化(模拟 session 重启)
        h2 = HistoryManager(history_file)
        assert h2.is_already_done("u1")


# ---------------- record_success ----------------


class TestRecord:
    def test_writes_jsonl(self, history_file):
        h = HistoryManager(history_file)
        h.record_success(
            url_key="bilibili://group/g1/BV1xxx",
            title="测试",
            duration_sec=1800,
            group_id="g1",
            source="whisper",
        )
        lines = history_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["url"] == "bilibili://group/g1/BV1xxx"
        assert record["title"] == "测试"
        assert record["duration_sec"] == 1800
        assert record["group_id"] == "g1"
        assert record["source"] == "whisper"
        assert "transcribed_at" in record

    def test_appends_multiple(self, history_file):
        h = HistoryManager(history_file)
        for i in range(3):
            h.record_success(f"u{i}", f"t{i}", 100, "g1", "whisper")
        lines = history_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3

    def test_updates_in_memory_set(self, history_file):
        h = HistoryManager(history_file)
        assert not h.is_already_done("u1")
        h.record_success("u1", "t1", 100, "g1", "whisper")
        assert h.is_already_done("u1")


# ---------------- make_url_key ----------------


class TestMakeUrlKey:
    def test_basic(self):
        """make_url_key(group_id, bvid) → bilibili://group/<gid>/<bvid>。"""
        key = HistoryManager.make_url_key("g1", "BV1xxx")
        assert key == "bilibili://group/g1/BV1xxx"

    def test_different_groups_same_video(self):
        """不同 group 同一 bvid → 不同 key(允许同视频属多组)。"""
        k1 = HistoryManager.make_url_key("g1", "BV1")
        k2 = HistoryManager.make_url_key("g2", "BV1")
        assert k1 != k2


# ---------------- count / iter ----------------


class TestInspection:
    def test_count_reflects_disk(self, history_file):
        h = HistoryManager(history_file)
        h.record_success("u1", "t1", 100, "g1", "whisper")
        h.record_success("u2", "t2", 200, "g1", "whisper")
        assert h.count == 2

    def test_iter_returns_records(self, history_file):
        """iter_records() → 生成历史记录(给 UI/统计用)。"""
        h = HistoryManager(history_file)
        h.record_success("u1", "t1", 100, "g1", "whisper")
        h.record_success("u2", "t2", 200, "g2", "api")
        records = list(h.iter_records())
        assert len(records) == 2
        assert records[0]["url"] == "u1"
        assert records[1]["source"] == "api"