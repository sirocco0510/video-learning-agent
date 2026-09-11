"""FFmpeg helper — 从输入(任意 ffmpeg 支持格式)抽 wav。

调用方约定:
- input_path 可以是 mp4/webm/m3u8 URL/本地路径
- output_path 必须 .wav 后缀
- 失败时调用方负责决策(降级 / 报警);半截 wav 会被本模块清掉

Phase 9.6.4(2026-09-10):新增 extract_browser_audio — 浏览器内 MediaRecorder 抽音,
用于 yunxuetang (b-learning.bill-jc.com) BCE DRM-encrypted m3u8 兜底(ffmpeg 直抽
被服务端 token 绑定 session 拒)。

Phase 9.6.6(2026-09-10):extract_browser_audio 的 webm→wav 转换加 ffmpeg
``-af atempo`` 后处理 — 4x 抓的音时间轴被压缩 4 倍,直送 whisper 会在快速连读场景
产生同音字幻觉("资格资格资格" 刷 66 行)+ 数字串乱码;atempo=0.5 拉伸 2 倍后消失。
详见 _BROWSER_CAPTURE_ATEMPO。
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


def extract_m3u8_audio(
    m3u8_url: str,
    output_path: Path,
    max_sec: int | None = None,
) -> None:
    """ffmpeg 流式抽 m3u8 音轨 → wav, 不缓存视频。

    与 extract_audio 的差异: -vn 跳过视频轨, m3u8 直接走 HLS 流式输入。
    适合 InternalSiteSpider 路径 ②: 3h 视频 ~150MB wav vs 1.5GB mp4。

    Args:
        m3u8_url: HLS manifest URL(可达, 含签名 token)
        output_path: 目标 wav 路径(需 .wav 后缀)
        max_sec: 只抽前 N 秒, 超出丢弃(FR-2.30.1)。None / 0 → 全量。
            `-t` 放在 `-i` **之后** 是输出侧选项: ffmpeg 产出够 N 秒就停止
            读取输入, 所以既截音频也省带宽(不再拉后续 HLS 切片)。

    Raises:
        RuntimeError: ffmpeg 返回非 0(网络/格式错)
        FileNotFoundError: ffmpeg 二进制缺失

    失败语义: 半截 wav 在 except 里被删, 避免磁盘残留。
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-vn",  # 跳过视频轨(磁盘友好: 不缓存 mp4)
        "-i", m3u8_url,
    ]
    if max_sec:
        cmd += ["-t", str(max_sec)]
    cmd += ["-ac", "1", "-ar", "16000", "-f", "wav", str(output_path)]
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


# Default max duration for browser-based audio capture. 双重作用:
#   ① 防挂死 —— video.duration 在 DRM 保护的 HLS 流上可能是 NaN / Infinity
#   ② FR-2.30.1 产品上限 —— 默认只留前 30 分钟(2026-09-10,3600 → 1800)。
# 调用方通常显式传 `audio.max_extract_sec`;此处默认值只在漏传时兜底。
_BROWSER_CAPTURE_MAX_DURATION_SEC = 1800

# Default playback rate for browser-based audio capture. 4x means a 3h video
# captures in ~45 min wall-clock. MediaRecorder gets audio at original sample
# rate (browser internal resample) — the *sample rate* is unchanged, but the
# *timeline* is compressed 4x, which whisper is NOT agnostic to (see
# _BROWSER_CAPTURE_ATEMPO). Chromium supports playbackRate up to ~16x.
_BROWSER_CAPTURE_PLAYBACK_RATE = 4.0

# Phase 9.6.6+ (2026-09-10):4x 抓的音让 whisper 看到"时间轴压缩 4 倍"的语流 ——
# 采样率没变,但音素密度是真实语速的 4 倍。快速连读场景(编程教程 / 公司简介 /
# 商业宣传片)因此产生同音字幻觉("资格资格资格" 连刷 66 行)和数字串乱码,质量
# 门控 fail。atempo 把时域拉伸 1/atempo 倍,把语流密度降回来。
#
# 0.5 = 拉伸 2 倍,即 4x 抓的音变成净 2x 速度(而非完全还原 1x)。
# 793df1f8 实测(4min23s 编程 if-else 教程):wav 263s → 526s,字符 522 → 2899
# (5.5x),"资格资格资格" 66 行 → 0 行,转出真实教学内容。
#
# 注:ffmpeg atempo 单级合法区间 [0.5, 100],0.5 正好是下边界 —— 要拉伸超过 2 倍
# 必须链式(如 "atempo=0.5,atempo=0.5" = 4 倍),本模块只暴露单级因子。
_BROWSER_CAPTURE_ATEMPO = 0.5

