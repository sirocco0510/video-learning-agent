"""FFmpeg helper — 从输入(任意 ffmpeg 支持格式)抽 wav。

调用方约定:
- input_path 可以是 mp4/webm/m3u8 URL/本地路径
- output_path 必须 .wav 后缀
- 失败时调用方负责决策(降级 / 报警);半截 wav 会被本模块清掉

Phase 9.6.4(2026-09-10):新增 extract_browser_audio — 浏览器内 MediaRecorder 抽音,
用于 yunxuetang (b-learning.bill-jc.com) BCE DRM-encrypted m3u8 兜底(ffmpeg 直抽
被服务端 token 绑定 session 拒)。
"""

from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright


logger = logging.getLogger(__name__)


def extract_audio(input_path: Path, output_path: Path) -> None:
    """ffmpeg 抽 input → wav(output_path)。

    Args:
        input_path: 任意 ffmpeg 支持格式(mp4/webm/m3u8/...)
        output_path: 目标 wav 路径(需 .wav 后缀)

    Raises:
        RuntimeError: ffmpeg 返回非 0
        FileNotFoundError: ffmpeg 二进制缺失或 input 不存在

    失败语义: 半截 wav 在 finally 里被删,避免 3 小时视频抽到一半崩了
    留 345MB 残文件占磁盘。
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-ar", "16000", "-ac", "1", "-f", "wav", str(output_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"extract_audio failed: {input_path} → {output_path}: {proc.stderr}"
            )
    except Exception:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                logger.warning("清理半截 wav 失败 %s,继续", output_path)
        raise


def extract_m3u8_audio(m3u8_url: str, output_path: Path) -> None:
    """ffmpeg 流式抽 m3u8 音轨 → wav, 不缓存视频。

    与 extract_audio 的差异: -vn 跳过视频轨, m3u8 直接走 HLS 流式输入。
    适合 InternalSiteSpider 路径 ②: 3h 视频 ~150MB wav vs 1.5GB mp4。

    Args:
        m3u8_url: HLS manifest URL(可达, 含签名 token)
        output_path: 目标 wav 路径(需 .wav 后缀)

    Raises:
        RuntimeError: ffmpeg 返回非 0(网络/格式错)
        FileNotFoundError: ffmpeg 二进制缺失

    失败语义: 半截 wav 在 except 里被删, 避免磁盘残留。
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-vn",  # 跳过视频轨(磁盘友好: 不缓存 mp4)
        "-i", m3u8_url,
        "-ac", "1", "-ar", "16000", "-f", "wav", str(output_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"extract_m3u8_audio failed: {m3u8_url} → {output_path}: {proc.stderr[:500]}"
            )
    except Exception:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                logger.warning("清理半截 wav 失败 %s, 继续", output_path)
        raise


# Default max duration for browser-based audio capture (avoids indefinite hangs when
# video.duration is NaN / Infinity on DRM-protected HLS streams).
_BROWSER_CAPTURE_MAX_DURATION_SEC = 3600

# Default playback rate for browser-based audio capture. 4x means a 3h video
# captures in ~45 min wall-clock. MediaRecorder gets audio at original sample
# rate (browser internal resample), so faster-whisper transcription is rate-
# agnostic. Chromium supports playbackRate up to ~16x.
_BROWSER_CAPTURE_PLAYBACK_RATE = 4.0

# JS that runs inside the navigated page: force-play the <video>, captureStream() its
# audio track, MediaRecorder → webm/opus chunks → base64 to Python. Awaits either
# "ended" event or maxDurationSec timeout (whichever first).
_BROWSER_CAPTURE_JS = """
async ({maxDurationSec, playbackRate}) => {
    const video = document.querySelector("video");
    if (!video) throw new Error("No <video> element");

    // Ensure autoplay (may need muted first); some browsers require user gesture,
    // but a fresh tab navigated by Playwright usually allows muted autoplay.
    video.muted = false;
    // Speed up playback so capture wall-clock is compressed (3h video @ 4x → 45min).
    // MediaRecorder gets audio at original sample rate (browser internal resample);
    // faster-whisper transcription is rate-agnostic.
    video.playbackRate = playbackRate;
    try { await video.play(); } catch(e) { /* autoplay restricted; will still record if stream is live */ }

    // Wait until we have current data (HAVE_CURRENT_DATA = 2) so stream isn't silent.
    await new Promise((resolve, reject) => {
        if (video.readyState >= 2) return resolve();
        const onReady = () => { video.removeEventListener("canplay", onReady); resolve(); };
        video.addEventListener("canplay", onReady);
        setTimeout(() => reject(new Error("video readyState timeout")), 10000);
    });

    const stream = video.captureStream();
    const audioTracks = stream.getAudioTracks();
    if (audioTracks.length === 0) throw new Error("No audio track");
    const audioStream = new MediaStream(audioTracks);

    const recorder = new MediaRecorder(audioStream, { mimeType: "audio/webm;codecs=opus" });
    window.__vlaChunks = [];
    recorder.ondataavailable = e => { if (e.data.size > 0) window.__vlaChunks.push(e.data); };
    recorder.start(1000);  // 1s chunk granularity
    window.__vlaStartTime = Date.now();

    await new Promise((resolve) => {
        const onEnded = () => resolve();
        video.addEventListener("ended", onEnded, { once: true });
        // Wall-clock budget scaled by playbackRate so maxDurationSec stays in
        // real-time seconds (not video-time seconds).
        setTimeout(resolve, (maxDurationSec / playbackRate) * 1000);
    });

    recorder.stop();
    await new Promise(r => { recorder.onstop = r; });

    const blob = new Blob(window.__vlaChunks, { type: "audio/webm" });
    const buf = await blob.arrayBuffer();
    const u8 = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
    return { b64: btoa(bin), duration: (Date.now() - window.__vlaStartTime) / 1000 };
}
"""


