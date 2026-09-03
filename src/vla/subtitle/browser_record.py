"""BrowserRecorder(SSOT: requirements.md FR-2.14/2.15/2.16/2.22 + implementation-plan.md Phase 3.2)。

职责:
- 通过 Chrome 扩展 Screen Recorder / Screencastify 录屏(自带抽音,无需 ffmpeg)
- 监听下载事件拿到扩展输出的视频文件
- 委托给 AudioTranscriber 接口(Phase 4 实现 faster-whisper)转写
- 磁盘友好: 转写完删除源视频文件

设计:
- AudioTranscriber Protocol(duck typing)→ Phase 4 WhisperTranscriber 实现
- MacOSNotifier 注入(可选,默认 None = 静默);Phase 6 真正发 osascript 通知,
  Phase 3 阶段是 stub no-op,所以即使不注入也不影响功能
- BrowserRecorder 注入 transcriber + notifier,自身不依赖 faster-whisper / macos_notify
- 录屏触发: 按 hotkey 启动(macOS 上 Screencastify 用 Alt+Shift+R 真 toggle)
- 录屏时长: duration_sec 传视频长度;recorder 不主动 stop(让用户在视频播完后手动)
  - 如果 hotkey_stop=True,会再按一次 hotkey 尝试 stop(macOS 上 Screencastify toggle 工作)
- 录屏前 grace: pre_grace_sec 在按 hotkey 后、阻塞 duration_sec 前等待,
  给用户时间在真实 Chrome 里手动按对应热键
- 下载等待: 轮询,超时由 config.browser_plugin.record_download_timeout_sec 控制
  - Screencastify 录完后自动跳新标签页,需要用户在编辑页点 btn-download,
    所以 download_timeout 要给用户留足时间(>=180s,30min 长视频考虑)

UX 提醒(B级,可选):
- start: notifier.info("录屏启动", "请在 Chrome 按 {hotkey} 启动录屏(预计 {duration}秒)")
- stop:  notifier.warning("录屏到时", "请在编辑标签页点 btn-download(超时 {timeout}s)")
- timeout: notifier.warning("录屏超时", "{url} 未在 {timeout}s 内收到文件,跳过")

典型工作流(macOS + Screencastify):
1. BrowserRecorder 按 Alt+Shift+R 启动录屏(CDP no-op,user-driven)
2. 等待 pre_grace_sec 秒(给用户时间在真实 Chrome 按对应热键)
3. 视频播放 duration_sec 秒
4. (可选)BrowserRecorder 按 Alt+Shift+R 尝试停止
5. notifier.warning 通知用户:"请在编辑标签页点 btn-download"
6. 用户在编辑页点 btn-download
7. Chrome download 事件触发 → BrowserRecorder 捕获 → save_as → transcriber
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from vla.config import VLAConfig
from vla.subtitle.page_control import pause_page_video
from vla.subtitle.probe_strategy import (
    ProbeContext,
    ProbeRegistry,
    ProbeResult,
)
from vla.ui.macos_notify import MacOSNotifier


logger = logging.getLogger(__name__)


def _safe_wait(page: object, ms: int) -> None:
    """page.wait_for_timeout 包装:page 被关掉(Chrome memory saver / popup 副作用 /
    扩展弹窗抢焦点)就 fall back 到 time.sleep,不依赖 page 存活。

    为什么需要:Chrome 在某些场景会自动 unload 后台页面(尤其独立 user-data-dir
    调试模式 + 无交互长时间 idle),page.wait_for_timeout 会抛
    "Target page, context or browser has been closed" — 但录屏本身仍在 Chrome
    里跑(用户手动按 hotkey + 下载),只是我们的同步控制流断了。
    真实 Chrome 窗口活动时不会触发(主流程上无影响,只是兜底)。
    """
    try:
        page.wait_for_timeout(ms)  # type: ignore[attr-defined]
    except Exception as e:
        msg = str(e)
        if "has been closed" in msg or "Target" in msg:
            logger.warning(
                "page.wait_for_timeout(%dms) 失败(%s),fall back 到 time.sleep — "
                "page 可能被 Chrome memory saver unload,录屏仍在继续",
                ms, msg[:80],
            )
            time.sleep(ms / 1000)
        else:
            raise


def _to_playwright_hotkey(hotkey: str) -> str:
    """把用户友好的 hotkey 翻译成 playwright 接受的格式。

    playwright 的 page.keyboard.press 不接受 "Command" / "Cmd"(会抛
    "Unknown key: 'Command'");macOS 上要用 "Meta"。其他修饰键照旧:
    Shift / Alt / Control。

    输入样例:
        "Command+Shift+R" → "Meta+Shift+R"
        "Cmd+Shift+R"     → "Meta+Shift+R"
        "Meta+Shift+R"    → "Meta+Shift+R"(幂等)
        "Alt+Shift+R"     → "Alt+Shift+R"(不变)

    用户在 chrome://extensions/shortcuts 里配的也是 "Command+Shift+R"
    (macOS 习惯),这里转换只是给 playwright 用。**通知文案仍展示原值**,
    避免用户混淆("我在系统设置里配的是 Cmd,为啥让我按 Meta?")。
    """
    parts = [p.strip() for p in hotkey.split("+")]
    translated = []
    for p in parts:
        low = p.lower()
        if low in ("command", "cmd", "commandorcontrol", "coc"):
            translated.append("Meta")
        else:
            translated.append(p)
    return "+".join(translated)


@runtime_checkable
class AudioTranscriber(Protocol):
    """音频转写接口(duck typing)。

    Phase 4 由 WhisperTranscriber 实现(faster-whisper);现在 FakeTranscriber 用于测试。
    """

    def transcribe(self, audio_path: Path) -> str:
        """接收视频/音频文件路径,返回字幕文本。"""
        ...


class BrowserRecorder:
    """录屏 + 监听下载 + 委托转写。"""

    def __init__(
        self,
        config: VLAConfig,
        transcriber: AudioTranscriber,
        notifier: MacOSNotifier | None = None,
        poll_interval_ms: int = 1000,
        probe_registry: ProbeRegistry | None = None,
    ) -> None:
        self.config = config
        self.transcriber = transcriber
        self.notifier = notifier
        self.hotkey = config.browser_plugin.record_hotkey
        self._timeout_sec = config.browser_plugin.record_download_timeout_sec
        self._pre_grace_sec = config.browser_plugin.record_pre_grace_sec
        self._post_buffer_sec = config.browser_plugin.record_post_buffer_sec
        self._poll_interval_ms = poll_interval_ms
        # 探针注册表(R-14):默认空 registry,保持向后兼容;
        # 上线时通过 default_probe_registry() 注入 Head/Referer/Cookie 三件套。
        self.probe_registry: ProbeRegistry = (
            probe_registry if probe_registry is not None else ProbeRegistry()
        )

    def probe(self, url: str) -> tuple[bool, list[ProbeResult]]:
        """按注册顺序跑所有 match(url) 的探针,直到任意一个 ok 或全部跑完。

        Returns:
            (ok, results) — ok=True 表示至少一个探针判定 url 可达;
            results 保留本次全部 ProbeResult 用于日志 / 调试。

        行为契约(R-14 SSOT):
        - session/page/cfg 都从 self 拿;session 留给调用方注入,
          这里只是占位(ProbeContext 需要)
        - 当前 session 暂未在 BrowserRecorder 内构造(留给 CDP 接管),
          上线时由 main_provider 注入一个 requests.Session 到 ctx.session
        """
        ctx = ProbeContext(session=None, page=None, cfg=self.config)
        results: list[ProbeResult] = []
        for strat in self.probe_registry.get_all_for(url):
            result = strat.run(url, ctx)
            logger.info(
                "probe[%s] url=%s ok=%s note=%s",
                strat.name, url[:80], result.ok, result.note,
            )
            results.append(result)
            if result.ok:
                return True, results
        return False, results

    def record_and_transcribe(
        self,
        page: object,
        url: str,
        duration_sec: int,
        save_dir: Path,
        hotkey_stop: bool = True,
    ) -> Path:
        """录屏 + 转写,返回**转写文本落盘文件**的路径(Path) — 不在内存持有文本。

        page: playwright sync Page(需 .keyboard.press / .wait_for_timeout / .context.on)
        url: 当前播放页 URL(供日志/通知;非强制使用)
        duration_sec: 估计的视频时长(秒);实际录屏由用户手动 Stop
        save_dir: 视频 + 转写文本落盘目录
        hotkey_stop: 录制结束后是否再按一次 hotkey 停止(默认 True;
            macOS 上 Screencastify toggle 工作正常,Win/Linux 上某些扩展 toggle 不灵,
            可传 False 关闭)

        Returns:
            Path — 转写文本文件路径 `save_dir / f"{video_stem}.transcript.txt"`。
            调用方按需读取(SSOT: 用户要求"录屏转写后需要保存到文件不要存内存")。

        时序(FR-2.14/2.15 + 用户新规):
        - t=0: notifier.info("录屏启动") + 按 hotkey(CDP no-op,user-driven)
        - t=pre_grace_sec: 用户有时间在真实 Chrome 启录屏 + 点 Play
        - t=pre_grace_sec+duration_sec: 视频**估计**播完
        - t=pre_grace_sec+duration_sec+post_buffer_sec(视频长度+30s 弹性):
            notifier.warning("录屏到时") — 视频应已播完,请点 btn-download
        - t=pre_grace_sec+duration_sec+post_buffer_sec: 自动 stop hotkey + 轮询下载
        - 用户回 Chrome → Stop + Download → Chrome download 事件 → save_as → 转写 → 落盘
        """
        save_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []

        def on_download(d) -> None:
            target = save_dir / d.suggested_filename
            try:
                d.save_as(target)
                downloaded.append(target)
                logger.info("录屏文件已落盘:%s", target)
            except Exception as e:
                logger.error("保存录屏文件失败:%s", e)

        ctx = page.context  # type: ignore[attr-defined]
        # 2026-09-02 UX 改:listen 在 browser 级别(跨 context 捕获 Chrome 扩展下载)。
        # 之前用 page.context.on:Chrome 扩展在自身 context 触发下载时不会进入
        # page 所属 context 的 listener,文件直接落到 ~/Downloads/,BrowserRecorder
        # 等不到,180s 后超时。browser.on 跨所有 context 都能拦到。
        # page.context.on 仍保留(防御性双注册,有的扩展走 context 走 page)。
        ctx.on("download", on_download)
        # getattr 防御:测试用 FakeContext 可能没有 .browser
        browser = getattr(ctx, "browser", None)
        if browser is not None:
            browser.on("download", on_download)

        video_path: Path | None = None
        transcript_path: Path | None = None
        try:
            # 0. 暂停页面视频(避免 B站等页面自动播放,让用户手动控制 Play 时机)
            # 不暂停的话:video 在 page load 后就开始播,等用户手动按 hotkey 时已经播了几秒,
            # duration_sec 计时不准。pause 后用户准备好手动 Play,录屏与视频对齐。
            pause_page_video(page)

            # 1. 通知 + 启动录屏(CDP no-op,user-driven)
            if self.notifier is not None:
                self.notifier.info(
                    "录屏启动",
                    f"请在 Chrome 按 {self.hotkey} 启动录屏(预计 {duration_sec}秒)",
                )
            page.keyboard.press(_to_playwright_hotkey(self.hotkey))  # type: ignore[attr-defined]
            logger.info(
                "录屏已启动 hotkey=%s (playwright=%s) url=%s duration=%ds grace=%ds",
                self.hotkey, _to_playwright_hotkey(self.hotkey),
                url, duration_sec, self._pre_grace_sec,
            )

            # 2. 给用户时间手动按 hotkey
            if self._pre_grace_sec > 0:
                _safe_wait(page, self._pre_grace_sec * 1000)

            # 3. 录制时长(估计视频长度)
            _safe_wait(page, duration_sec * 1000)

            # 3.5 弹性 30s(用户新规:视频可能比估计略长,等到时间 = 视频长度 + 30s 弹性)
            # post_buffer **在 warning 前**:warning 触发点 = duration + post_buffer = "视频长度 + 30s 弹性"
            if self._post_buffer_sec > 0:
                logger.info(
                    "等待弹性 %ds(video 实际可能比估计略长)",
                    self._post_buffer_sec,
                )
                _safe_wait(page, self._post_buffer_sec * 1000)

            # 3.6 录屏到时 warning(此时 = duration + post_buffer,"视频长度 + 30s 弹性" 后)
            if self.notifier is not None:
                self.notifier.warning(
                    "录屏到时",
                    f"视频应已播完(预估 {duration_sec}s + {self._post_buffer_sec}s 弹性),"
                    f"请在编辑标签页点 btn-download(超时 {self._timeout_sec}s)",
                )
            logger.info(
                ">>> 录屏到时:视频应已播完(预估 %ds + %ds 弹性) <<<",
                duration_sec, self._post_buffer_sec,
            )

            # 4. 停止录屏(可选)
            if hotkey_stop:
                try:
                    page.keyboard.press(_to_playwright_hotkey(self.hotkey))  # type: ignore[attr-defined]
                    logger.info("录屏已停止,等待扩展输出文件")
                except Exception as e:
                    msg = str(e)
                    if "has been closed" in msg or "Target" in msg:
                        logger.warning(
                            "录屏停止 hotkey 跳过(page 已关闭):%s — 用户需手动 Stop",
                            msg[:80],
                        )
                    else:
                        raise

            # 5. 轮询下载(超时由 config.browser_plugin.record_download_timeout_sec 控制)
            timeout_ms = self._timeout_sec * 1000
            elapsed = 0
            while elapsed < timeout_ms:
                _safe_wait(page, self._poll_interval_ms)
                elapsed += self._poll_interval_ms
                if downloaded:
                    break

            # 5.5 软宽限(2026-09-02):超时后再等 5s,Chrome 扩展下载有时比 timeout 晚到
            # 几十秒(用户最后才点 Download)。期间继续监听,不强 raise。
            if not downloaded:
                soft_grace_ms = 5_000
                logger.info(
                    "下载超时,进入 %dms 软宽限(等用户最后点 Download)",
                    soft_grace_ms,
                )
                soft_elapsed = 0
                while soft_elapsed < soft_grace_ms:
                    _safe_wait(page, 500)
                    soft_elapsed += 500
                    if downloaded:
                        break

            # 5.6 最后兜底(2026-09-02):如果 listener 没拦到(扩展走非标准下载通道),
            # 去 ~/Downloads 找最近修改的 .webm/.mp4(本次录制时间内)挪过来。
            if not downloaded:
                recovered = self._recover_from_downloads_dir(save_dir, url)
                if recovered is not None:
                    downloaded.append(recovered)
                    logger.warning(
                        "📥 从 ~/Downloads 兜底挪文件:%s → %s",
                        recovered, save_dir,
                    )

            if not downloaded:
                if self.notifier is not None:
                    self.notifier.warning(
                        "录屏超时",
                        f"{url} 未在 {self._timeout_sec}s 内收到文件,跳过",
                    )
                raise RuntimeError(
                    f"录屏 {self._timeout_sec}s 内未生成文件 url={url};"
                    f"可能扩展未安装 / hotkey 未生效 / 用户未在编辑页点 btn-download"
                )

            video_path = downloaded[-1]

            # 7. 转写
            # 7.0 提前捕获 stem — StreamingTranscriber 可能已删 video_path
            video_stem = video_path.stem
            text = self.transcriber.transcribe(video_path)

            # 7.5 落盘到 .transcript.txt(用户新规:"录屏转写后需要保存到文件不要存内存")
            transcript_path = save_dir / f"{video_stem}.transcript.txt"
            transcript_path.write_text(text, encoding="utf-8")
            logger.info(
                "💾 转写文本已落盘:%s (%d 字符)", transcript_path, len(text),
            )
            return transcript_path

        finally:
            # 8. 移除监听(context + browser,2026-09-02 双注册)
            try:
                ctx.remove_listener("download", on_download)
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.remove_listener("download", on_download)
            except Exception:
                pass
            # 9. 磁盘友好: 删除源视频(兜底 — transcriber 可能已按 FR-3.3 删过)
            # exists() 防御: StreamingTranscriber 已在 transcribe() 内 unlink 视频源,
            # 这里只补删那些"transcriber 没管"的旧实现,避免 FileNotFoundError warning 噪音。
            if video_path is not None and video_path.exists():
                try:
                    video_path.unlink()
                    logger.info("🗑️ 兜底清理录屏源文件:%s", video_path)
                except OSError as e:
                    logger.warning("清理录屏文件失败:%s %s", video_path, e)

    # ---------------- helpers ----------------

    def _recover_from_downloads_dir(
        self, save_dir: Path, url: str,
    ) -> Path | None:
        """兜底:Chrome 扩展下载走非标准通道、listener 没拦到时,从 ~/Downloads
        找最近修改的 .webm/.mp4 挪到 save_dir(2026-09-02 UX 改)。

        匹配规则:
        - 文件后缀:.webm / .mp4(Chrome 录屏扩展常见输出)
        - 修改时间在最近 10 分钟内(避免误挪历史文件)
        - 文件名含 "Screen Recording" 或 "Screencastify"(降低误判)
          —— 拿不到精确录屏 id,用启发式

        返回挪好后的 save_dir 内文件路径;没匹配返回 None。
        """
        from datetime import datetime, timedelta

        downloads_dir = Path.home() / "Downloads"
        if not downloads_dir.exists():
            return None

        candidates = []
        now = datetime.now()
        cutoff = now - timedelta(minutes=10)
        for ext in ("*.webm", "*.mp4"):
            for p in downloads_dir.glob(ext):
                try:
                    mtime = datetime.fromtimestamp(p.stat().st_mtime)
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                name = p.name
                if "Screen Recording" in name or "Screencastify" in name:
                    candidates.append((mtime, p))

        if not candidates:
            return None

        # 选最新的(本轮 spike 大概率就一个)
        candidates.sort(key=lambda t: t[0], reverse=True)
        latest = candidates[0][1]
        target = save_dir / latest.name
        try:
            latest.rename(target)
            logger.info(
                "📥 兜底从 ~/Downloads 挪文件:%s → %s(url=%s)",
                latest, target, url[:60],
            )
            return target
        except OSError as e:
            logger.warning("兜底挪文件失败 %s → %s:%s", latest, target, e)
            return None