"""audio_scan.py 测试(F2-10 2026-09-08)。

覆盖:
- find_today_dir 创今天 YYYY-MM-DD/ 文件夹(自动 mkdir)
- scan_untranscribed_audio 空→None / 全已转写→None / 第 1 个未转写→返回 / 按文件名排序
"""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from vla.subtitle.audio_scan import find_today_dir, scan_untranscribed_audio


class TestFindTodayDir:
    def test_creates_today_subdir(self, tmp_path: Path):
        """find_today_dir 应在 downloads_root 下创建 YYYY-MM-DD/ 目录。"""
        result = find_today_dir(tmp_path)
        today = datetime.date.today().isoformat()
        expected = tmp_path / today
        assert result == expected
        assert result.exists()
        assert result.is_dir()

    def test_idempotent_when_today_exists(self, tmp_path: Path):
        """重复调用不抛错(mkdir exist_ok)。"""
        first = find_today_dir(tmp_path)
        second = find_today_dir(tmp_path)
        assert first == second
        assert first.exists()

    def test_creates_parent_chain(self, tmp_path: Path):
        """downloads_root 不存在时,mkdir parents=True 自动建链。"""
        nested = tmp_path / "a" / "b" / "audio"
        result = find_today_dir(nested)
        today = datetime.date.today().isoformat()
        assert result == nested / today
        assert result.exists()

    def test_uses_frozen_today_when_date_mocked(self, tmp_path: Path):
        """模拟 date.today() 返回固定值,验证 YYYY-MM-DD 格式。"""
        with patch("vla.subtitle.audio_scan.date") as mock_date:
            mock_date.today.return_value = datetime.date(2026, 9, 8)
            mock_date.side_effect = lambda *a, **kw: datetime.date(2026, 9, 8)
            result = find_today_dir(tmp_path)
        assert result == tmp_path / "2026-09-08"


class TestScanUntranscribedAudio:
    def test_empty_dir_returns_none(self, tmp_path: Path):
        """空目录 → None。"""
        today_dir = tmp_path / "2026-09-08"
        today_dir.mkdir()
        assert scan_untranscribed_audio(today_dir) is None

    def test_missing_dir_returns_none(self, tmp_path: Path):
        """目录不存在 → None(防御性,不抛)。"""
        missing = tmp_path / "no-such-date"
        assert scan_untranscribed_audio(missing) is None

    def test_all_already_transcribed_returns_none(self, tmp_path: Path):
        """所有 webm 都有 .transcribed.txt sidecar → None。"""
        today_dir = tmp_path / "2026-09-08"
        today_dir.mkdir()
        for name in ("111.webm", "222.webm"):
            (today_dir / name).write_bytes(b"x")
            (today_dir / name).with_suffix(".transcribed.txt").touch()
        assert scan_untranscribed_audio(today_dir) is None

    def test_returns_first_untranscribed(self, tmp_path: Path):
        """返回第 1 个无 sidecar 的 .webm 路径。"""
        today_dir = tmp_path / "2026-09-08"
        today_dir.mkdir()
        (today_dir / "111.webm").write_bytes(b"x")
        (today_dir / "111.webm").with_suffix(".transcribed.txt").touch()
        (today_dir / "222.webm").write_bytes(b"x")
        # 222 没有 sidecar,应返回
        result = scan_untranscribed_audio(today_dir)
        assert result == today_dir / "222.webm"

    def test_returns_sorted_first_when_multiple_untranscribed(self, tmp_path: Path):
        """多文件均未转写 → 返回按文件名排序后的第 1 个(顺序稳定)。"""
        today_dir = tmp_path / "2026-09-08"
        today_dir.mkdir()
        for name in ("999.webm", "111.webm", "555.webm"):
            (today_dir / name).write_bytes(b"x")
        result = scan_untranscribed_audio(today_dir)
        # sorted() → 111.webm 第 1
        assert result == today_dir / "111.webm"

    def test_ignores_non_webm_files(self, tmp_path: Path):
        """非 .webm 文件不参与扫描(只看 .webm)。"""
        today_dir = tmp_path / "2026-09-08"
        today_dir.mkdir()
        (today_dir / "notes.txt").write_text("hi")
        (today_dir / "video.mp4").write_bytes(b"x")
        assert scan_untranscribed_audio(today_dir) is None
