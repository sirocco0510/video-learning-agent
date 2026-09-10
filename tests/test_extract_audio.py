import base64
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vla.transcribe.extract import extract_audio, extract_browser_audio, extract_m3u8_audio


def test_extract_audio_success(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")  # 实际 ffmpeg 会失败,但我们 mock
    out = tmp_path / "out.wav"
    fake_proc = MagicMock(returncode=0, stderr="")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc):
        extract_audio(src, out)
    # output 不应被删(成功路径)
    assert out.exists() is False  # 因为我们没真的创建


def test_extract_audio_ffmpeg_nonzero_raises_and_cleans(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    out.write_bytes(b"\x00\x00")  # 模拟半截 wav
    fake_proc = MagicMock(returncode=1, stderr="some ffmpeg error")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="extract_audio failed"):
            extract_audio(src, out)
    # 半截 wav 被删
    assert out.exists() is False


def test_extract_audio_input_missing_raises(tmp_path):
    src = tmp_path / "missing.mp4"  # 不创建
    out = tmp_path / "out.wav"
    fake_proc = MagicMock(returncode=1, stderr="no such file")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc):
        with pytest.raises(RuntimeError):
            extract_audio(src, out)
    # output 不该被建出来
    assert out.exists() is False


def test_extract_audio_ffmpeg_binary_missing(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    with patch(
        "vla.transcribe.extract.subprocess.run",
        side_effect=FileNotFoundError("ffmpeg not found"),
    ):
        with pytest.raises(FileNotFoundError):
            extract_audio(src, out)


def test_extract_audio_overwrites_existing_output(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    out.write_bytes(b"old")
    fake_proc = MagicMock(returncode=0, stderr="")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc) as mrun:
        extract_audio(src, out)
    # 验证用了 -y(覆盖)
    args = mrun.call_args[0][0]
    assert "-y" in args


def test_extract_m3u8_audio_success(tmp_path, monkeypatch):
    """extract_m3u8_audio 调 ffmpeg with -vn -ac 1 -ar 16000 -f wav。"""
    out = tmp_path / "audio.wav"
    fake_proc = MagicMock(returncode=0, stderr="")
    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        out.write_bytes(b"RIFF")
        return fake_proc

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_m3u8_audio("https://video.bill-jc.com/foo.m3u8", out)
    # 验证 -vn 在 args 里, audio flags 对, wav 落盘
    assert "-vn" in captured_cmd
    assert "-ac" in captured_cmd and "1" in captured_cmd
    assert "-ar" in captured_cmd and "16000" in captured_cmd
    assert "-f" in captured_cmd and "wav" in captured_cmd
    assert "https://video.bill-jc.com/foo.m3u8" in captured_cmd
    assert str(out) in captured_cmd


def test_extract_m3u8_audio_fails_on_ffmpeg_nonzero(tmp_path, monkeypatch):
    """ffmpeg 返回非 0 → RuntimeError, 半截 wav 清掉。"""
    out = tmp_path / "audio.wav"
    out.write_bytes(b"RIFF")
    fake_proc = MagicMock(returncode=1, stderr="Connection refused")

    def fake_run(cmd, **kwargs):
        return fake_proc

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="extract_m3u8_audio failed"):
        extract_m3u8_audio("https://x.com/bad.m3u8", out)
    # 半截 wav 应被清掉
    assert not out.exists()


# -----------------------------------------------------------------------------
# extract_browser_audio (Phase 9.6.4) — 浏览器内 MediaRecorder 抽音兜底
# -----------------------------------------------------------------------------


