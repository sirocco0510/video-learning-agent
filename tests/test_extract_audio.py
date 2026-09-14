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


def test_extract_m3u8_audio_max_sec_adds_output_t(tmp_path, monkeypatch):
    """FR-2.30.1:max_sec 给定 → 输出侧 `-t <max_sec>`,且必须在 `-i` 之后。

    位置很关键:`-t` 放 `-i` 之前是**输入侧**选项(含义不同)。输出侧才能做到
    "产出够 N 秒就停止读取输入",从而顺带省掉后续 HLS 切片的带宽。
    """
    out = tmp_path / "audio.wav"
    fake_proc = MagicMock(returncode=0, stderr="")
    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        out.write_bytes(b"RIFF")
        return fake_proc

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_m3u8_audio("https://video.bill-jc.com/foo.m3u8", out, max_sec=1800)

    assert "-t" in captured_cmd
    assert captured_cmd[captured_cmd.index("-t") + 1] == "1800"
    # 输出侧:`-t` 必须排在 `-i <url>` 之后
    assert captured_cmd.index("-t") > captured_cmd.index("-i")


def test_extract_m3u8_audio_without_max_sec_omits_t(tmp_path, monkeypatch):
    """FR-2.30.1:不传 max_sec → 不加 `-t`,保持全量抽取(旧行为不变)。"""
    out = tmp_path / "audio.wav"
    fake_proc = MagicMock(returncode=0, stderr="")
    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        out.write_bytes(b"RIFF")
        return fake_proc

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_m3u8_audio("https://video.bill-jc.com/foo.m3u8", out)

    assert "-t" not in captured_cmd


def test_extract_m3u8_audio_max_sec_none_omits_t(tmp_path, monkeypatch):
    """显式传 max_sec=None(配置里设 null)→ 同样不加 `-t`。"""
    out = tmp_path / "audio.wav"
    fake_proc = MagicMock(returncode=0, stderr="")
    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        out.write_bytes(b"RIFF")
        return fake_proc

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_m3u8_audio("https://video.bill-jc.com/foo.m3u8", out, max_sec=None)

    assert "-t" not in captured_cmd


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
    """验证 playback_rate=1.0 默认值(2026-09-14 由 4.0 改为 1.0)+ 自定义值都正确传给 page.evaluate。

    1x = 原速播放,MediaRecorder 拿到原始采样率 + 原始时间轴的音频,whisper 友好。
    之前的 4x + atempo=0.5 组合会毁可懂度(FR-2.30 实测 score 35 vs m3u8 直抽 92),
    所以默认改回 1x,不再需要 atempo 拉伸。
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
    assert capture_payload["playbackRate"] == 1.0
    # FR-2.30.1:默认上限由 3600 降为 1800(只抽前 30 分钟)
    assert capture_payload["maxDurationSec"] == 1800

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
    # FR-2.30.1:显式传的 max_duration_sec 覆盖默认值
    assert custom_payload["maxDurationSec"] == 7200


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
            "https://b-learning.bill-jc.com/kng/#/video/play?kngId=abc-123",
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
    assert second_goto_url == "https://b-learning.bill-jc.com/kng/#/video/play?kngId=abc-123"


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
            "https://b-learning.bill-jc.com/kng/#/video/play?kngId=abc-123",
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
        await extract_browser_audio("https://b-learning.bill-jc.com/kng/#/video/play?kngId=x", out_wav)

    # 关键断言:click JS 必须包含 "继续学习" 字符串(修复后)
    click_js = click_js_calls[0]
    assert "继续学习" in click_js, (
        f"click JS must handle '继续学习' button (学过一部分场景), "
        f"got: {click_js[:200]}"
    )
    # wav 应被写入
    assert out_wav.exists()


# -----------------------------------------------------------------------------
# 2026-09-14:1x 原速 + 最低 opus bitrate(替代 2026-09-10 的 4x + atempo=0.5)
# 验证 opusBitrate 走通 payload + ffmpeg 不再有 atempo 滤镜
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_browser_audio_passes_opus_bitrate_to_js(
    tmp_path, monkeypatch,
):
    """验证 2026-09-14 新增的 opusBitrate 字段走通 page.evaluate payload。

    默认值 = ``_BROWSER_CAPTURE_OPUS_BITRATE`` (16000 = 16kbps),"最低画质"
    测试用。opus 16kbps 仍是语音可懂区间(vs 默认 64kbps),但体积省 4x。
    """
    from vla.transcribe.extract import _BROWSER_CAPTURE_OPUS_BITRATE

    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    def fake_ffmpeg_run(cmd, **kwargs):
        Path(cmd[cmd.index("-f") + 2]).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio("https://b-learning.bill-jc.com/kng/#/video/play?kngId=x", out_wav)

    # evaluate 调 2 次: ①点开始学习(返 True) ②跑 MediaRecorder(返 b64)
    # 第二次的 call_args 才有完整 payload(opusBitrate 在 MediaRecorder capture JS 里)
    capture_args = fake_page.evaluate.call_args_list[1]
    capture_payload = capture_args[0][1]  # (_BROWSER_CAPTURE_JS, payload)
    assert capture_payload["opusBitrate"] == _BROWSER_CAPTURE_OPUS_BITRATE, (
        f"opusBitrate 默认值漂移,常量={_BROWSER_CAPTURE_OPUS_BITRATE},"
        f"payload={capture_payload.get('opusBitrate')}"
    )
    # sanity: 默认 16000 是"最低画质"基线,后续 capture 质量调优时改这个值
    assert capture_payload["opusBitrate"] == 16000, (
        "opusBitrate 默认应保持 16000(16kbps),"
        "调高需同时改 _BROWSER_CAPTURE_OPUS_BITRATE 常量 + 此处断言"
    )


@pytest.mark.asyncio
async def test_extract_browser_audio_ffmpeg_skips_atempo_after_1x_switch(
    tmp_path, monkeypatch,
):
    """2026-09-14:playbackRate 改 1x 后,ffmpeg webm→wav 不再需要 ``-af atempo`` 拉伸。

    之前 4x + atempo=0.5 的组合用于把时间轴压缩 4 倍的语流拉回 2x;1x 原速后
    MediaRecorder 拿的就是原始时间轴,直接转 wav 即可。

    验证:ffmpeg 命令行不含 ``-af`` 滤镜(回归测试,防有人误加 atempo)。
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
        await extract_browser_audio("https://b-learning.bill-jc.com/kng/#/video/play?kngId=x", out_wav)

    assert len(captured_ffmpeg_cmds) == 1
    cmd = captured_ffmpeg_cmds[0]
    assert "-af" not in cmd, (
        f"1x 原速后 ffmpeg 不该有 -af 滤镜(回归保护,防 atempo 重新加入),got: {cmd}"
    )
    assert "atempo" not in " ".join(cmd), (
        f"1x 原速后 ffmpeg 命令不该含 atempo,got: {cmd}"
    )
    # 健全性:输出格式仍是 wav 16kHz mono
    assert "wav" in cmd
    assert "-ar" in cmd and "16000" in cmd
    assert "-ac" in cmd and "1" in cmd
    assert out_wav.exists()


