"""Whisper 转写的音频预处理 — 从视频源抽 whisper 规格的 wav。

**模块定位** — 本模块是 faster-whisper 流式转写的**前置步骤**,不是通用音频抽音工具:

- 唯一调用方:``transcribe/streaming.py``(whisper streaming 入口)
- 所有 wav 参数(16kHz、mono)都是 whisper 的输入需求,**不是** 通用音频规格;
  移到 source/ 会让源端反过来"知道"下游是 whisper,耦合方向反了
- 因此放在 ``transcribe/`` 下,而不是 ``source/``;历史原因见 commit ab54249

如果未来要服务其他音频消费方(摘要 / 关键词提取),应抽到独立模块(如
``audio_extract.py``),把 16k/mono 等 whisper 私有需求做成调用方传入的参数。

调用方约定:
- input_path 可以是 mp4/webm/m3u8 URL/本地路径
- output_path 必须 .wav 后缀
- 失败时调用方负责决策(降级 / 报警);半截 wav 会被本模块清掉

Phase 9.6.4(2026-09-10):新增 extract_browser_audio — 浏览器内 MediaRecorder 抽音,
原用于 yunxuetang (b-learning.bill-jc.com) BCE DRM-encrypted m3u8 兜底(ffmpeg 直抽
被服务端 token 绑定 session 拒)。**2026-09-14** 改为 ``main_provider`` 路径 ② m3u8
失败时不再走 browser fallback(直接跳过视频);函数本身保留供测试 1x + 最低画质的
耗时/性能。

Phase 9.6.6(2026-09-10,2026-09-14 移除):曾加 ffmpeg ``-af atempo`` 后处理,应对 4x
抓音的语流压缩问题。**2026-09-14 改为 1x 原速播放后 atempo 不再需要**,该段删除。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright


logger = logging.getLogger(__name__)


def extract_audio(input_path: Path, output_path: Path) -> None:
    """ffmpeg 抽 input → wav(output_path),按 whisper 输入规格(16kHz/mono)输出。

    Args:
        input_path: 任意 ffmpeg 支持格式(mp4/webm/m3u8/...)
        output_path: 目标 wav 路径(需 .wav 后缀,wav 会被写成 16kHz / mono)

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

# Default playback rate for browser-based audio capture (2026-09-14 由 4.0 改为 1.0)。
# 1x = 原速播放,MediaRecorder 拿原始采样率 + 原始时间轴的音频,whisper 友好。
# 之前的 4x 是为压缩 wall-clock(3h → 45min),但配合 atempo=0.5 后仍毁可懂度
# (FR-2.30 实测 score 35 vs m3u8 直抽 92);2026-09-14 决定:
#   - m3u8 失败不再 fallback 到 browser(见 main_provider.py 注释)
#   - browser 函数保留供测试,默认改 1x 测基线耗时/性能
_BROWSER_CAPTURE_PLAYBACK_RATE = 1.0

# MediaRecorder opus bitrate(2026-09-14 新增)。默认 16000 = 16kbps,opus 下限偏上,
# 语音仍可懂但体积省 4x(对比默认 ~64kbps)。用于测试"最低画质"录屏的耗时/性能。
# 调高 bitrate 改这里即可,JS 端从 payload `opusBitrate` 字段读。
_BROWSER_CAPTURE_OPUS_BITRATE = 16000

# Python-side 安全兜底(2026-09-14 新增):page.evaluate 套 asyncio.wait_for,
# 防止 JS 线程死 / Chrome 标签页崩溃时 Python 无限等。JS 内 setTimeout 兜的是
# "视频不结束 + DRMed stream NaN duration" 场景;Python 这层兜的是更狠的
# "浏览器死了" 场景 — JS 引擎都不在了,setTimeout 不会触发,只能 Python 切。
#
# 默认 1800s(30 分钟),作为 capture wall-clock 硬上限。**含义**:
#   - caller 传 max_duration_sec=1800 + 默认 safety = capture 最长 1800s(正常)
#   - caller 传 max_duration_sec=7200(2 小时)+ 默认 safety = capture 实际封顶 1800s
#     → 这正是"防 max_duration 误传太大"的设计意图;故意要长视频请改这里
#   - 触发时直接 RuntimeError,不重试(走 main_provider 的"m3u8 失败 → 跳过视频"分支)
_BROWSER_CAPTURE_SAFETY_TIMEOUT_SEC = 1800