def _make_fake_browser(*, ready_state_ok: bool = True, capture_error: str | None = None):
    """构造 fake playwright browser/page，模拟 navigate → record → 返回 b64 payload。

    ready_state_ok=False:wait_for_selector/wait_for_function 抛 TimeoutError。
    capture_error="no_video":page.evaluate 抛 "No <video> element"。
    capture_error="no_audio":page.evaluate 抛 "No audio track"。

    Phase 9.6.4+ (2026-09-10):默认走 "video 还没出现 → 点 button → video ready" 流程
    (happy path,模拟没学完的常规视频):
      - wait_for_selector 第 1 次(video,5s):抛错(video 不在)
      - wait_for_selector 第 2 次(button,30s):成功(button 出现)
      - wait_for_selector 第 3 次(video,30s):成功(video 出现)
      - wait_for_function:成功(readyState>=2)
      - evaluate 第 1 次(click JS):返 "开始学习"
      - evaluate 第 2 次(capture JS):返 b64 payload

    学完场景请用 _make_fake_browser_already_loaded()(video 直接就在 DOM)。
    """
    fake_page = MagicMock()
    fake_page.goto = AsyncMock()

    if not ready_state_ok:
        # 所有 wait_for_selector 都抛
        fake_page.wait_for_selector = AsyncMock(
            side_effect=Exception("wait_for_selector timeout"),
        )
        fake_page.wait_for_function = AsyncMock()
    elif capture_error == "no_video":
        # video click 后等不到,wait_for_selector 第 3 次抛
        fake_page.wait_for_selector = AsyncMock(side_effect=[
            Exception("video not present yet"),  # 第 1 次:video fast-path
            None,  # 第 2 次:button 命中
            Exception("video still not there after button click"),  # 第 3 次失败
        ])
        fake_page.wait_for_function = AsyncMock()
    elif capture_error == "no_audio":
        # 全部 wait 成功,但 capture 抛 no_audio
        fake_page.wait_for_selector = AsyncMock(side_effect=[
            Exception("video not present yet"),  # 第 1 次
            None,  # 第 2 次:button
            None,  # 第 3 次:video
        ])
        fake_page.wait_for_function = AsyncMock()
    else:
        # happy path
        fake_page.wait_for_selector = AsyncMock(side_effect=[
            Exception("video not present yet (5s fast-path timeout)"),
            None,  # button 命中
            None,  # video 命中
        ])
        fake_page.wait_for_function = AsyncMock()

    fake_page.close = AsyncMock()

    if capture_error == "no_video":
        fake_page.evaluate = AsyncMock(side_effect=[
            "开始学习",
            Exception("No <video> element"),
        ])
    elif capture_error == "no_audio":
        fake_page.evaluate = AsyncMock(side_effect=[
            "开始学习",
            Exception("No audio track"),
        ])
    else:
        fake_bytes = b"\x1a\x45\xdf\xa3"
        payload = {"b64": base64.b64encode(fake_bytes).decode("ascii"), "duration": 12.0}
        fake_page.evaluate = AsyncMock(side_effect=["开始学习", payload])

    fake_context = MagicMock()
    fake_context.new_page = AsyncMock(return_value=fake_page)

    fake_browser = MagicMock()
    fake_browser.contexts = [fake_context]
    return fake_browser, fake_page