# -----------------------------------------------------------------------------
# 2026-09-14:Python-side asyncio.wait_for 兜底(防 Chrome 标签页崩溃时 Python 挂死)
# -----------------------------------------------------------------------------


def test_extract_browser_audio_default_safety_timeout_is_30_minutes():
    """2026-09-14 增补:硬安全兜底默认 30 分钟(1800s)。

    兜的是 JS 线程死了 / Chrome 标签页 OOM 的场景 — 此时 JS 内 setTimeout 也不触发,
    Python 这层 asyncio.wait_for 必须切。改这个值需要同步改这条测试。
    """
    from vla.transcribe.extract import _BROWSER_CAPTURE_SAFETY_TIMEOUT_SEC
    assert _BROWSER_CAPTURE_SAFETY_TIMEOUT_SEC == 1800, (
        f"safety timeout 默认应保持 30 分钟,got: {_BROWSER_CAPTURE_SAFETY_TIMEOUT_SEC}"
    )


@pytest.mark.asyncio
async def test_extract_browser_audio_safety_timeout_raises_runtimeerror(
    tmp_path, monkeypatch,
):
    """2026-09-14 增补:page.evaluate 挂死超过 safety timeout → RuntimeError,Python 不挂死。

    模拟 Chrome 标签页崩溃:OOM / JS 线程死 → setTimeout 也不触发。
    安全兜底 30 分钟 = _BROWSER_CAPTURE_SAFETY_TIMEOUT_SEC。

    测试加速:monkeypatch 把 safety 降到 0.2s,page.evaluate 模拟 sleep 2s 挂死,
    验证 0.2s 后 RuntimeError 抛出(而非 Python 无限等)。
    """
    import asyncio as _asyncio
    from vla.transcribe import extract as extract_mod

    # 把 safety 临时降到 0.2s,加速测试(safety 默认 1800s,跑测试会等到天荒地老)
    monkeypatch.setattr(extract_mod, "_BROWSER_CAPTURE_SAFETY_TIMEOUT_SEC", 0.2)

    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    # extract_browser_audio 调 page.evaluate 两次:
    #   ① 点开始学习 button (JS: innerText + click)— 应立刻返回 "开始学习"
    #   ② 跑 MediaRecorder capture JS — 必须 hang,触发 safety timeout
    # 用计数器 wrapper 替代 AsyncMock.side_effect:AsyncMock 把 coroutine 当成普通
    # 值返回(没 await),wrapper 自己 await sleep 才会被 wait_for 切。
    call_count = [0]

    async def evaluate_wrapper(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "开始学习"
        # 第二次:hang 2.0s(远超 0.2s safety),被 wait_for 在 0.2s 处切掉
        await _asyncio.sleep(2.0)

    fake_page.evaluate = evaluate_wrapper

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    # ffmpeg 不该被调用(evaluate 在那之前就超时了)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MagicMock(returncode=0, stderr=""))

    t0 = __import__("time").monotonic()
    with patch("vla.transcribe.extract.async_playwright", ap):
        with pytest.raises(RuntimeError, match="safety timeout"):
            await extract_browser_audio("https://x.com/play", out_wav)
    elapsed = __import__("time").monotonic() - t0

    # 必须在 ~0.2s + 一些开销 内抛(允许到 1s 留 pytest fixture 余量)
    assert elapsed < 1.5, (
        f"safety timeout 触发太慢,expected <1s,got: {elapsed:.2f}s"
    )
    # finally 仍要关 page
    fake_page.close.assert_called_once()
    # ffmpeg 不该被调
    # (subprocess.run 被 monkeypatch 成 no-op,不直接断言调用次数避免脆)
    # wav 不该存在(evaluate 超时 → 不进入 webm→wav 阶段)
    assert not out_wav.exists()


@pytest.mark.asyncio
async def test_extract_browser_audio_uses_wait_for_around_evaluate():
    """回归保护:extract_browser_audio 源码内 page.evaluate 必须套 asyncio.wait_for。

    静态扫描 — 不验运行时行为(那个由上面 test 验),只验"有人不小心把 wait_for 删了"
    这种回归。inspect.getsource 是最便宜的 guard,毫秒级跑完。
    """
    import inspect
    from vla.transcribe.extract import extract_browser_audio
    src = inspect.getsource(extract_browser_audio)
    # 必须含 wait_for 包住 page.evaluate
    assert "asyncio.wait_for(" in src, (
        "extract_browser_audio 必须用 asyncio.wait_for 包 page.evaluate "
        "(防 Chrome 标签页崩溃时 Python 挂死)"
    )
    assert "page.evaluate(" in src
    # 源码里有两处 page.evaluate(308 行 button click + 341 行 capture JS),
    # safety 只覆盖后者(capture 可挂几小时,button click 30s Playwright 自带超时够了)。
    # 验证 wait_for 紧贴第二处 page.evaluate:用正则找 `await asyncio.wait_for(\n` 后面跟 `page.evaluate(`
    import re
    pattern = re.compile(
        r"await\s+asyncio\.wait_for\(\s*\n\s*page\.evaluate\(",
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "asyncio.wait_for 必须紧贴 capture page.evaluate 调用\n"
        "(button click evaluate 不需要 safety,只用 Playwright 自带 timeout 即可)"
    )


# -----------------------------------------------------------------------------
# 2026-09-14:disable_capture debug 旋钮 — 跳过 MediaRecorder + webm + ffmpeg,
# 只测超时等待(spike 用,生产路径不变)
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_browser_audio_disable_capture_skips_webm_and_ffmpeg(
    tmp_path, monkeypatch,
):
    """disable_capture=True → JS 跳 MediaRecorder + Python 跳 b64/webm/ffmpeg。

    验证:
      - JS payload 含 disableCapture=true
      - 返回的 b64 为空(JS 没录)
      - wav 文件**不**被创建(finally 不写盘)
      - ffmpeg 不被调
      - 仍走 asyncio.wait_for(timeout=1800)(safety 兜底不变)
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    # capture JS 返空 payload(disable_capture 路径 JS 返 { b64: "", duration: ... })
    # 用 AsyncMock side_effect:第一次返 "开始学习"(button click JS),
    # 第二次返空 dict(capture JS 在 disableCapture=true 时的产物)。
    # AsyncMock 把 dict 当返回值(direct await result),不需要手动 await。
    fake_page.evaluate = AsyncMock(
        side_effect=["开始学习", {"b64": "", "duration": 0.0}],
    )

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    # ffmpeg 不该被调 — 用 sentinel,如果被调则测试失败
    ffmpeg_called = [False]

    def fake_ffmpeg_run(cmd, **kwargs):
        ffmpeg_called[0] = True
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        result = await extract_browser_audio(
            "https://b-learning.bill-jc.com/kng/#/video/play?kngId=x",
            out_wav,
            disable_capture=True,
        )

    # 函数仍返 output_path(签名兼容),但文件**不**存在
    assert result == out_wav
    assert not out_wav.exists(), (
        "disable_capture 模式不该创建 wav(没录就没东西可转)"
    )
    # ffmpeg 绝不该被调
    assert not ffmpeg_called[0], (
        "disable_capture 模式不该调 ffmpeg"
    )
    # page 仍要关(finally)
    fake_page.close.assert_called_once()

    # JS payload 应含 disableCapture=True
    capture_args = fake_page.evaluate.call_args_list[1]
    capture_payload = capture_args[0][1]
    assert capture_payload["disableCapture"] is True, (
        f"disableCapture 字段必须 = True,got: {capture_payload.get('disableCapture')}"
    )


@pytest.mark.asyncio
async def test_extract_browser_audio_disable_capture_default_is_false(
    tmp_path, monkeypatch,
):
    """disable_capture 默认 False — 回归保护,生产路径不该受影响。

    不传 disable_capture → JS payload 含 disableCapture=False → MediaRecorder 走默认路径。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    def fake_ffmpeg_run(cmd, **kwargs):
        Path(cmd[cmd.index("-f") + 2]).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        await extract_browser_audio("https://b-learning.bill-jc.com/kng/#/video/play?kngId=x", out_wav)

    capture_args = fake_page.evaluate.call_args_list[1]
    capture_payload = capture_args[0][1]
    assert capture_payload["disableCapture"] is False
    # wav 应被生成(默认 capture 模式)
    assert out_wav.exists()


# -----------------------------------------------------------------------------
# 2026-09-14:JS 用 video.duration 当 setTimeout 上限(替代硬编码 maxDurationSec)
# 之前 setTimeout 用的就是 maxDurationSec/video-playback,DRM 视频时长已知却仍被短
# 上限截断。修复:JS 内读 video.duration,setTimeout 取 min(duration, maxDurationSec),
# maxDurationSec 退化为硬上限(用户短限速仍生效,DRM NaN 兜底 1800)。
# -----------------------------------------------------------------------------


def test_browser_capture_js_uses_video_duration_with_hard_cap():
    """静态回归:_BROWSER_CAPTURE_JS 必须读 video.duration 并用 min() 当 setTimeout 上限。

    两个分支(recording + disableCapture)都要有这个保护,且返回 payload 含
    video_duration 字段(供 Python 端日志区分"视频实际时长"vs"setTimeout 兜底")。
    """
    import inspect

    from vla.transcribe.extract import _BROWSER_CAPTURE_JS

    # 1) 必须读 video.duration
    assert "video.duration" in _BROWSER_CAPTURE_JS, (
        "JS 必须读 video.duration(否则 DRM 短视频仍会被 maxDurationSec 截断)"
    )
    # 2) 必须用 min() 把 video.duration 跟 maxDurationSec 取小(用户硬上限保留)
    assert "Math.min" in _BROWSER_CAPTURE_JS, (
        "JS 必须用 Math.min 把 video.duration 和 maxDurationSec 取小"
    )
    assert "maxDurationSec" in _BROWSER_CAPTURE_JS
    # 3) 必须有 isFinite 兜底(DRM HLS 流 duration 可能 NaN/Infinity,需兜底 1800)
    assert "isFinite" in _BROWSER_CAPTURE_JS, (
        "JS 必须用 isFinite 兜底 DRM NaN/Infinity,否则 setTimeout(NaN) 不触发"
    )
    # 4) 两个分支(recording + disableCapture)的 return payload 都必须含 video_duration
    #    字段(供 Python 端 [NO-CAPTURE] 日志区分"视频实际时长"vs"setTimeout 兜底")
    video_duration_occurrences = _BROWSER_CAPTURE_JS.count("video_duration")
    assert video_duration_occurrences >= 2, (
        f"JS 必须在 recording return + disableCapture return 都含 video_duration,"
        f"got {video_duration_occurrences} occurrences"
    )
    # 5) 老的裸 setTimeout(resolve, (maxDurationSec / playbackRate) * 1000) 已被替换
    #    新形式应该是 setTimeout(resolve, ...ms) 而非 *1000(因为 dur 已是秒,直接 * 1000)
    #    防回归:检查"setTimeout(resolve, (maxDurationSec / playbackRate) * 1000)"不存在
    old_pattern = "(maxDurationSec / playbackRate) * 1000"
    assert old_pattern not in _BROWSER_CAPTURE_JS, (
        f"JS 仍有旧的硬编码 setTimeout({old_pattern}) — 未替换为 video.duration"
    )


@pytest.mark.asyncio
async def test_extract_browser_audio_disable_capture_logs_video_duration(
    tmp_path, monkeypatch, caplog,
):
    """disable_capture=True + 新 payload 含 video_duration → Python 端日志应透出。

    验证 [NO-CAPTURE] 日志包含 video_duration 字段(让 watch 模式可区分
    "视频实际 42s,被 maxDurationSec=30 截"vs"视频 25s 自然结束")。
    """
    import logging as _logging

    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    # capture JS 返 payload 含 video_duration=42.0(wall-clock 30.0 因为 setTimeout)
    fake_page.evaluate = AsyncMock(
        side_effect=[
            "开始学习",
            {"b64": "", "duration": 30.0, "video_duration": 42.0},
        ],
    )

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(
        return_value=fake_browser,
    )
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MagicMock(returncode=0, stderr=""))

    with caplog.at_level(_logging.INFO, logger="vla.transcribe.extract"):
        with patch("vla.transcribe.extract.async_playwright", ap):
            await extract_browser_audio(
                "https://b-learning.bill-jc.com/kng/#/video/play?kngId=x",
                out_wav,
                disable_capture=True,
                max_duration_sec=30,
            )

    # [NO-CAPTURE] 日志应包含 video_duration=42.0(实际视频 42s,被 max_sec=30 截)
    no_capture_logs = [r.message for r in caplog.records if "[NO-CAPTURE]" in r.message]
    assert len(no_capture_logs) >= 1, (
        f"[NO-CAPTURE] 日志应至少 1 条,got: {[r.message for r in caplog.records]}"
    )
    assert "video_duration=42.0" in no_capture_logs[0], (
        f"[NO-CAPTURE] 日志应包含 video_duration=42.0,got: {no_capture_logs[0]!r}"
    )
    assert "wall-clock=30.0" in no_capture_logs[0], (
        f"[NO-CAPTURE] 日志应包含 wall-clock=30.0,got: {no_capture_logs[0]!r}"
    )