async def extract_browser_audio(
    video_url: str,
    output_path: Path,
    *,
    cdp_url: str = "http://localhost:9222",
    max_duration_sec: int = _BROWSER_CAPTURE_MAX_DURATION_SEC,
    playback_rate: float = _BROWSER_CAPTURE_PLAYBACK_RATE,
) -> Path:
    """浏览器内 MediaRecorder 抽音 → webm → ffmpeg → wav。

    用于 yunxuetang (b-learning.bill-jc.com) BCE DRM-encrypted m3u8 兜底:服务端
    key token 按 session 发放, ffmpeg 直抽会 400 拒。

    流程:
      1. async_playwright + chromium.connect_over_cdp(cdp_url) 借用户主 Chrome
      2. context.new_page() 新建播放页(不污染用户当前 tab)
      3. goto → 等 <video> ready → playbackRate 加速 → MediaRecorder 录 webm/opus
      4. base64 传回 Python → 临时 webm → ffmpeg -vn -ac 1 -ar 16000 转 wav
      5. unlink webm(磁盘友好), close page

    Args:
        video_url: yunxuetang 播放页 URL(已带 kngId / token)
        output_path: 目标 wav 路径(需 .wav 后缀)
        cdp_url: Chrome CDP 端点(默认 localhost:9222)
        max_duration_sec: 视频时长上限(秒,video-time;非 wall-clock);超过兜底停录
        playback_rate: HTMLMediaElement.playbackRate,默认 4x(3h 视频 → 45min
            捕获;MediaRecorder 拿原始采样率音频,浏览器内部 resample)

    Returns:
        output_path

    Raises:
        RuntimeError: 视频元素缺失 / captureStream 无 audio / MediaRecorder 不支持
                      / ffmpeg 转换失败(半截 wav/webm 已被清理)
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    webm_path: Path | None = None
    page = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            page = await context.new_page()

            await page.goto(video_url, wait_until="domcontentloaded")

            try:
                # bill-jc SPA 默认显示课程详情页,<video> 只在用户点 "开始学习"
                # 后才挂载(bind 在 yxtf-button yxtf-button--primary)。
                # 不点的话 querySelector('video') 永远 None。
                # state="attached": 只要 DOM 里有,不要求 visible(可能被课程详情
                # overlay 挡住,但我们 click() 直接调 trigger,绕过 overlay)。
                await page.wait_for_selector(
                    "button.yxtf-button--primary", timeout=30_000, state="attached",
                )
                await page.evaluate(
                    """
                    () => {
                        for (const b of document.querySelectorAll('button')) {
                            if (b.innerText && b.innerText.trim() === '开始学习') {
                                b.click();
                                return true;
                            }
                        }
                        return false;
                    }
                    """
                )
                await page.wait_for_selector("video", timeout=30_000, state="attached")
                await page.wait_for_function(
                    "document.querySelector('video') && document.querySelector('video').readyState >= 2",
                    timeout=30_000,
                )
            except Exception as e:
                raise RuntimeError(
                    f"browser audio capture failed: <video> element not ready: {e}"
                ) from e

            try:
                result = await page.evaluate(
                    _BROWSER_CAPTURE_JS,
                    {"maxDurationSec": max_duration_sec, "playbackRate": playback_rate},
                )
            except Exception as e:
                raise RuntimeError(f"browser audio capture failed: {e}") from e

            if not result or not result.get("b64"):
                raise RuntimeError("browser audio capture failed: empty b64 payload")

            # Write webm → ffmpeg → wav (in tempdir; clean up webm after).
            with tempfile.NamedTemporaryFile(
                prefix="vla_browser_", suffix=".webm", delete=False
            ) as f:
                webm_path = Path(f.name)
                f.write(base64.b64decode(result["b64"]))

            try:
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(webm_path),
                    "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(output_path),
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"ffmpeg webm→wav failed: {proc.stderr[:500]}"
                    )
            except Exception:
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except OSError:
                        logger.warning("清理半截 wav 失败 %s, 继续", output_path)
                raise
    finally:
        # Cleanup webm temp (disk-friendly) + close page we created.
        if webm_path is not None:
            try:
                webm_path.unlink()
            except OSError:
                logger.warning("清理 webm 临时文件失败 %s, 继续", webm_path)
        if page is not None:
            try:
                await page.close()
            except Exception:
                logger.warning("close page 失败, 继续", exc_info=False)

    return output_path