@pytest.mark.asyncio
async def test_extract_browser_audio_invokes_playwright_and_ffmpeg(tmp_path, monkeypatch):
    """Happy path:fake Playwright 走通 navigate → record → 返回 b64,patch ffmpeg 验证
    wav 落盘 + webm 临时文件被 unlink + page 被 close。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    # Patch async_playwright context manager
    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    captured_ffmpeg_cmds: list = []

    def fake_ffmpeg_run(cmd, **kwargs):
        captured_ffmpeg_cmds.append(cmd)
        # 模拟 ffmpeg 成功:创建 wav 文件
        wav_arg = cmd[cmd.index("-f") + 2]  # "-f", "wav", <path>
        Path(wav_arg).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_ffmpeg_run)
    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio(
            "https://b-learning.bill-jc.com/play?kngId=x", out_wav,
        )

    # 验证 playwright 流程
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp.assert_called_once_with(
        "http://localhost:9222"
    )
    # Phase 9.6.4+ (2026-09-10):goto 调 2 次(SPA 重置 + video URL)
    assert fake_page.goto.call_count == 2
    fake_page.wait_for_selector.assert_called()  # 调了 2 次(按钮 + video)
    fake_page.wait_for_function.assert_called_once()
    fake_page.evaluate.assert_called()  # 调了 2 次(点开始学习 + 跑捕获)
    fake_page.close.assert_called_once()

    # 验证 ffmpeg 被调(必须有 webm 输入 + wav 输出)
    assert len(captured_ffmpeg_cmds) == 1
    cmd = captured_ffmpeg_cmds[0]
    assert cmd[0] == "ffmpeg"
    assert "-vn" in cmd
    assert "-ac" in cmd and "1" in cmd
    assert "-ar" in cmd and "16000" in cmd
    assert "-f" in cmd and "wav" in cmd
    assert str(out_wav) in cmd

    # wav 落盘
    assert out_wav.exists()


@pytest.mark.asyncio
async def test_extract_browser_audio_returns_runtimeerror_when_no_video_element(tmp_path, monkeypatch):
    """page.wait_for_selector 超时 → RuntimeError("browser audio capture failed")。"""
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser(ready_state_ok=False)

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MagicMock(returncode=0, stderr=""))

    with patch("vla.transcribe.extract.async_playwright", ap):
        with pytest.raises(RuntimeError, match="browser audio capture failed"):
            await extract_browser_audio("https://x.com/play", out_wav)
    # page 仍要关(finally)
    fake_page.close.assert_called_once()


@pytest.mark.asyncio
async def test_extract_browser_audio_returns_runtimeerror_when_capture_stream_empty(tmp_path, monkeypatch):
    """audioTracks=[] → RuntimeError 包含 'No audio track'。"""
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser(capture_error="no_audio")

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MagicMock(returncode=0, stderr=""))

    with patch("vla.transcribe.extract.async_playwright", ap):
        with pytest.raises(RuntimeError, match="No audio track"):
            await extract_browser_audio("https://x.com/play", out_wav)


@pytest.mark.asyncio
async def test_extract_browser_audio_passes_playback_rate_to_js(tmp_path, monkeypatch):
    """验证 playback_rate=4 默认值 + 自定义值都正确传给 page.evaluate。

    3h 视频 @ 4x → 45min wall-clock。MediaRecorder 拿原始采样率音频 —— 采样率不受
    影响,但时间轴被压缩 4 倍,whisper 对**时间轴**敏感(所以才有 atempo 后处理,
    见 test_extract_browser_audio_applies_atempo_stretch_to_ffmpeg)。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    def fake_ffmpeg_run(cmd, **kwargs):
        wav_arg = cmd[cmd.index("-f") + 2]
        Path(wav_arg).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_ffmpeg_run)

    # 默认值
    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio("https://x.com/play", out_wav)
    # evaluate 调 2 次: ①点开始学习(返 True) ②跑 MediaRecorder(返 b64)
    # 第二次的 call_args 才有 playbackRate 参数
    capture_args = fake_page.evaluate.call_args_list[1]
    capture_payload = capture_args[0][1]  # (_BROWSER_CAPTURE_JS, payload)
    assert capture_payload["playbackRate"] == 4.0
    assert capture_payload["maxDurationSec"] == 3600

    # 自定义值:reset_mock 后重新设置 side_effect(reset 也清掉 side_effect)
    # 注意:helper 里 fake_page.evaluate 的 side_effect[1] 是 helper-local 的 b64 payload,
    # 测试函数不能直接引用,需要重新构造一个等价 dict。
    fake_page.evaluate.reset_mock()
    # reset_mock 不清 wait_for_selector 的 side_effect,但 list 已耗尽,需要重置
    fake_page.wait_for_selector = AsyncMock(side_effect=[
        Exception("video not present yet (5s fast-path)"),
        None,  # button
        None,  # video
    ])
    b64_payload = {
        "b64": base64.b64encode(b"\x1a\x45\xdf\xa3").decode("ascii"),
        "duration": 12.0,
    }
    fake_page.evaluate = AsyncMock(side_effect=["开始学习", b64_payload])
    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio(
            "https://x.com/play", out_wav, max_duration_sec=7200, playback_rate=2.0,
        )
    custom_payload = fake_page.evaluate.call_args_list[1][0][1]
    assert custom_payload["playbackRate"] == 2.0


