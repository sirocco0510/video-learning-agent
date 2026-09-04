"""Tab Audio Recorder 触发器 (SSOT: spec §3.1, FR-2.21/2.24/2.24a/2.25).

设计要点:
- 无状态:调用方每进程创建一个 TabAudioRecorder(cfg) 实例,不做模块级单例(FR-2.21)。
- 所有方法 async,匹配 playwright 异步 API。
- probe_status 防御性:任何 chrome.management.getAll 异常 → 返回 "not_installed",
  永不向调用方抛错(主流程不中断,FR-2.21 降级语义)。
- 不需要 macOS TCC 屏幕录制权限(Tab Audio Recorder 用 chrome.tabCapture,
  不走 navigator.mediaDevices.getUserMedia)。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ---- 异常类型 ----


class ExtensionNotFoundError(Exception):
    """Tab Audio Recorder 扩展未在 chrome.management.getAll() 中匹配到。

    _resolve_ext_id 在找不到匹配扩展时抛出,SubtitleStrategy 捕获后
    写 quality_skip.csv(FR-2.21 降级)。
    """


class RecorderTriggerError(Exception):
    """扩展触发录制失败:bg page evaluate 异常 / 跳转 editor.html 超时。

    主调度降级到 quality_skip(FR-2.21),不记 transcribe_fail
    (Whisper 还没启动)。
    """


class DownloadTimeoutError(Exception):
    """click_download 在 timeout_sec 内未收到 download 事件。

    文件留在 audio_failed/,供排查(FR-2.25 + FR-2.22 失败归档)。
    """


# ---- 主类 ----


# 用于从 editor.html URL 提取 audio_id 的正则(FR-2.24 实现细节)
_EDITOR_URL_PATTERN = re.compile(r"editor\.html\?id=(\d+)")


# 探测时执行的 JS:在 background page 里调用 chrome.management.getAll()
# 并把结果 resolve 出来。该 API 是浏览器级,不需要页面焦点。
_PROBE_GET_ALL_JS = """
async () => {
    return new Promise((resolve, reject) => {
        if (typeof chrome === 'undefined' || !chrome.management) {
            reject(new Error('chrome.management unavailable'));
            return;
        }
        chrome.management.getAll((extensions) => resolve(extensions));
    });
}
"""


class TabAudioRecorder:
    """Tab Audio Recorder 触发器 (SSOT: spec §3.1).

    Public methods:
        probe_status: 三态探测 enabled / disabled / not_installed
        start_recording: 触发扩展开始录制,返回 audio_id
        click_download: 在 editor.html 上点下载按钮,落盘到 save_dir

    Internal:
        _resolve_ext_id: 从 chrome.management.getAll() 动态匹配扩展 ID
                         (不硬编码 hanfcigjijjcbdbfoplddndcblmlfiio)
    """

    def __init__(
        self,
        match_keyword: str = "tab audio",
        save_dir: Path = Path("./logs/audio_raw"),
        match_timeout_sec: float = 5.0,
    ) -> None:
        """Args:
            match_keyword: 扩展名/描述的匹配关键词(默认 "tab audio"),
                           可从 cfg.extension.tab_audio_recorder.match_keyword 注入
            save_dir: 音频落盘目录(Path;mkdir 在调用方负责或 click_download 内兜底)
            match_timeout_sec: probe_status 超时阈值(秒);默认 5.0
        """
        self.match_keyword = match_keyword.lower()
        self.save_dir = Path(save_dir)
        self.match_timeout_sec = match_timeout_sec

    # ---- 探测 + 解析扩展 ID ----

    async def probe_status(
        self, browser: Any
    ) -> Literal["enabled", "disabled", "not_installed"]:
        """无状态探测 (FR-2.24a): 每次调用即时查 chrome.management.getAll()。

        Returns:
            "enabled" — 找到扩展且 enabled=True
            "disabled" — 找到扩展但 enabled=False
            "not_installed" — 未找到 / chrome.management 不可用 / 异常 / 超时

        失败(任何异常)→ 防御性返回 "not_installed",永不向调用方抛错。
        """
        try:
            extensions = await asyncio.wait_for(
                browser.evaluate(_PROBE_GET_ALL_JS),
                timeout=self.match_timeout_sec,
            )
        except Exception as e:
            logger.warning("⚠️ probe_status 异常,降级为 not_installed: %s", e)
            return "not_installed"

        if not isinstance(extensions, list):
            return "not_installed"

        for ext in extensions:
            if not isinstance(ext, dict):
                continue
            name = (ext.get("name") or "").lower()
            desc = (ext.get("description") or "").lower()
            if self.match_keyword in name or self.match_keyword in desc:
                return "enabled" if ext.get("enabled") else "disabled"

        return "not_installed"

    async def _resolve_ext_id(self, browser: Any) -> str:
        """从 chrome.management.getAll() 匹配 name/description.contains(match_keyword)。

        找不到匹配的扩展 → raise ExtensionNotFoundError(让上层走 quality_skip)。

        Returns:
            扩展 ID 字符串(Chrome 扩展唯一 ID,32 字符)。
        """
        try:
            extensions = await asyncio.wait_for(
                browser.evaluate(_PROBE_GET_ALL_JS),
                timeout=self.match_timeout_sec,
            )
        except Exception as e:
            raise ExtensionNotFoundError(
                f"chrome.management.getAll 失败: {e}"
            ) from e

        if not isinstance(extensions, list):
            raise ExtensionNotFoundError("chrome.management.getAll 返回非列表")

        for ext in extensions:
            if not isinstance(ext, dict):
                continue
            name = (ext.get("name") or "").lower()
            desc = (ext.get("description") or "").lower()
            if self.match_keyword in name or self.match_keyword in desc:
                ext_id = ext.get("id")
                if isinstance(ext_id, str) and ext_id:
                    return ext_id

        raise ExtensionNotFoundError(
            f"未找到匹配 match_keyword='{self.match_keyword}' 的扩展"
        )

    # ---- 触发录制 ----

    async def start_recording(
        self,
        driver: Any,
        url: str,
        duration_sec: int,
        post_buffer_sec: int = 30,
    ) -> str:
        """FR-2.24: 触发扩展开始录制,轮询 bg_page.url 直到 editor.html?id=<audio_id>。

        步骤(spec §3.1):
            1. _resolve_ext_id(driver) → ext_id (NO hardcode)
            2. 找到扩展 background page(driver.targets() 中 url 同时含
               chrome-extension://<ext_id>/ 和 _generated_background_page.html)
            3. 在 bg page 上 evaluate `startTabRecording()`(扩展内部暴露的全局函数)
            4. 轮询 bg_page.url 直到匹配 editor.html?id=<digits>
            5. 正则提取 audio_id 返回

        Args:
            driver: playwright Driver(提供 targets() + evaluate() 方法)
            url: 视频 URL(供扩展抓取 tab 音频;此函数不直接使用,扩展自己读 tab)
            duration_sec: 录制时长(秒);扩展自己计时 stop,我们等 duration+post_buffer
            post_buffer_sec: 额外等待扩展完成编码的时间(默认 30s)

        Returns:
            audio_id(扩展分配的纯数字字符串,来自 editor.html URL ?id= 参数)

        Raises:
            ExtensionNotFoundError: _resolve_ext_id 没找到匹配扩展
                                    (spec §3.1 line 113,FR-2.21 quality_skip)
            RecorderTriggerError: 找到 ext_id 但找不到 bg page / evaluate 失败 / 跳转超时
        """
        # 步骤 1: 动态解析 ext_id(spec §3.1 line 113:不硬编码)。
        # 找不到扩展 → ExtensionNotFoundError 自然抛出,
        # 上层 SubtitleStrategy 捕获后写 quality_skip.csv(FR-2.21 降级)。
        ext_id = await self._resolve_ext_id(driver)

        # 步骤 2: 找 background page(targets 中 url 同时含
        # chrome-extension://<ext_id>/ 与 _generated_background_page.html)。
        # 双重匹配避免选错扩展的 bg page(多个 chrome-extension 同时打开时)。
        bg_page: Any = None
        try:
            targets = await driver.targets() if hasattr(driver, "targets") else []
        except Exception as e:
            raise RecorderTriggerError(f"driver.targets() 失败: {e}") from e

        ext_prefix = f"chrome-extension://{ext_id}/"
        for t in targets:
            t_url = getattr(t, "url", "") or ""
            if ext_prefix in t_url and "_generated_background_page.html" in t_url:
                bg_page = t
                break

        if bg_page is None:
            raise RecorderTriggerError(
                f"未找到扩展 {ext_id} 的 background page;"
                "请先在 Chrome 启用 Tab Audio Recorder 并打开一次"
            )

        # 步骤 3: 启动录制(扩展暴露 startTabRecording() 或等价函数)
        # 用 try/except 防御:扩展 API 可能改名,降级到 RecorderTriggerError
        try:
            await bg_page.evaluate(
                "typeof startTabRecording === 'function' ? startTabRecording() : null"
            )
        except Exception as e:
            raise RecorderTriggerError(f"启动录制失败: {e}") from e

        # 步骤 4 + 5: 轮询 bg_page.url 直到匹配 editor.html?id=<digits>
        # 总等待时长 = duration_sec + post_buffer_sec
        # (扩展自己 stop + 编码需要 buffer,FR-2.15 后置 buffer)
        # 用 polling 而不是单次 sleep,提高响应速度
        loop = asyncio.get_event_loop()
        deadline = loop.time() + duration_sec + post_buffer_sec
        poll_interval = 0.5

        while loop.time() < deadline:
            current_url = getattr(bg_page, "url", "") or ""
            match = _EDITOR_URL_PATTERN.search(current_url)
            if match:
                return match.group(1)
            await asyncio.sleep(poll_interval)

        raise RecorderTriggerError(
            f"等待 editor.html 跳转超时 ({duration_sec + post_buffer_sec}s);"
            f"最后 url={getattr(bg_page, 'url', '?')}"
        )

    # ---- 落盘下载 ----

    async def click_download(
        self,
        driver: Any,
        audio_id: str,
        ext_id: str,
        save_dir: Path | None = None,
        timeout_sec: int = 180,
    ) -> Path:
        """FR-2.25: 直接 goto editor.html?id=<audio_id>,注册 download 监听,点下载按钮。

        关键顺序:必须 page.on("download") 先注册,再点按钮(否则事件丢失)。

        步骤:
            1. save_dir 兜底: None → self.save_dir(创建 mkdir(parents=True, exist_ok=True))
            2. context.on("download", handler) 注册监听(关键:先注册)
            3. page.goto(chrome-extension://<ext_id>/editor.html?id=<audio_id>)
            4. 在 editor.html 内 evaluate 找下载按钮,点击
               (候选 selector: button:has-text("Download"), button:has-text("保存"),
                #download-btn, [data-action="download"])
            5. 等 download 事件触发,download.save_as(<save_dir>/<audio_id>.webm)
            6. 超时 → raise DownloadTimeoutError

        Args:
            driver: playwright Driver(提供 context / new_page / goto / click / evaluate)
            audio_id: 扩展分配的音频 ID(来自 editor.html ?id= 参数)
            ext_id: 扩展 ID(由调用方从 _resolve_ext_id 获取,避免重复探测)
            save_dir: 落盘目录;None → self.save_dir
            timeout_sec: download 事件等待超时(秒;默认 180)

        Returns:
            落盘后的音频文件路径(save_dir / f"{audio_id}.webm",FR-2.26 命名规范)

        Raises:
            DownloadTimeoutError: timeout_sec 内未收到 download 事件
        """
        target_dir = Path(save_dir) if save_dir is not None else self.save_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{audio_id}.webm"

        # 拿到 context(playwright Driver 的 context)
        ctx = getattr(driver, "context", driver)

        # 步骤 2: 先注册 download 监听(关键!)
        loop = asyncio.get_event_loop()
        download_future: asyncio.Future[Any] = loop.create_future()

        def on_download(dl: Any) -> None:
            if not download_future.done():
                download_future.set_result(dl)

        ctx.on("download", on_download)

        # 步骤 3: 打开 editor.html
        editor_url = f"chrome-extension://{ext_id}/editor.html?id={audio_id}"
        page = ctx.new_page()
        try:
            await page.goto(editor_url)
        except Exception as e:
            raise DownloadTimeoutError(f"goto editor.html 失败: {e}") from e

        # 步骤 4: 找下载按钮并点击
        # 候选 selector 按优先级尝试,首个点击成功即可
        download_selectors = [
            'button:has-text("Download")',
            'button:has-text("保存")',
            "#download-btn",
            '[data-action="download"]',
        ]
        clicked = False
        for selector in download_selectors:
            try:
                await page.click(selector, timeout=2000)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            raise DownloadTimeoutError(
                f"editor.html 中未找到下载按钮(已尝试 {len(download_selectors)} 个 selector)"
            )

        # 步骤 5: 等 download 事件
        try:
            download = await asyncio.wait_for(download_future, timeout=timeout_sec)
        except asyncio.TimeoutError as e:
            raise DownloadTimeoutError(
                f"等待 download 事件超时 ({timeout_sec}s)"
            ) from e

        await download.save_as(target_path)
        return target_path