# JS that runs inside the navigated page: force-play the <video>, captureStream() its
# audio track, MediaRecorder → webm/opus chunks → base64 to Python. Awaits either
# "ended" event or maxDurationSec timeout (whichever first).
_BROWSER_CAPTURE_JS = """
async ({maxDurationSec, playbackRate, opusBitrate, disableCapture, mute}) => {
    const video = document.querySelector("video");
    if (!video) throw new Error("No <video> element");

    // Phase 9.6.4+ (2026-09-10):学完状态(capture JS 第一次被调用时,video 可能已 ended)
    // 重置 currentTime 到 0,否则 addEventListener("ended") 永远不触发(事件已发过),
    // capture JS 会一直等 setTimeout(15min)。seekTo(0) 强制从头播放。
    if (video.ended || (video.duration && video.currentTime >= video.duration - 1)) {
        try { video.currentTime = 0; } catch(e) { /* seek 失败也继续 */ }
    }

    // 2026-09-14 静音播放(默认 True):watch 模式 + 真实 capture 都默认静音。
    // 影响:
    //   - 浏览器音频输出静音,不会突然响(打扰用户)
    //   - captureStream() **仍拿到音频数据**(mute 只影响 audio output,不影响
    //     MediaStream source),所以 MediaRecorder 录到的内容不变
    //   - 自动播放更稳(浏览器对 muted autoplay 比 unmuted 更友好,无需用户手势)
    video.muted = mute;
    // 2026-09-14:playbackRate 改为默认 1x(原速播放),MediaRecorder 拿到原始采样率
    // + 原始时间轴的音频,whisper 友好。
    // (FR-2.30 实测 score 35 vs m3u8 直抽 92),所以此处不压缩时间轴。
    video.playbackRate = playbackRate;
    try { await video.play(); } catch(e) { /* autoplay restricted; will still record if stream is live */ }

    // Wait until we have current data (HAVE_CURRENT_DATA = 2) so stream isn't silent.
    await new Promise((resolve, reject) => {
        if (video.readyState >= 2) return resolve();
        const onReady = () => { video.removeEventListener("canplay", onReady); resolve(); };
        video.addEventListener("canplay", onReady);
        setTimeout(() => reject(new Error("video readyState timeout")), 10000);
    });

    // 2026-09-14:JS 侧读取 video.duration 当 setTimeout 上限(替代硬编码 maxDurationSec)。
    // 之前 setTimeout 用的就是 maxDurationSec / playbackRate,DRM 视频时长已知却仍被短
    // 上限截断(典型场景:catalog 默认 --max-sec=30,但视频实际 5min,JS 30s 就退出)。
    // 修复:用 min(video.duration, maxDurationSec) 当 setTimeout bound,前者让视频自然
    // 播完,后者保留用户硬限速(短测试时仍可截断)。DRM HLS 流 duration 可能 NaN /
    // Infinity → isFinite 兜底 1800s(=_BROWSER_CAPTURE_MAX_DURATION_SEC,防止
    // setTimeout(NaN) 永远不触发把 JS 挂死)。
    const rawDur = isFinite(video.duration) && video.duration > 0
        ? Math.ceil(video.duration) : 1800;
    const videoDuration = rawDur;
    const waitMs = Math.min(videoDuration, maxDurationSec) * 1000 / playbackRate;

    // 2026-09-14 spike(debug 旋钮):disableCapture=true → 跳过 MediaRecorder 全流程,
    // 只测超时等待(JS 端 setTimeout / video.ended vs Python 端 asyncio.wait_for)。
    // 默认 false,生产路径不变。
    if (!disableCapture) {
        const stream = video.captureStream();
        const audioTracks = stream.getAudioTracks();
        if (audioTracks.length === 0) throw new Error("No audio track");
        const audioStream = new MediaStream(audioTracks);

        // 2026-09-14:audioBitsPerSecond 从 payload opusBitrate 读取,默认 16000 (16kbps)。
        // opus 下限偏上,语音仍可懂但体积省 4x(对比默认 ~64kbps)。用于测"最低画质"耗时/性能。
        const recorder = new MediaRecorder(audioStream, {
            mimeType: "audio/webm;codecs=opus",
            audioBitsPerSecond: opusBitrate,
        });
        window.__vlaChunks = [];
        recorder.ondataavailable = e => { if (e.data.size > 0) window.__vlaChunks.push(e.data); };
        recorder.start(1000);  // 1s chunk granularity
        window.__vlaStartTime = Date.now();

        await new Promise((resolve) => {
            const onEnded = () => resolve();
            video.addEventListener("ended", onEnded, { once: true });
            // 2026-09-14:用 waitMs(video.duration 与 maxDurationSec 的较小者)替代硬编码
            // maxDurationSec,保证视频能自然播完,不被误传短 maxDurationSec 截断。
            setTimeout(resolve, waitMs);
        });

        recorder.stop();
        await new Promise(r => { recorder.onstop = r; });

        const blob = new Blob(window.__vlaChunks, { type: "audio/webm" });
        const buf = await blob.arrayBuffer();
        const u8 = new Uint8Array(buf);
        let bin = "";
        for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
        return { b64: btoa(bin), duration: (Date.now() - window.__vlaStartTime) / 1000, video_duration: videoDuration };
    }

    // disableCapture 路径:不录,只跑超时等待(同样的 ended-or-setTimeout 竞速)
    window.__vlaStartTime = Date.now();
    await new Promise((resolve) => {
        const onEnded = () => resolve();
        video.addEventListener("ended", onEnded, { once: true });
        // 2026-09-14:同上,waitMs 用 min(video.duration, maxDurationSec),不要硬编码
        setTimeout(resolve, waitMs);
    });
    return { b64: "", duration: (Date.now() - window.__vlaStartTime) / 1000, video_duration: videoDuration };
}
"""


