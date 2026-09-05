"""page_control 测试(SSOT: requirements.md FR-2.17 + Phase 3.5)。

覆盖:
- 页面有 video 元素 → 调 evaluate 暂停,返回 "paused"
- 视频已暂停 → 返回 "already_paused"
- 无 video 元素 → 返回 "no_video_element"
- evaluate 抛错 → log warning,不抛,返回 "no_video_element"
- JS 内容包含 "video" 和 "pause"(确保实现真的暂停 video)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from vla.subtitle.page_control import pause_page_video


class FakePage:
    """Mock browser page:返回预设的 evaluate 值。"""

    def __init__(self, return_value):
        self._return_value = return_value
        self.calls: list[str] = []

    def evaluate(self, js: str):
        self.calls.append(js)
        return self._return_value


# ---------------- happy path ----------------


def test_pauses_video_when_element_exists():
    """页面有 video 元素 → 返回 'paused'。"""
    page = FakePage("paused")
    result = pause_page_video(page)
    assert result == "paused"
    assert len(page.calls) == 1
    js = page.calls[0]
    assert "video" in js
    assert "pause" in js


def test_already_paused_returns_already_paused():
    page = FakePage("already_paused")
    result = pause_page_video(page)
    assert result == "already_paused"


def test_no_video_element_returns_no_video_element():
    page = FakePage("no_video_element")
    result = pause_page_video(page)
    assert result == "no_video_element"


# ---------------- failure handling ----------------


def test_evaluate_exception_does_not_crash():
    """evaluate 抛错 → 返回 'no_video_element' + log warning,不抛。"""
    page = MagicMock()
    page.evaluate.side_effect = RuntimeError("JS execution failed")
    result = pause_page_video(page)
    assert result == "no_video_element"


def test_evaluate_returns_none_falls_back_to_no_video_element():
    """evaluate 返回 None(意外情况)→ 返回 'no_video_element'。"""
    page = FakePage(None)
    result = pause_page_video(page)
    assert result == "no_video_element"


def test_works_with_duck_typed_page():
    """只需 page 有 .evaluate 方法(不依赖 playwright 强类型)。"""

    class CustomPage:
        def __init__(self, return_value):
            self._value = return_value
            self.calls: list[str] = []

        def evaluate(self, js: str):
            self.calls.append(js)
            return self._value

    p = CustomPage("paused")
    result = pause_page_video(p)
    assert result == "paused"
    assert len(p.calls) == 1


# ---------------- JS 脚本正确性 ----------------


def test_js_uses_query_selector_video():
    """JS 用 querySelector('video') 探测 video 元素。"""
    page = FakePage("paused")
    pause_page_video(page)
    js = page.calls[0]
    assert "querySelector" in js
    assert "'video'" in js or '"video"' in js


def test_js_checks_paused_before_calling_pause():
    """JS 先检查 v.paused,避免重复 pause。"""
    page = FakePage("paused")
    pause_page_video(page)
    js = page.calls[0]
    assert "paused" in js