# JS that runs inside the navigated page: force-play the <video>, captureStream() its
# audio track, MediaRecorder → webm/opus chunks → base64 to Python. Awaits either
# "ended" event or maxDurationSec timeout (whichever first).
_BROWSER_CAPTURE_JS = """
async ({maxDurationSec, playbackRate}) => {
    const video = document.querySelector("video");
    if (!video) throw new Error("No <video> element");

    // Phase 9.6.4+ (2026-09-10):学完状态(capture JS 第一次被调用时,video 可能已 ended)
    // 重置 currentTime 到 0,否则 addEventListener("ended") 永远不触发(事件已发过),
    // capture JS 会一直等 setTimeout(15min)。seekTo(0) 强制从头播放。
    if (video.ended || (video.duration && video.currentTime >= video.duration - 1)) {
        try { video.currentTime = 0; } catch(e) { /* seek 失败也继续 */ }
    }

    // Ensure autoplay (may need muted first); some browsers require user gesture,
    // but a fresh tab navigated by Playwright usually allows muted autoplay.
    video.muted = false;
    // Speed up playback so capture wall-clock is compressed (3h video @ 4x → 45min).
    // MediaRecorder gets audio at original sample rate (browser internal resample), so
    // the *sample rate* is preserved — but the *timeline* is compressed by this factor,
    // which faster-whisper is NOT agnostic to. The Python side undoes the compression
    // with ffmpeg atempo (see _BROWSER_CAPTURE_ATEMPO); without it, fast continuous
    // speech produces homophone hallucinations.
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
    atempo: float = _BROWSER_CAPTURE_ATEMPO,
) -> Path:
    """浏览器内 MediaRecorder 抽音 → webm → ffmpeg → wav。

    用于 yunxuetang (b-learning.bill-jc.com) BCE DRM-encrypted m3u8 兜底:服务端
    key token 按 session 发放, ffmpeg 直抽会 400 拒。

    流程:
      1. async_playwright + chromium.connect_over_cdp(cdp_url) 借用户主 Chrome
      2. context.new_page() 新建播放页(不污染用户当前 tab)
      3. **SPA 重置**:先 goto bill-jc 根 URL 让 SPA 初始化(否则 new_page 后的空白
         tab 默认 route 不是视频学习页,button.yxtf-button--primary 永远不渲染)
      4. goto video_url → 等 button → 点"开始学习" → 等 <video> ready →
         playbackRate 加速 → MediaRecorder 录 webm/opus
      5. base64 传回 Python → 临时 webm → ffmpeg -vn -ac 1 -ar 16000
         **-af atempo={atempo}** 转 wav(tempo 拉伸把 4x 压缩的语流密度降回来)
      6. unlink webm(磁盘友好), close page

    Args:
        video_url: yunxuetang 播放页 URL(已带 kngId / token)
        output_path: 目标 wav 路径(需 .wav 后缀)
        cdp_url: Chrome CDP 端点(默认 localhost:9222)
        max_duration_sec: 视频时长上限(秒,video-time;非 wall-clock);超过兜底停录
        playback_rate: HTMLMediaElement.playbackRate,默认 4x(3h 视频 → 45min
            捕获;MediaRecorder 拿原始采样率音频,浏览器内部 resample)
        atempo: ffmpeg atempo 因子,默认 0.5(时域拉伸 2 倍)。4x 抓的音因此变成
            净 2x 速度 —— 不拉伸(1.0)会让 whisper 在快速连读场景产生同音字幻觉。
            合法区间 [0.5, 100];0.5 是下边界,更慢需链式。

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

            # Phase 9.6.4+ (2026-09-10):SPA 重置 — new_page() 创建空白 tab,
            # bill-jc SPA 默认 route 不是视频学习页(SPA 状态未知)。先 goto 根
            # URL 让 SPA 初始化,再 goto 目标 video URL,这样 button 一定能渲染。
            # 否则 30s 等不到 button.yxtf-button--primary → RuntimeError → fallback。
            await page.goto(
                "https://b-learning.bill-jc.com/", wait_until="domcontentloaded",
            )
            await page.goto(video_url, wait_until="domcontentloaded")

            try:
                # Phase 9.6.4+ (2026-09-10):bill-jc SPA 根据学习进度显示不同 button:
                #   - 没学过 → "开始学习"
                #   - 学过一部分 → "继续学习"
                #   - 学完想重看 → "重新学习"
                #   - 已学完 → 没有 button,video 元素已在 DOM(readyState=4)
                # 学完状态(77c57083 spike 发现)如果没有 fast-path 检测,SPA 不渲染
                # 任何"开始学习"类 button → 等 30s timeout → fallback ffmpeg,失去 4x。
                #
                # 流程:
                #   ① 先快速 wait_for_selector("video", timeout=5s)— 学完场景命中
                #   ② 否则 wait button → click(支持 3 种 text) → wait video ready
                try:
                    await page.wait_for_selector("video", timeout=5_000, state="attached")
                    logger.info(
                        "[BROWSER] video element already present (学完场景),"
                        " skip button click — go directly to capture"
                    )
                except Exception:
                    # video 不在,需要点 button 进 player
                    await page.wait_for_selector(
                        "button.yxtf-button--primary", timeout=30_000, state="attached",
                    )
                    await page.evaluate(
                        """
                        () => {
                            const targets = ['开始学习', '继续学习', '重新学习'];
                            for (const b of document.querySelectorAll('button')) {
                                const txt = b.innerText ? b.innerText.trim() : '';
                                if (targets.includes(txt)) {
                                    b.click();
                                    return txt;  // 返哪个 text 被点,便于排查
                                }
                            }
                            return null;
                        }
                        """
                    )
                    await page.wait_for_selector(
                        "video", timeout=30_000, state="attached",
                    )
                    await page.wait_for_function(
                        "document.querySelector('video') && "
                        "document.querySelector('video').readyState >= 2",
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
                    "-vn", "-ac", "1", "-ar", "16000",
                    # Phase 9.6.6+ (2026-09-10):4x 抓的音时间轴被压缩 4 倍,atempo
                    # 拉伸 1/atempo 倍把语流密度降回 whisper 友好区间(默认 0.5
                    # = 净 2x)。不拉伸会在快速连读场景产生同音字幻觉,详见
                    # _BROWSER_CAPTURE_ATEMPO 注释。
                    "-af", f"atempo={atempo}",
                    "-f", "wav", str(output_path),
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