async def extract_browser_audio(
    video_url: str,
    output_path: Path,
    *,
    cdp_url: str = "http://localhost:9222",
    max_duration_sec: int = _BROWSER_CAPTURE_MAX_DURATION_SEC,
    playback_rate: float = _BROWSER_CAPTURE_PLAYBACK_RATE,
    disable_capture: bool = False,
    mute: bool = True,
) -> Path:
    """浏览器内 MediaRecorder 抽音 → webm → ffmpeg → wav。

    用于 yunxuetang (b-learning.bill-jc.com) BCE DRM-encrypted m3u8 兜底:**2026-09-14
    后 main_provider.py 不再调用本函数**(m3u8 失败 → 直接跳过);保留供测试 1x +
    最低 opus bitrate 的耗时/性能。

    流程:
      1. async_playwright + chromium.connect_over_cdp(cdp_url) 借用户主 Chrome
      2. context.new_page() 新建播放页(不污染用户当前 tab)
      3. **SPA 重置**:先 goto bill-jc 根 URL 让 SPA 初始化(否则 new_page 后的空白
         tab 默认 route 不是视频学习页,button.yxtf-button--primary 永远不渲染)
      4. goto video_url → 等 button → 点"开始学习" → 等 <video> ready →
         playbackRate 加速 → MediaRecorder 录 webm/opus(audioBitsPerSecond = 16kbps)
      5. base64 传回 Python → 临时 webm → ffmpeg -vn -ac 1 -ar 16000 转 wav
         (**2026-09-14 移除 atempo**,1x 抓的音不需要拉伸回 2x)
      6. unlink webm(磁盘友好), close page

    Args:
        video_url: yunxuetang 播放页 URL(已带 kngId / token)
        output_path: 目标 wav 路径(需 .wav 后缀)
        cdp_url: Chrome CDP 端点(默认 localhost:9222)
        max_duration_sec: 视频时长上限(秒,video-time;非 wall-clock);超过兜底停录
        playback_rate: HTMLMediaElement.playbackRate,**默认 1.0**(2026-09-14 由 4.0
            改为 1.0,原速播放,whisper 友好)。MediaRecorder 拿原始采样率音频,
            浏览器内部 resample。opus bitrate 见模块常量 ``_BROWSER_CAPTURE_OPUS_BITRATE``
            (默认 16000 = 16kbps,2026-09-14 新增,用于测试"最低画质")。
        disable_capture: True → 跳过 MediaRecorder 抓音,只测超时等待
            (2026-09-14 spike 用,生产默认 False)。
        mute: True → ``video.muted = true`` 静音播放(2026-09-14 加,默认 True)。
            只影响 audio output,**不影响** captureStream()(仍抓到原音频)→
            MediaRecorder 录到的内容不变。好处是用户不会被突然的视频声音打扰,
            且 muted autoplay 比 unmuted 更稳(浏览器无需 user gesture)。

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
                # 2026-09-14:套 asyncio.wait_for 兜底,防止 Chrome 标签页崩溃 / OOM 时
                # JS 线程死、setTimeout 也不触发 → Python 无限等。safety 触发时直接抛
                # RuntimeError,触发者走 main_provider "m3u8 失败 → 跳过" 分支。
                result = await asyncio.wait_for(
                    page.evaluate(
                        _BROWSER_CAPTURE_JS,
                        {
                            "maxDurationSec": max_duration_sec,
                            "playbackRate": playback_rate,
                            "opusBitrate": _BROWSER_CAPTURE_OPUS_BITRATE,
                            "disableCapture": disable_capture,
                            "mute": mute,
                        },
                    ),
                    timeout=_BROWSER_CAPTURE_SAFETY_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                # safety timeout 触发 — JS 引擎死了 / 浏览器崩了。
                # finally 仍会跑(关 page + 清 webm),然后异常往外冒。
                raise RuntimeError(
                    f"browser audio capture safety timeout {_BROWSER_CAPTURE_SAFETY_TIMEOUT_SEC}s "
                    "(setTimeout + video.ended + asyncio.wait_for all failed; "
                    "possible Chrome tab crash / OOM)"
                )
            except Exception as e:
                raise RuntimeError(f"browser audio capture failed: {e}") from e

            # 2026-09-14 spike(debug 旋钮):disable_capture=True → 跳过 b64 解码 + webm
            # 落盘 + ffmpeg 转 wav,只测超时等待。output_path 不被创建(spike 自己判断)。
            # 返回 output_path 让调用方签名兼容,但文件**不**存在 — 调用方应检查。
            if disable_capture:
                # 2026-09-14:日志加 video_duration 字段,让 watch 模式能区分:
                #   - video_duration < wall-clock / playbackRate → 视频自然 ended,
                #     JS wall-clock 接近真实 duration(略大是因为 setTimeout 仍挂在事件循环)
                #   - video_duration ≈ max_duration_sec → JS 等到上限被截
                #   - video_duration < wall-clock / playbackRate < max_duration_sec →
                #     视频结束时间晚于 wall-clock,异常(几乎不该发生)
                logger.info(
                    "[NO-CAPTURE] skip b64 decode / webm / ffmpeg,"
                    " wall-clock=%.2fs reported_by_js, video_duration=%s",
                    result.get("duration", 0.0) if result else 0.0,
                    f"{result.get('video_duration')}s" if result and result.get("video_duration") is not None else "unknown",
                )
                return output_path

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
                    # 2026-09-14:1x 原速播放后,webm 音频时间轴未压缩,无需 atempo 拉伸。
                    # 之前 Phase 9.6.6 (2026-09-10) 用的 `-af atempo=0.5` 已删除。
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
