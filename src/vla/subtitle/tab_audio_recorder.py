"""Tab Audio Recorder 探测器 (SSOT: spec §3.1, FR-2.24a)。

F2-10 (2026-09-08) 退化:Tab Audio Recorder 不再由代码自动触发(用户手动按
Cmd+Shift+R + 手动点 downloadWavBtn),代码只保留 `probe_status` 探测扩展
是否安装(给 `plugin_status.mark_unavailable` 用)。

设计要点:
- 无状态:调用方每进程创建一个 TabAudioRecorder(cfg) 实例,不做模块级单例(FR-2.21)。
- 所有方法 async,匹配 playwright 异步 API。
- probe_status 防御性:任何 chrome.management.getAll 异常 → 返回 "not_installed",
  永不向调用方抛错(主流程不中断,FR-2.21 降级语义)。
- 不需要 macOS TCC 屏幕录制权限(Tab Audio Recorder 用 chrome.tabCapture,
  不走 navigator.mediaDevices.getUserMedia)。

v3.2.1.5 探测路径切换:不再调 `chrome.management.getAll()`(MV3 service_worker 下不可用),
改走 **CDP `getTargets` 枚举 chrome-extension service_worker + manifest.json 读 name**。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ---- 主类 ----


# 从 chrome-extension:// URL 提取 ext_id(32 字符 a-p)
_EXT_ID_RE = re.compile(r"chrome-extension://([a-p]{32})/")


# v3.2.1.5: 在 manifest 页面里读 name/description
# chrome-extension://<id>/manifest.json 在浏览器中渲染为纯文本,JSON.parse 即可。
_READ_MANIFEST_JS = "() => document.body.innerText"


class TabAudioRecorder:
    """Tab Audio Recorder 探测器 (SSOT: spec §3.1, FR-2.24a)。

    F2-10 (2026-09-08) 退化:start_recording / click_download / _resolve_ext_id
    已删(用户手动按 Cmd+Shift+R + 手动点 downloadWavBtn)。
    只保留 `probe_status` 探测扩展是否安装(给 plugin_status.mark_unavailable 用)。

    Public methods:
        probe_status: 三态探测 enabled / disabled / not_installed
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
            save_dir: 历史字段(F2-10 后仅 probe_status,不再写入,但保留以兼容旧构造方)
            match_timeout_sec: probe_status 超时阈值(秒);默认 5.0
        """
        self.match_keyword = match_keyword.lower()
        self.save_dir = Path(save_dir)
        self.match_timeout_sec = match_timeout_sec

    # ---- 探测 + 解析扩展 ID ----

    async def probe_status(
        self, driver: Any
    ) -> Literal["enabled", "disabled", "not_installed"]:
        """无状态探测 (FR-2.24a)。

        v3.2.1.5 root cause: 旧实现用 `chrome.management.getAll()`。该 API 只在
        扩展页面(及弹窗)里可用,**普通 web 页面**上是 `undefined`,所以旧的
        `_PROBE_GET_ALL_JS` 在 B站视频页上永远 reject → 路径②永远标记
        not_installed。MV3 之后扩展普遍走 service_worker,旧版本还硬匹配
        `_generated_background_page.html`,也匹配不到。

        修法: 不依赖扩展 API,直接走 **CDP targets 列举 service_worker + manifest.json**:
          1. `await driver.targets()` → 所有 CDP 目标
          2. 过滤 `chrome-extension://<id>/` 的 type=service_worker
          3. 对每个 ext_id 创建 background page → goto `chrome-extension://<id>/manifest.json`
          4. `page.evaluate("() => JSON.parse(document.body.innerText)")` 读 name/description
          5. 名字或描述 contains `match_keyword` → 命中,直接返回 enabled
             (MV3 启用状态通过 `service_worker` 存在性已隐式表达,MV2 同理)

        Args:
            driver: BrowserDriver 实例(必须有 .targets() 和 .arun_on_driver_thread())。
                    不再接受裸 browser — driver 是唯一可靠的桥。

        Returns:
            "enabled" — 找到匹配 match_keyword 的扩展
            "not_installed" — 没有任何 chrome-extension target / 都不匹配 /
                              任何步骤异常 / 超时

        失败(任何异常)→ 防御性返回 "not_installed",永不向调用方抛错(FR-2.21 降级)。
        """
        try:
            # 1. 拿 CDP 目标列表
            if not hasattr(driver, "targets"):
                logger.warning("⚠️ probe_status: driver 没有 targets(),降级 not_installed")
                return "not_installed"
            try:
                targets = await driver.targets()
            except Exception as e:
                logger.warning("⚠️ probe_status: driver.targets() 失败: %s", e)
                return "not_installed"

            ext_ids: set[str] = set()
            for t in targets or []:
                t_url = getattr(t, "url", "") or ""
                # type=service_worker (MV3) 或 type=page (MV2 background page)
                t_type = getattr(t, "type", "") or ""
                m = _EXT_ID_RE.search(t_url)
                if m and t_type in ("service_worker", "page"):
                    ext_ids.add(m.group(1))

            if not ext_ids:
                return "not_installed"

            # 2. 读每个 manifest.json,看 name/description 命中 match_keyword
            status, _ext_id = await asyncio.wait_for(
                self._scan_manifests(driver, ext_ids),
                timeout=self.match_timeout_sec,
            )
            return status
        except asyncio.TimeoutError:
            logger.warning("⚠️ probe_status 超时(%.1fs),降级 not_installed",
                           self.match_timeout_sec)
            return "not_installed"
        except Exception as e:
            logger.warning("⚠️ probe_status 异常,降级为 not_installed: %s", e)
            return "not_installed"

    async def _scan_manifests(
        self, driver: Any, ext_ids: set[str]
    ) -> tuple[Literal["enabled", "not_installed"], str | None]:
        """对每个 ext_id,后台 page goto manifest.json → evaluate JSON.parse。

        v3.2.1.5 + v3.2.1.7:返回 (status, ext_id) 而不只是 status,
        让 _resolve_ext_id 复用同一逻辑(不再走旧的 chrome.management.getAll)。
        """
        for ext_id in ext_ids:
            try:
                page = await driver.arun_on_driver_thread(
                    driver.new_background_page
                )
                if page is None:
                    continue
                await driver.arun_on_driver_thread(
                    page.goto,
                    f"chrome-extension://{ext_id}/manifest.json",
                    timeout=10000,
                )
                raw = await driver.arun_on_driver_thread(
                    page.evaluate, _READ_MANIFEST_JS
                )
                # 关闭 page 避免堆积 popup(manifest 页面也是 chrome-extension://)
                try:
                    await driver.arun_on_driver_thread(page.close)
                except Exception:
                    pass
                if not isinstance(raw, str):
                    continue
                try:
                    manifest = json.loads(raw)
                except Exception:
                    continue
                name = (manifest.get("name") or "").lower()
                desc = (manifest.get("description") or "").lower()
                if self.match_keyword in name or self.match_keyword in desc:
                    return "enabled", ext_id
            except Exception as e:
                logger.debug("probe_status scan %s 失败: %s", ext_id[:12], e)
                continue
        return "not_installed", None