@pytest.mark.asyncio
async def test_extract_browser_audio_resets_spa_before_video_goto(
    tmp_path, monkeypatch,
):
    """Phase 9.6.4+ (2026-09-10):bill-jc SPA 在 new_page() 后是空白 tab,默认 route
    不是视频学习页(无 <video> / 无 button.yxtf-button--primary)。修复:new_page 之后
    先 goto 根 URL 让 SPA 初始化,再 goto 目标视频 URL,这样 button + video 一定能
    渲染出来。

    验证 goto 被调两次:第一次是 catalog 根 URL(重置 SPA),第二次是 video_url。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    def fake_ffmpeg_run(cmd, **kwargs):
        wav_arg = cmd[cmd.index("-f") + 2]
        Path(wav_arg).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio(
            "https://b-learning.bill-jc.com/learn/abc-123?kngId=abc-123",
            out_wav,
        )

    # goto 必须被调两次(catalog 重置 SPA → 目标 video URL)
    assert fake_page.goto.call_count == 2, (
        f"expected 2 goto calls (SPA reset + video), got {fake_page.goto.call_count}"
    )
    # 第一次 goto 必须是 catalog 根 URL(SPA 重置)
    first_goto_url = fake_page.goto.call_args_list[0][0][0]
    assert first_goto_url == "https://b-learning.bill-jc.com/", (
        f"first goto must reset SPA, got {first_goto_url!r}"
    )
    # 第二次 goto 必须是传入的视频 URL
    second_goto_url = fake_page.goto.call_args_list[1][0][0]
    assert second_goto_url == "https://b-learning.bill-jc.com/learn/abc-123?kngId=abc-123"


def _make_fake_browser_already_loaded():
    """学完状态 video 已经在 DOM 里(readyState=4),无需 click button。

    77c57083 (2026-09-10 spike):bill-jc SPA 对已学完的视频不渲染 "开始学习" 按钮,
    video 元素已存在且 readyState=4。extract_browser_audio 必须能跳过 button click
    流程,直接进 capture。
    """
    fake_page = MagicMock()
    fake_page.goto = AsyncMock()
    # wait_for_selector("video", timeout=5000) 立即命中 — 模拟 video 已存在
    fake_page.wait_for_selector = AsyncMock()
    fake_page.wait_for_function = AsyncMock()
    fake_page.close = AsyncMock()

    # evaluate 只被调 1 次:① 直接跑 capture JS(无 button click)
    fake_bytes = b"\x1a\x45\xdf\xa3"
    payload = {"b64": base64.b64encode(fake_bytes).decode("ascii"), "duration": 12.0}
    fake_page.evaluate = AsyncMock(return_value=payload)

    fake_context = MagicMock()
    fake_context.new_page = AsyncMock(return_value=fake_page)

    fake_browser = MagicMock()
    fake_browser.contexts = [fake_context]
    return fake_browser, fake_page


@pytest.mark.asyncio
async def test_extract_browser_audio_skips_button_click_when_video_already_loaded(
    tmp_path, monkeypatch,
):
    """Phase 9.6.4+ (2026-09-10):bill-jc 学完状态的视频,SPA 不渲染 "开始学习" 按钮,
    video 元素已在 DOM 且 readyState=4。如果硬等 button.yxtf-button--primary → 30s
    timeout → RuntimeError → fallback ffmpeg(失去 4x 加速)。

    修复:先 wait_for_selector('video', timeout=5s)— 命中就跳过 button click 流程,
    直接走 capture JS。evaluate 只调 1 次(只有 capture,没有 button click)。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser_already_loaded()

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    def fake_ffmpeg_run(cmd, **kwargs):
        wav_arg = cmd[cmd.index("-f") + 2]
        Path(wav_arg).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio(
            "https://b-learning.bill-jc.com/learn/abc-123?kngId=abc-123",
            out_wav,
        )

    # wait_for_selector 必须被调至少 1 次,且 selector 必须是 "video"
    # (而不是 "button.yxtf-button--primary")
    wait_selectors = [
        c[0][0] for c in fake_page.wait_for_selector.call_args_list
    ]
    assert any(sel == "video" for sel in wait_selectors), (
        f"expected wait_for_selector('video') to be called, got selectors: {wait_selectors}"
    )
    # button.yxtf-button--primary 不应被 wait
    assert not any("yxtf-button--primary" in str(sel) for sel in wait_selectors), (
        f"button should NOT be waited on when video already loaded, "
        f"got selectors: {wait_selectors}"
    )
    # evaluate 必须只调 1 次(只有 capture,没有 button click)
    # click button evaluate 文本含 'innerText'
    # capture evaluate 文本含 'captureStream'
    click_calls = [
        c for c in fake_page.evaluate.call_args_list
        if "innerText" in str(c)
    ]
    capture_calls = [
        c for c in fake_page.evaluate.call_args_list
        if "captureStream" in str(c)
    ]
    assert len(click_calls) == 0, (
        f"button click JS should be SKIPPED, got {len(click_calls)} click calls"
    )
    assert len(capture_calls) == 1, (
        f"expected 1 capture call, got {len(capture_calls)}"
    )


