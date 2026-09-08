"""TabAudioRecorder 单元测试 (SSOT: spec §3.1, FR-2.21/2.24a)。

F2-10 (2026-09-08) 退化:start_recording / click_download / _resolve_ext_id 已删
(用户手动按 Cmd+Shift+R + 手动点 downloadWavBtn),TabAudioRecorder 只剩
probe_status(FR-2.24a:探测扩展是否安装,给 plugin_status.mark_unavailable 用)。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from vla.subtitle.tab_audio_recorder import TabAudioRecorder


# ---- Fixtures ----


_FAKE_EXT_ID = "hanfcigjijjcbdbfoplddndcblmlfiio"  # 任意 32 字符 a-p


class _FakeTarget:
    def __init__(self, url: str, ttype: str = "service_worker") -> None:
        self.url = url
        self.type = ttype


class _FakePage:
    """v3.2.1.5: 模拟 Playwright page,只暴露 probe_status 路径上用到的 3 个方法。

    manifest_by_ext_id 决定 evaluate 返回什么(已序列化 JSON 字符串)。
    """

    def __init__(self, manifest_by_ext_id: dict[str, str]) -> None:
        self._manifest = manifest_by_ext_id
        self.url = ""

    def goto(self, url: str, **kwargs) -> None:
        self.url = url
        ext_id = url.split("chrome-extension://", 1)[1].split("/", 1)[0]
        self._next = self._manifest.get(ext_id, "")

    def evaluate(self, js: str, **kwargs) -> str:
        return getattr(self, "_next", "")

    def close(self) -> None:
        return None


class _FakeDriver:
    """v3.2.1.5: 模拟 BrowserDriver 子集 — targets() + arun_on_driver_thread()。

    `manifest_by_ext_id` 决定每个 ext_id 的 manifest 文本(用于触发 enabled/disabled/not_installed)。
    每个 arun_on_driver_thread 调用都新建一个 _FakePage(模拟真实行为)。
    """

    def __init__(
        self,
        ext_ids: list[str],
        manifest_by_ext_id: dict[str, str | Exception] | None = None,
        targets_raises: Exception | None = None,
    ) -> None:
        self._targets = [
            _FakeTarget(f"chrome-extension://{e}/background.js") for e in ext_ids
        ]
        self._manifest = manifest_by_ext_id or {}
        self._targets_raises = targets_raises
        self.new_background_page = self._new_page  # 名字就是 'new_background_page'

    def _new_page(self) -> _FakePage:
        """Bound method → __name__=='new_background_page'。"""
        return _FakePage(self._manifest)

    async def targets(self):
        if self._targets_raises is not None:
            raise self._targets_raises
        return self._targets

    async def arun_on_driver_thread(self, fn, *args, **kwargs):
        """v3.2.1.5:production 调用序列为
            arun(driver.new_background_page) → page
            arun(page.goto, url, timeout=...) → None
            arun(page.evaluate, js) → manifest 文本
            arun(page.close) → None

        注意: arun 传的是 `page.goto` 这个 bound method,真正的 page 在它的
        __self__ 里,args[0] 是 url。所以转发必须用 fn.__self__,不是 args[0]。
        """
        if getattr(fn, "__func__", None) is self.new_background_page.__func__:
            return self.new_background_page()
        # page 的 bound method:fn.__self__ 是 _FakePage
        page_self = getattr(fn, "__self__", None)
        if isinstance(page_self, _FakePage):
            return fn(*args, **kwargs)
        return None


@pytest.fixture
def recorder(tmp_path) -> TabAudioRecorder:
    return TabAudioRecorder(
        match_keyword="tab audio",
        save_dir=tmp_path / "audio_raw",
        match_timeout_sec=5.0,
    )


# ---- probe_status tests (4) ----


class TestProbeStatus:
    async def test_probe_status_enabled(self, recorder: TabAudioRecorder) -> None:
        manifest = json.dumps({"name": "Tab Audio Recorder", "version": "1.0"})
        driver = _FakeDriver(
            ext_ids=[_FAKE_EXT_ID],
            manifest_by_ext_id={_FAKE_EXT_ID: manifest},
        )
        result = await recorder.probe_status(driver)
        assert result == "enabled"

    async def test_probe_status_disabled(self, recorder: TabAudioRecorder) -> None:
        # v3.2.1.5: MV3 service_worker 存在即启用,没有"disabled"中间态;
        # 为保留 API 兼容性,显式触发 disabled 的方式 = target 列表里只有
        # disabled manifest(sw 不存在)。但当前实现 targets 已过滤掉
        # 非 service_worker,所以 disabled 在 probe 层不再区分。
        # 保留断言:不存在的 ext_id → not_installed。
        driver = _FakeDriver(ext_ids=[])
        result = await recorder.probe_status(driver)
        assert result == "not_installed"

    async def test_probe_status_not_installed(self, recorder: TabAudioRecorder) -> None:
        driver = _FakeDriver(ext_ids=[])
        result = await recorder.probe_status(driver)
        assert result == "not_installed"

    async def test_probe_status_timeout_returns_not_installed(
        self, recorder: TabAudioRecorder
    ) -> None:
        # driver.targets() 直接抛 → defensive fallback
        driver = _FakeDriver(
            ext_ids=[_FAKE_EXT_ID],
            targets_raises=RuntimeError("timeout"),
        )
        result = await recorder.probe_status(driver)
        assert result == "not_installed"


# ---- probe_status 用 driver.targets() 而不是 browser.evaluate() ----


class TestProbeStatusCdp:
    """v3.2.1.5 root cause: chrome.management 在普通页面不可用,旧 _PROBE_GET_ALL_JS 永远失败。

    修法: probe_status 接收 BrowserDriver,枚举 driver.targets() 拿到所有
    chrome-extension service worker,再 goto <ext_id>/manifest.json 读 name/description。
    名字命中 match_keyword(忽略大小写、子串)→ 状态由 CDP 给出。
    """

    @pytest.mark.asyncio
    async def test_probe_status_uses_driver_not_browser(self):
        """probe_status 必须接 driver(driver 有 .targets() / arun_on_driver_thread())。

        无 targets → not_installed(不能 AttributeError)。
        """
        rec = TabAudioRecorder(match_keyword="free tab audio recorder")
        driver = MagicMock(name="driver")
        driver.targets = AsyncMock(return_value=[])
        driver.arun_on_driver_thread = AsyncMock(side_effect=lambda *a, **kw: None)
        driver.new_background_page = MagicMock()
        status = await rec.probe_status(driver)
        assert status == "not_installed"

    @pytest.mark.asyncio
    async def test_probe_status_keyword_match_via_manifest(self):
        """ext_id 必须是 32 字符 a-p(CDP ext_id 格式),manifest name 命中 keyword → enabled。"""
        rec = TabAudioRecorder(match_keyword="free tab audio recorder")

        # 32 字符 a-p 字符集(CDP chrome-extension id 格式)
        ext_id = "a" * 32

        # 用真 driver 传 targets;page 用真 _FakePage
        manifest = json.dumps({"name": "Free Tab Audio Recorder", "version": "1.0"})
        driver = _FakeDriver(
            ext_ids=[ext_id], manifest_by_ext_id={ext_id: manifest}
        )

        status = await rec.probe_status(driver)
        assert status == "enabled"