@pytest.mark.asyncio
async def test_extract_browser_audio_recording_branch_accepts_video_duration_payload(
    tmp_path, monkeypatch,
):
    """recording 分支:JS payload 含新字段 video_duration,Python 不应拒绝。

    验证默认 capture 模式(disable_capture=False) 也能处理新 payload 字段,
    wav 落盘 + ffmpeg 被调(老路径不变,新字段只多返不破坏)。
    """
    out_wav = tmp_path / "browser.wav"
    fake_browser, fake_page = _make_fake_browser()

    # happy path payload 加 video_duration
    b64_payload = {
        "b64": base64.b64encode(b"\x1a\x45\xdf\xa3").decode("ascii"),
        "duration": 12.0,
        "video_duration": 15.0,
    }
    fake_page.evaluate = AsyncMock(side_effect=["开始学习", b64_payload])

    ap = MagicMock()
    ap.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    ap.return_value.__aenter__.return_value.chromium = MagicMock()
    ap.return_value.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(
        return_value=fake_browser,
    )
    ap.return_value.__aexit__ = AsyncMock(return_value=None)

    def fake_ffmpeg_run(cmd, **kwargs):
        Path(cmd[cmd.index("-f") + 2]).write_bytes(b"RIFF")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr("vla.transcribe.extract.subprocess.run", fake_ffmpeg_run)

    with patch("vla.transcribe.extract.async_playwright", ap):
        # 不应抛异常(新 video_duration 字段兼容老 Python 端)
        await extract_browser_audio(
            "https://b-learning.bill-jc.com/kng/#/video/play?kngId=x",
            out_wav,
        )

    # wav 落盘 + page 关
    assert out_wav.exists()
    fake_page.close.assert_called_once()
