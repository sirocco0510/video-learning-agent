"""reestablish_video_page 测试(SSOT: requirements.md FR-2.28.2g)。

长视频末尾截图前的状态恢复:Chrome 台前 + B站 tab 激活 + 全屏 + playing。
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vla.capture.reestablish import (
    activate_chrome,
    detect_frontmost_app,
    reestablish_video_page,
)


# ---------------- 异步 mock helpers ----------------


def _make_page(url: str = "https://www.bilibili.com/video/BV1abc") -> Any:
    page = MagicMock()
    page.url = url
    page.bring_to_front = AsyncMock()
    page.evaluate = AsyncMock()
    return page


def _make_browser(*urls: str) -> Any:
    pages = []
    for url in urls:
        p = MagicMock()
        p.url = url
        pages.append(p)
    ctx = MagicMock()
    ctx.pages = pages
    browser = MagicMock()
    browser.contexts = [ctx]
    return browser


@pytest.fixture(autouse=True)
def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """避免 asyncio.sleep 真等,加快测试。"""
    async def fast_sleep(_secs: float) -> None:
        return None
    import vla.capture.reestablish as mod
    monkeypatch.setattr(mod.asyncio, "sleep", fast_sleep)


# ---------------- step 1: tab finder 兜底 ----------------


async def test_step1_page_already_on_target_skips_finder() -> None:
    """page.url 已含 bvid → 不调 find_bilibili_page。"""
    page = _make_page()
    browser = _make_browser()
    with patch("vla.capture.reestablish.find_bilibili_page") as mock_find:
        result = await reestablish_video_page(page, browser, "BV1abc")
    mock_find.assert_not_called()
    assert result is page  # 原 page


async def test_step1_url_mismatch_calls_finder_returns_new_page() -> None:
    """page.url 不含 bvid → 调 find_bilibili_page,返回新 page。"""
    page = _make_page(url="https://example.com/something")
    new_page = _make_page(url="https://www.bilibili.com/video/BV1abc")
    browser = _make_browser()
    with patch("vla.capture.reestablish.find_bilibili_page", return_value=new_page) as mock_find:
        result = await reestablish_video_page(page, browser, "BV1abc")
    mock_find.assert_called_once_with(browser, "BV1abc")
    assert result is new_page


async def test_step1_finder_returns_none_uses_original_page() -> None:
    """finder 返回 None → 用原 page 兜底(不抛)。"""
    page = _make_page(url="https://example.com")
    browser = _make_browser()
    with patch("vla.capture.reestablish.find_bilibili_page", return_value=None):
        result = await reestablish_video_page(page, browser, "BV1abc")
    assert result is page  # 原 page 兜底


# ---------------- step 2: frontmost ----------------


async def test_step2_frontmost_not_chrome_activates_chrome() -> None:
    """前台是 Code → activate_chrome 调一次。"""
    page = _make_page()
    browser = _make_browser()
    with patch("vla.capture.reestablish.detect_frontmost_app", return_value="Code"), \
         patch("vla.capture.reestablish.activate_chrome", return_value=True) as mock_act, \
         patch("vla.capture.reestablish.find_bilibili_page"):
        await reestablish_video_page(page, browser, "BV1abc")
    mock_act.assert_called_once()


async def test_step2_frontmost_chrome_skips_activate() -> None:
    """前台是 Chrome → 不调 activate_chrome。"""
    page = _make_page()
    browser = _make_browser()
    with patch("vla.capture.reestablish.detect_frontmost_app", return_value="Google Chrome"), \
         patch("vla.capture.reestablish.activate_chrome") as mock_act, \
         patch("vla.capture.reestablish.find_bilibili_page"):
        await reestablish_video_page(page, browser, "BV1abc")
    mock_act.assert_not_called()


async def test_step2_frontmost_none_skips_activate() -> None:
    """detect 失败 → 不调 activate_chrome(不假设一定是 Chrome)。"""
    page = _make_page()
    browser = _make_browser()
    with patch("vla.capture.reestablish.detect_frontmost_app", return_value=None), \
         patch("vla.capture.reestablish.activate_chrome") as mock_act, \
         patch("vla.capture.reestablish.find_bilibili_page"):
        await reestablish_video_page(page, browser, "BV1abc")
    mock_act.assert_not_called()


# ---------------- step 3: bring to front + focus ----------------


async def test_step3_bring_to_front_and_focus_called() -> None:
    """bring_to_front + window.focus 都调。"""
    page = _make_page()
    browser = _make_browser()
    with patch("vla.capture.reestablish.find_bilibili_page"), \
         patch("vla.capture.reestablish.detect_frontmost_app", return_value="Google Chrome"):
        await reestablish_video_page(page, browser, "BV1abc")
    page.bring_to_front.assert_awaited_once()
    # evaluate("window.focus()") 是 step 3
    focus_call = [c for c in page.evaluate.await_args_list
                  if c.args and "focus" in str(c.args[0])]
    assert len(focus_call) >= 1


async def test_step3_failure_does_not_raise() -> None:
    """bring_to_front 抛 → log warning + 继续后续(fullscreen 仍执行)。"""
    page = _make_page()
    page.bring_to_front = AsyncMock(side_effect=Exception("boom"))
    browser = _make_browser()
    with patch("vla.capture.reestablish.find_bilibili_page"), \
         patch("vla.capture.reestablish.detect_frontmost_app", return_value="Google Chrome"):
        # 不抛
        result = await reestablish_video_page(page, browser, "BV1abc")
    assert result is page


# ---------------- step 4: fullscreen ----------------


async def test_step4_fullscreen_called_with_video_element() -> None:
    """step 4 evaluate 用 document.querySelector('video').requestFullscreen。"""
    page = _make_page()
    browser = _make_browser()
    with patch("vla.capture.reestablish.find_bilibili_page"), \
         patch("vla.capture.reestablish.detect_frontmost_app", return_value="Google Chrome"):
        await reestablish_video_page(page, browser, "BV1abc")
    fullscreen_calls = [c for c in page.evaluate.await_args_list
                        if c.args and "requestFullscreen" in str(c.args[0])]
    assert len(fullscreen_calls) >= 1
    assert "video" in str(fullscreen_calls[0].args[0])


async def test_step4_fullscreen_failure_continues() -> None:
    """fullscreen 抛 → log warning + 继续 step 5。"""
    page = _make_page()
    # step 3 正常,step 4 fullscreen 抛
    call_count = [0]

    async def fake_evaluate(script: str):
        call_count[0] += 1
        if "requestFullscreen" in script:
            raise Exception("NotAllowedError")
        return None

    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    browser = _make_browser()
    with patch("vla.capture.reestablish.find_bilibili_page"), \
         patch("vla.capture.reestablish.detect_frontmost_app", return_value="Google Chrome"):
        result = await reestablish_video_page(page, browser, "BV1abc")
    assert result is page  # 不抛


# ---------------- step 5: resume if paused ----------------


async def test_step5_paused_video_calls_play() -> None:
    """video.paused=true → step 5 evaluate 返回 true → 调 play。"""
    page = _make_page()
    browser = _make_browser()

    # step 5 evaluate 返回 true(代表 paused)
    async def fake_evaluate(script: str):
        if "paused" in script and "play" in script:
            return True
        return None

    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    with patch("vla.capture.reestablish.find_bilibili_page"), \
         patch("vla.capture.reestablish.detect_frontmost_app", return_value="Google Chrome"):
        await reestablish_video_page(page, browser, "BV1abc")
    # play 调用来自 step 5 evaluate 内部,这里只验证 evaluate 被调
    paused_calls = [c for c in page.evaluate.await_args_list
                    if c.args and "paused" in str(c.args[0])]
    assert len(paused_calls) >= 1


async def test_step5_playing_video_does_not_play() -> None:
    """video.paused=false → step 5 evaluate 返回 false → 不 sleep。"""
    page = _make_page()
    browser = _make_browser()

    async def fake_evaluate(script: str):
        if "paused" in script and "play" in script:
            return False  # 没暂停
        return None

    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    with patch("vla.capture.reestablish.find_bilibili_page"), \
         patch("vla.capture.reestablish.detect_frontmost_app", return_value="Google Chrome"):
        await reestablish_video_page(page, browser, "BV1abc")
    paused_calls = [c for c in page.evaluate.await_args_list
                    if c.args and "paused" in str(c.args[0])]
    assert len(paused_calls) >= 1


# ---------------- end-to-end: 整流程不抛 ----------------


async def test_full_flow_returns_page_and_does_not_raise() -> None:
    """整流程 happy path → 返回 page,所有 evaluate 被调。"""
    page = _make_page()
    browser = _make_browser()
    with patch("vla.capture.reestablish.find_bilibili_page"), \
         patch("vla.capture.reestablish.detect_frontmost_app", return_value="Google Chrome"):
        result = await reestablish_video_page(page, browser, "BV1abc")
    assert result is page
    # step 3 (focus) + step 4 (fullscreen) + step 5 (paused check) 至少各 1 次
    evaluate_calls = [str(c.args[0]) for c in page.evaluate.await_args_list if c.args]
    assert any("focus" in s for s in evaluate_calls)
    assert any("requestFullscreen" in s for s in evaluate_calls)
    assert any("paused" in s for s in evaluate_calls)


async def test_cascading_failures_does_not_raise() -> None:
    """每一步都抛 → 仍返回 page(全部异常被吞, log warning)。"""
    page = _make_page()
    page.bring_to_front = AsyncMock(side_effect=Exception("step3"))

    async def fake_evaluate(script: str):
        raise Exception(f"boom:{script[:20]}")

    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    browser = _make_browser()
    with patch("vla.capture.reestablish.find_bilibili_page"), \
         patch("vla.capture.reestablish.detect_frontmost_app", return_value="Code"), \
         patch("vla.capture.reestablish.activate_chrome", return_value=True):
        # 不抛
        result = await reestablish_video_page(page, browser, "BV1abc")
    assert result is page


# ---------------- helpers 自身测试 ----------------


def test_detect_frontmost_app_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """osascript 失败 → 返回 None,不抛。"""
    import vla.capture.reestablish as mod

    def fake_run(*a, **kw):
        raise FileNotFoundError("osascript not found")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert detect_frontmost_app() is None


def test_detect_frontmost_app_returns_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """osascript 成功 → 返回 strip 后的 stdout。"""
    import vla.capture.reestablish as mod

    class R:
        returncode = 0
        stdout = "Google Chrome\n"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert detect_frontmost_app() == "Google Chrome"


def test_activate_chrome_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import vla.capture.reestablish as mod

    class R:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert activate_chrome() is True


def test_activate_chrome_returns_false_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import vla.capture.reestablish as mod

    def fake_run(*a, **kw):
        raise FileNotFoundError("osascript missing")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert activate_chrome() is False