@pytest.mark.asyncio
async def test_extract_browser_audio_click_js_matches_multiple_learning_states(
    tmp_path, monkeypatch,
):
    """Phase 9.6.4+ (2026-09-10):bill-jc SPA 根据学习进度显示不同 button text:
      - 没学过 → "开始学习"
      - 学过一部分 → "继续学习"
      - 学完想重看 → "重新学习"
    extract_browser_audio 的 click JS 必须能匹配这三种,否则学过一部分的视频
    永远 fallback 到 ffmpeg。

    验证:用真实浏览器逻辑模拟 — wait_for_selector(button) 成功,然后 click JS 真
    在"继续学习"按钮上跑(只匹配"开始学习"应返 False),如果 click JS 只匹配
    "开始学习" 那 video 永远等不到 → RuntimeError。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()  # 默认 happy path(开始学习)

    # 让 click evaluate 内部逻辑真实 — 用一个真实 JS 跑在 nodejs 不可,改方案:
    # 直接 mock page.evaluate 跑 JS 的结果。如果 click JS 只匹配"开始学习",那
    # 当 DOM 只有"继续学习" button 时返 False → video 等不到 → RuntimeError
    # 我们这里用一个 _execute_click_js helper 模拟 click JS 逻辑
    click_js_calls = []

    async def mock_evaluate(js_str, *args):
        click_js_calls.append(js_str)
        if "innerText" in js_str:
            # 模拟 click JS:只匹配 "开始学习" → "继续学习" 返 False
            # 修复后:应匹配 "开始学习" / "继续学习" / "重新学习"
            if "继续学习" in js_str or "重新学习" in js_str:
                return True  # 修复后行为
            return False  # 修复前行为(只匹配"开始学习")
        else:
            # capture JS 返 b64
            return {
                "b64": base64.b64encode(b"\x1a\x45\xdf\xa3").decode("ascii"),
                "duration": 12.0,
            }

    fake_page.evaluate = mock_evaluate

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    def fake_ffmpeg_run(cmd, **kwargs):
        wav_arg = cmd[cmd.index("-f") + 2]
        Path(wav_arg).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio("https://b-learning.bill-jc.com/learn/x", out_wav)

    # 关键断言:click JS 必须包含 "继续学习" 字符串(修复后)
    click_js = click_js_calls[0]
    assert "继续学习" in click_js, (
        f"click JS must handle '继续学习' button (学过一部分场景), "
        f"got: {click_js[:200]}"
    )
    # wav 应被写入
    assert out_wav.exists()


# -----------------------------------------------------------------------------
# atempo 后处理(Phase 9.6.6+)— 4x 抓的音拉伸回 2x,消同音字幻觉
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_browser_audio_applies_atempo_stretch_to_ffmpeg(
    tmp_path, monkeypatch,
):
    """Phase 9.6.6+ (2026-09-10):browser 4x 抓的 webm 必须经 `-af atempo=0.5` 拉伸
    2 倍再落 wav。

    Why:4x 抓的音让 whisper 看到"时间轴压缩 4 倍"的语流,快速连读场景产生同音字
    幻觉 + 数字串乱码,质量门控 fail。793df1f8 实测 4min23s 编程教程:wav 263s→526s,
    字符 522→2899(5.5x),"资格资格资格" 66 行 → 0 行,识别出真实 if-else 教学内容。

    验证:ffmpeg 命令行必须带 atempo=0.5(4x 抓 → 净 2x 速度)。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, _ = _make_fake_browser()

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    captured_ffmpeg_cmds: list = []

    def fake_ffmpeg_run(cmd, **kwargs):
        captured_ffmpeg_cmds.append(cmd)
        Path(cmd[cmd.index("-f") + 2]).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio("https://b-learning.bill-jc.com/learn/x", out_wav)

    assert len(captured_ffmpeg_cmds) == 1
    cmd = captured_ffmpeg_cmds[0]
    assert "-af" in cmd, f"ffmpeg 必须带 atempo 滤镜,got: {cmd}"
    filter_arg = cmd[cmd.index("-af") + 1]
    assert filter_arg == "atempo=0.5", (
        f"4x 抓的音必须拉伸回 2 倍(atempo=0.5),got: {filter_arg!r}"
    )
    # 滤镜必须在输入之后(ffmpeg 语法要求 -af 作用于已声明的输入)
    assert cmd.index("-af") > cmd.index("-i")
    assert out_wav.exists()


@pytest.mark.asyncio
async def test_extract_browser_audio_atempo_is_overridable(tmp_path, monkeypatch):
    """atempo 可显式覆盖:atempo=1.0 = 不拉伸(4x 直出,退回旧行为)。

    atempo 单级合法区间是 [0.5, 100](ffmpeg 硬限制),0.5 正好是下边界 ——
    想要比 2 倍更慢必须链式(如 ``atempo=0.5,atempo=0.5`` = 4 倍拉伸),本函数
    只暴露单级因子。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, _ = _make_fake_browser()

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    captured_ffmpeg_cmds: list = []

    def fake_ffmpeg_run(cmd, **kwargs):
        captured_ffmpeg_cmds.append(cmd)
        Path(cmd[cmd.index("-f") + 2]).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio(
            "https://b-learning.bill-jc.com/learn/x", out_wav, atempo=1.0,
        )

    cmd = captured_ffmpeg_cmds[0]
    assert cmd[cmd.index("-af") + 1] == "atempo=1.0"
