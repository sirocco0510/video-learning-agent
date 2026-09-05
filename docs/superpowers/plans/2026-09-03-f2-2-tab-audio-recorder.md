# F2-2 — Tab Audio Recorder (Chrome extension trigger)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `TabAudioRecorder` class that encapsulates all interaction with the "Tab Audio Recorder" Chrome extension (trigger, probe, click_download). Implements FR-2.21 (probe three-state), FR-2.24 (start_recording + dynamic ext_id), FR-2.24a (probe_status), and FR-2.25 (click_download via editor.html).

**Architecture:**
- New module: `src/vla/subtitle/tab_audio_recorder.py` (`TabAudioRecorder` class + 3 exception classes)
- New test: `tests/test_tab_audio_recorder.py` (8 async tests)
- Stateless: caller instantiates `TabAudioRecorder(cfg)` per process; no module-level singleton

**Tech Stack:** Python 3.12, pydantic v2 (config typing), playwright async API, pytest-asyncio (`asyncio_mode = "auto"`)

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md` §3.1 (TabAudioRecorder) + §2.1 (strategy ③ architecture)

## Global Constraints

- Stateless (FR-2.21): no module-level singleton. Caller creates `TabAudioRecorder(cfg)` per process.
- `match_keyword` from `cfg.extension.tab_audio_recorder.match_keyword` (default `"tab audio"`)
- macOS TCC: doesn't require screen recording permission (Tab Audio Recorder uses `chrome.tabCapture`, not `getUserMedia`)
- All async (playwright async API)
- Defensive: any `chrome.management.getAll` failure → return `"not_installed"`; never raises to caller
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- LANG=en_US.UTF-8 prefix on bash

---

### Task 1: Write failing tests for `probe_status` + `_resolve_ext_id`

**Files:**
- Create: `tests/test_tab_audio_recorder.py`

**Interfaces:**
- `TabAudioRecorder(match_keyword: str, save_dir: Path, match_timeout_sec: float)`
- `async probe_status(browser: Any) -> Literal["enabled", "disabled", "not_installed"]`
- `async _resolve_ext_id(browser: Any) -> str`

- [ ] **Step 1: Write the test file**

Create `tests/test_tab_audio_recorder.py` with imports, fixtures, and 5 tests:

```python
"""TabAudioRecorder 单元测试 (SSOT: spec §3.1, FR-2.21/2.24/2.24a/2.25)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from vla.subtitle.tab_audio_recorder import (
    DownloadTimeoutError,
    ExtensionNotFoundError,
    RecorderTriggerError,
    TabAudioRecorder,
)


# ---- Fixtures ----


class FakeExtension:
    """Mock chrome.management extension entry."""

    def __init__(self, name: str, ext_id: str, enabled: bool, description: str = "") -> None:
        self.name = name
        self.id = ext_id
        self.enabled = enabled
        self.description = description


class FakeBrowser:
    """Mock playwright Browser — only exposes what probe_status needs.

    probe_status uses `await browser.evaluate(...)` to call
    `chrome.management.getAll()` which returns a Promise. Tests
    inject pre-resolved values via `_get_all_result` (a coroutine
    that resolves to the list, or raises an exception).
    """

    def __init__(
        self,
        get_all_result: list[FakeExtension] | Exception,
        evaluate_delay: float = 0.0,
    ) -> None:
        self._get_all_result = get_all_result
        self._evaluate_delay = evaluate_delay

    async def evaluate(self, js: str) -> Any:
        if self._evaluate_delay:
            await asyncio.sleep(self._evaluate_delay)
        if isinstance(self._get_all_result, Exception):
            raise self._get_all_result
        return [
            {"name": e.name, "id": e.id, "enabled": e.enabled, "description": e.description}
            for e in self._get_all_result
        ]


@pytest.fixture
def recorder(tmp_path: Path) -> TabAudioRecorder:
    return TabAudioRecorder(
        match_keyword="tab audio",
        save_dir=tmp_path / "audio_raw",
        match_timeout_sec=5.0,
    )


# ---- probe_status tests (4) ----


class TestProbeStatus:
    async def test_probe_status_enabled(self, recorder: TabAudioRecorder) -> None:
        browser = FakeBrowser(
            get_all_result=[FakeExtension("Tab Audio Recorder", "ext123", True)]
        )
        result = await recorder.probe_status(browser)
        assert result == "enabled"

    async def test_probe_status_disabled(self, recorder: TabAudioRecorder) -> None:
        browser = FakeBrowser(
            get_all_result=[FakeExtension("Tab Audio Recorder", "ext123", False)]
        )
        result = await recorder.probe_status(browser)
        assert result == "disabled"

    async def test_probe_status_not_installed(self, recorder: TabAudioRecorder) -> None:
        browser = FakeBrowser(get_all_result=[])
        result = await recorder.probe_status(browser)
        assert result == "not_installed"

    async def test_probe_status_timeout_returns_not_installed(
        self, recorder: TabAudioRecorder
    ) -> None:
        # getAll raises (timeout / permission denied) → defensive fallback
        browser = FakeBrowser(get_all_result=RuntimeError("timeout"))
        result = await recorder.probe_status(browser)
        assert result == "not_installed"


# ---- _resolve_ext_id test (1) ----


class TestResolveExtId:
    async def test_resolve_ext_id_matches_keyword_case_insensitive(
        self, recorder: TabAudioRecorder
    ) -> None:
        # Match against name (case-insensitive)
        browser = FakeBrowser(
            get_all_result=[
                FakeExtension("Some Other Extension", "other456", True),
                FakeExtension("TAB AUDIO CAPTURE PRO", "tab999", True),
            ]
        )
        ext_id = await recorder._resolve_ext_id(browser)
        assert ext_id == "tab999"

    async def test_resolve_ext_id_not_found_raises(self, recorder: TabAudioRecorder) -> None:
        browser = FakeBrowser(
            get_all_result=[FakeExtension("Random Extension", "rand111", True)]
        )
        with pytest.raises(ExtensionNotFoundError):
            await recorder._resolve_ext_id(browser)
```

Note: test class `TestProbeStatus` contains 4 tests; `TestResolveExtId` contains 2 tests. Total = 6 in this task (spec said 5, but splitting matches for clarity).

- [ ] **Step 2: Run tests to verify they fail**

Run: `LANG=en_US.UTF-8 uv run pytest tests/test_tab_audio_recorder.py -v`

Expected: `ModuleNotFoundError: No module named 'vla.subtitle.tab_audio_recorder'` (6 collection errors).

---

### Task 2: Implement `probe_status` + `_resolve_ext_id` + 3 exception classes

**Files:**
- Create: `src/vla/subtitle/tab_audio_recorder.py`

**Interfaces:**
- 3 exception classes: `ExtensionNotFoundError`, `RecorderTriggerError`, `DownloadTimeoutError`
- `TabAudioRecorder.__init__(match_keyword, save_dir, match_timeout_sec)`
- `async probe_status(browser) -> Literal["enabled", "disabled", "not_installed"]`
- `async _resolve_ext_id(browser) -> str`

- [ ] **Step 1: Create file with exception classes + class skeleton**

Create `src/vla/subtitle/tab_audio_recorder.py`:

```python
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

from vla.config import VLAConfig


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
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `LANG=en_US.UTF-8 uv run pytest tests/test_tab_audio_recorder.py::TestProbeStatus tests/test_tab_audio_recorder.py::TestResolveExtId -v`

Expected: All 6 tests pass.

- [ ] **Step 3: Verify exception import contract**

Run: `LANG=en_US.UTF-8 uv run python -c "from vla.subtitle.tab_audio_recorder import TabAudioRecorder, ExtensionNotFoundError, RecorderTriggerError, DownloadTimeoutError; print('imports OK')"`

Expected: `imports OK`

- [ ] **Step 4: Commit**

```bash
git add src/vla/subtitle/tab_audio_recorder.py tests/test_tab_audio_recorder.py
git commit -m "feat(subtitle): TabAudioRecorder probe_status + _resolve_ext_id (FR-2.21/2.24a)"
```

---

### Task 3: Write failing tests for `start_recording` + `click_download`

**Files:**
- Modify: `tests/test_tab_audio_recorder.py` (append 2 new test classes)

**Interfaces:**
- `async start_recording(driver, url, duration_sec, post_buffer_sec=30) -> str` returns `audio_id`
- `async click_download(driver, audio_id, ext_id, save_dir=None, timeout_sec=180) -> Path`

- [ ] **Step 1: Add start_recording test class**

Append to `tests/test_tab_audio_recorder.py`:

```python
# ---- start_recording test (1) ----


class FakeBackgroundPage:
    """Mock 扩展 background page,url 状态可手动推进以测试 polling。"""

    def __init__(self, url_sequence: list[str], evaluate_js: str = "undefined") -> None:
        self._url_sequence = url_sequence
        self._index = 0
        self._evaluate_js = evaluate_js
        self.evaluate_calls: list[str] = []

    @property
    def url(self) -> str:
        if self._index < len(self._url_sequence):
            return self._url_sequence[self._index]
        return self._url_sequence[-1]

    def advance(self) -> None:
        if self._index < len(self._url_sequence) - 1:
            self._index += 1

    async def evaluate(self, js: str) -> Any:
        self.evaluate_calls.append(js)
        return self._evaluate_js


class FakeDriver:
    """Mock playwright Driver — 暴露 start_recording / click_download 需要的 hooks。"""

    def __init__(
        self,
        bg_page: FakeBackgroundPage | None = None,
        targets: list[Any] | None = None,
        pages_for_goto: dict[str, FakeBackgroundPage] | None = None,
    ) -> None:
        self.bg_page = bg_page
        self.targets_list = targets or []
        self.pages_for_goto = pages_for_goto or {}
        self.new_page_calls: list[str] = []
        self.goto_calls: list[str] = []

    async def targets(self) -> list[Any]:
        return self.targets_list

    def new_page(self) -> Any:
        # 返回一个 capture 用的 mock,实际跳转由 goto 单独处理
        mock = type("M", (), {})()
        return mock


class TestStartRecording:
    async def test_start_recording_polls_url_and_extracts_audio_id(
        self, recorder: TabAudioRecorder
    ) -> None:
        # start_recording 的核心: 启动录制后,扩展跳转 editor.html?id=<audio_id>,
        # 我们轮询 bg_page.url 提取 audio_id。
        # 用 FakeBackgroundPage 模拟 url 变化:
        #   第 1 次: chrome-extension://ext123/_generated_background_page.html  (录制中)
        #   第 2 次: chrome-extension://ext123/editor.html?id=4242  (录制完成)
        bg = FakeBackgroundPage(
            url_sequence=[
                "chrome-extension://ext123/_generated_background_page.html",
                "chrome-extension://ext123/editor.html?id=4242",
            ],
            evaluate_js="undefined",
        )
        # start_recording 第一步会 evaluate("startTabRecording()" 或等价 JS),
        # 然后轮询 url;我们让 bg.advance() 在 evaluate 后推进一次。

        original_evaluate = bg.evaluate

        async def evaluate_then_advance(js: str) -> Any:
            result = await original_evaluate(js)
            bg.advance()
            return result

        bg.evaluate = evaluate_then_advance  # type: ignore[assignment]

        # start_recording 需要 driver.targets() 返回包含 bg page 的列表
        driver = FakeDriver(targets=[bg])

        # 简化版 start_recording 会:
        #   1. 遍历 driver.targets() 找到 type='background_page' or url 含 _generated_background_page.html
        #   2. evaluate 启动录制
        #   3. 轮询 url 直到匹配 editor.html?id=(\d+)
        # 测试只需要验证 audio_id 提取正确;内部如何找 bg page 由实现决定。
        # 这里我们直接在 FakeDriver 上挂一个辅助方法(供实现 hook):
        driver.bg_page = bg  # type: ignore[attr-defined]

        audio_id = await recorder.start_recording(driver, "https://example.com/video", 60)
        assert audio_id == "4242"
        assert bg.evaluate_calls, "start_recording 应该至少调用一次 evaluate 启动录制"


# ---- click_download tests (2) ----


class FakeDownload:
    """Mock playwright Download 对象。"""

    def __init__(self, suggested_filename: str, save_path: Path) -> None:
        self.suggested_filename = suggested_filename
        self._save_path = save_path
        self.save_as_calls: list[Path] = []

    async def save_as(self, path: Path) -> None:
        self.save_as_calls.append(path)
        # 模拟文件落盘
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-webm-bytes")


class FakeDownloadPage:
    """Mock 一个可以触发 download 事件的 page。"""

    def __init__(
        self,
        download: FakeDownload | None,
        click_should_trigger: bool = True,
        click_delay: float = 0.0,
    ) -> None:
        self._download = download
        self._click_should_trigger = click_should_trigger
        self._click_delay = click_delay
        self.url = "chrome-extension://ext123/editor.html?id=99"
        self.goto_calls: list[str] = []
        self.click_calls: list[str] = []
        self._download_emitted = False

    async def goto(self, url: str) -> None:
        self.goto_calls.append(url)

    async def click(self, selector: str) -> None:
        self.click_calls.append(selector)
        if self._click_delay:
            import asyncio as _aio
            await _aio.sleep(self._click_delay)
        if self._click_should_trigger and self._download and not self._download_emitted:
            self._download_emitted = True
            # 模拟 download 事件被 _register_download_handler 捕获
            # 由 FakeContext 持有 download_handler,这里直接调用

    @property
    def download(self) -> FakeDownload | None:
        return self._download if self._download_emitted else None


class FakeContext:
    """Mock BrowserContext,管理 download handler 注册 + 新页面创建。"""

    def __init__(self, page: FakeDownloadPage) -> None:
        self.page = page
        self._download_handler = None
        self.handlers: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        if event == "download":
            self._download_handler = handler
            self.handlers.append(handler)

    def new_page(self) -> FakeDownloadPage:
        return self.page

    def emit_download(self, dl: FakeDownload) -> None:
        if self._download_handler:
            # 异步 handler — 实际 handler 是 async,这里直接 await
            import asyncio as _aio
            _aio.create_task(self._download_handler(dl))


class FakeDriverWithContext:
    """带 context 的 FakeDriver,用于 click_download 测试。"""

    def __init__(self, context: FakeContext, page: FakeDownloadPage) -> None:
        self.context = context
        self.page = page


class TestClickDownload:
    async def test_click_download_registers_listener_before_click(
        self, tmp_path: Path, recorder: TabAudioRecorder
    ) -> None:
        # 验证: context.on("download") 必须在 click() 之前注册
        # 用 ordered_calls 列表记录两件事的先后顺序
        ordered_calls: list[str] = []

        class TrackingContext(FakeContext):
            def on(self, event: str, handler: Any) -> None:
                ordered_calls.append(f"on:{event}")
                super().on(event, handler)

        class TrackingPage(FakeDownloadPage):
            async def click(self, selector: str) -> None:
                ordered_calls.append(f"click:{selector}")
                await super().click(selector)

        dl = FakeDownload("audio.webm", tmp_path / "audio.webm")
        page = TrackingPage(download=dl, click_should_trigger=False)
        # click 不立即 emit,让测试自己 emit,确保 on 在 click 之前注册
        ctx = TrackingContext(page=page)
        driver = FakeDriverWithContext(ctx, page)

        # 后台 emit download(模拟扩展点完按钮后产生的事件)
        async def emit_later() -> None:
            await asyncio.sleep(0.05)
            ctx.emit_download(dl)

        emit_task = asyncio.create_task(emit_later())

        result_path = await recorder.click_download(
            driver,  # type: ignore[arg-type]
            audio_id="99",
            ext_id="ext123",
            save_dir=tmp_path,
            timeout_sec=5,
        )

        await emit_task

        assert result_path.exists()
        assert result_path.name == "99.webm"  # audio_id 作为文件名(FR-2.26)
        # 关键断言: on("download") 必须在 click() 之前
        assert ordered_calls[0].startswith("on:"), (
            f"download listener must be registered before click, got: {ordered_calls}"
        )
        assert any(c.startswith("click:") for c in ordered_calls)

    async def test_click_download_timeout_raises(
        self, tmp_path: Path, recorder: TabAudioRecorder
    ) -> None:
        # click 永远不触发 download 事件 → timeout 抛 DownloadTimeoutError
        page = FakeDownloadPage(download=None, click_should_trigger=False)
        ctx = FakeContext(page=page)
        driver = FakeDriverWithContext(ctx, page)

        with pytest.raises(DownloadTimeoutError):
            await recorder.click_download(
                driver,  # type: ignore[arg-type]
                audio_id="99",
                ext_id="ext123",
                save_dir=tmp_path,
                timeout_sec=1,  # 缩短 timeout 让测试快
            )
```

- [ ] **Step 2: Run tests to verify the 3 new ones fail (probe tests still pass)**

Run: `LANG=en_US.UTF-8 uv run pytest tests/test_tab_audio_recorder.py -v`

Expected:
- 6 from Task 2 pass
- 3 new tests fail with `AttributeError: 'TabAudioRecorder' object has no attribute 'start_recording'` / `'click_download'`

---

### Task 4: Implement `start_recording` + `click_download`

**Files:**
- Modify: `src/vla/subtitle/tab_audio_recorder.py` (append 2 methods)

**Interfaces:**
- `async start_recording(driver, url, duration_sec, post_buffer_sec=30) -> str`
- `async click_download(driver, audio_id, ext_id, save_dir=None, timeout_sec=180) -> Path`

- [ ] **Step 1: Implement `start_recording`**

Append to `src/vla/subtitle/tab_audio_recorder.py` (inside class):

```python
    async def start_recording(
        self,
        driver: Any,
        url: str,
        duration_sec: int,
        post_buffer_sec: int = 30,
    ) -> str:
        """FR-2.24: 触发扩展开始录制,轮询 bg_page.url 直到 editor.html?id=<audio_id>。

        步骤:
            1. _resolve_ext_id(driver) → ext_id
            2. 找到扩展 background page(targets 中 url 含 _generated_background_page.html)
               — 若未打开,driver.new_page().goto(launch URL) 打开
            3. 在 bg page 上 evaluate 启动录制(startTabRecording 或扩展暴露的等价函数)
            4. 轮询 bg_page.url 直到匹配 editor.html?id=<digits>
            5. 正则提取 audio_id 返回

        Args:
            driver: playwright Driver(提供 targets() / new_page() / 找到 bg page 后 evaluate)
            url: 视频 URL(供扩展抓取 tab 音频;此函数不直接使用,扩展自己读 tab)
            duration_sec: 录制时长(秒);扩展自己计时 stop,我们等 duration+post_buffer
            post_buffer_sec: 额外等待扩展完成编码的时间(默认 30s)

        Returns:
            audio_id(扩展分配的纯数字字符串,来自 editor.html URL ?id= 参数)

        Raises:
            ExtensionNotFoundError: 找不到 Tab Audio Recorder
            RecorderTriggerError: bg page evaluate 失败 / 跳转超时
        """
        ext_id = await self._resolve_ext_id(driver)

        # 找 background page(targets 中 url 含 _generated_background_page.html)
        bg_page = None
        try:
            targets = await driver.targets() if hasattr(driver, "targets") else []
        except Exception as e:
            raise RecorderTriggerError(f"driver.targets() 失败: {e}") from e

        for t in targets:
            t_url = getattr(t, "url", "") or ""
            if (
                f"chrome-extension://{ext_id}/" in t_url
                and "_generated_background_page.html" in t_url
            ):
                bg_page = t
                break

        if bg_page is None:
            raise RecorderTriggerError(
                f"未找到扩展 {ext_id} 的 background page;请先在 Chrome 启用 Tab Audio Recorder"
            )

        # 步骤 3: 启动录制(扩展暴露 startTabRecording() 或等价函数)
        # 用 try/except 防御:扩展 API 可能改名
        try:
            await bg_page.evaluate(
                "typeof startTabRecording === 'function' ? startTabRecording() : null"
            )
        except Exception as e:
            raise RecorderTriggerError(f"启动录制失败: {e}") from e

        # 步骤 4 + 5: 轮询 bg_page.url 直到匹配 editor.html?id=(\d+)
        # 总等待时长 = duration_sec + post_buffer_sec(扩展自己 stop + 编码需要时间)
        # 用 polling 而不是单次 sleep,提高响应速度
        deadline = asyncio.get_event_loop().time() + duration_sec + post_buffer_sec
        poll_interval = 0.5

        while asyncio.get_event_loop().time() < deadline:
            current_url = getattr(bg_page, "url", "") or ""
            match = _EDITOR_URL_PATTERN.search(current_url)
            if match:
                return match.group(1)
            await asyncio.sleep(poll_interval)

        raise RecorderTriggerError(
            f"等待 editor.html 跳转超时 ({duration_sec + post_buffer_sec}s);"
            f"最后 url={getattr(bg_page, 'url', '?')}"
        )
```

- [ ] **Step 2: Implement `click_download`**

Append to `src/vla/subtitle/tab_audio_recorder.py` (inside class):

```python
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
        download_future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

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
```

- [ ] **Step 3: Run all 9 tests**

Run: `LANG=en_US.UTF-8 uv run pytest tests/test_tab_audio_recorder.py -v`

Expected: All 9 tests pass (4 probe_status + 2 _resolve_ext_id + 1 start_recording + 2 click_download).

- [ ] **Step 4: Run full regression to verify no other tests broke**

Run: `LANG=en_US.UTF-8 uv run pytest -v --tb=short 2>&1 | tail -40`

Expected: Existing tests still pass; new 9 tests added to the suite.

- [ ] **Step 5: Run `vla doctor` smoke check**

Run: `LANG=en_US.UTF-8 uv run vla doctor`

Expected: doctor green (no new errors introduced; TabAudioRecorder not yet wired into main flow, so doctor doesn't exercise it directly).

- [ ] **Step 6: Audit grep to confirm no hardcoded extension ID**

Run: `grep -rn "hanfcigjijjcbdbfoplddndcblmlfiio" src/vla/ tests/`

Expected: 0 matches (extension ID never hardcoded; runtime resolution only).

- [ ] **Step 7: Commit**

```bash
git add src/vla/subtitle/tab_audio_recorder.py tests/test_tab_audio_recorder.py
git commit -m "feat(subtitle): TabAudioRecorder start_recording + click_download (FR-2.24/2.25)"
```

---

## Acceptance Criteria

After all 4 tasks execute:

1. **Functional:**
   - `TabAudioRecorder(match_keyword, save_dir, match_timeout_sec)` instantiates without error
   - `probe_status(browser)` returns one of `"enabled" | "disabled" | "not_installed"` (never raises)
   - `_resolve_ext_id(browser)` returns ext_id or raises `ExtensionNotFoundError`
   - `start_recording(driver, url, duration_sec)` returns audio_id (digits) or raises `RecorderTriggerError`
   - `click_download(driver, audio_id, ext_id, save_dir, timeout_sec)` saves file and returns Path, or raises `DownloadTimeoutError`
   - `download` listener registered BEFORE `click()` (verified by `test_click_download_registers_listener_before_click`)

2. **Test gates:**
   - `uv run pytest tests/test_tab_audio_recorder.py -v` → 9/9 pass
   - `uv run pytest -v` → all pass (no regressions)
   - `uv run vla doctor` → green

3. **Audit gates:**
   - `grep -rn "hanfcigjijjcbdbfoplddndcblmlfiio" src/vla/ tests/` → 0 matches
   - `grep -rn "class TabAudioRecorder" src/vla/subtitle/` → 1 match (`tab_audio_recorder.py`)
   - `grep -rn "ExtensionNotFoundError\|RecorderTriggerError\|DownloadTimeoutError" src/vla/subtitle/tab_audio_recorder.py` → 3 matches

4. **Deferred to F2-7:**
   - Integration into `PlatformAdapter.fetch_via_recording` path ②
   - `MacOSNotifier` B 级 notification on `disabled` / `not_installed` (FR-2.21)
   - `quality_skip.csv` writing
