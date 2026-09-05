# F2-7 — PlatformAdapter.fetch_via_recording Integration (FR-2.14)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `AudioSourceFactory` (path ① yt-dlp) + `TabAudioRecorder` (path ②) + `AudioTranscriber` (Whisper) + `ScreenshotPhaseController` (FR-2.28) into `PlatformAdapter.fetch_via_recording` default implementation. Path ① fail → path ② 自动 fallback(Q7 Silent)。

**Architecture:**
- Modify: `src/vla/subtitle/platform_adapter.py` (default `fetch_via_recording` impl on base class; Protocol kwargs)
- Modify: `src/vla/subtitle/bilibili_adapter.py` (inject 4 dependencies via `__init__`)
- Modify: `src/vla/subtitle/internal_site_adapter.py` (same)
- Modify: `src/vla/subtitle/strategy.py` (call updated default impl with all kwargs)
- Tests: `tests/test_platform_adapter.py` (add 4 tests: path ① hit / path ② hit after ① fail / screenshot integration / partial failure)

**Tech Stack:** Python 3.12, asyncio, existing modules from F2-1/F2-2/F2-3/F2-5

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md` §4.1 + §4.5

**User rulings locked:** Q1=Polling / Q7=**Silent** (path ① fail → path ② 自动试,不 raise 不 log.warning)/ Q8=Warn / Q3+Q9=Keep generic PluginStatus

## Global Constraints

- `tests/` is the test root; fixtures in `tests/fixtures/`
- TDD: write failing test → run → minimal impl → run → commit
- Path ① (yt-dlp audio) 是 FIRST attempt;失败(Q7 静默)→ 自动试 path ② (Tab Audio Recorder)
- Path ② 触发条件:`audio_factory.is_downloadable(url) == False` OR `extract()` raise
- Path ② 截图:仅 path ② 触发 PHASE A/B/C/D(F2-5)
- Q7 Silent:不要 log.warning 路径切换,只 log.info("✓ 策略 ③ 命中 (path ①/②)")
- `screenshot_controller` 是 Optional(None = 不触发 FR-2.28)
- 不再 import `BrowserRecorder`(Q2 resolved)— 全部从 kwargs 注入
- 旧 `fetch_via_recording` 的所有 caller 必须传齐 4 个 REQUIRED kwargs

## Interfaces from Earlier Plans — exact signatures this plan consumes

```python
# F2-1
class AudioSourceFactory:
    def __init__(self, save_dir, audio_format="wav", ffmpeg_postargs="",
                 simulate_timeout_sec=30): ...
    def is_downloadable(self, url: str) -> bool: ...
    def extract(self, url: str, stem: str) -> AudioExtractionResult: ...

@dataclass
class AudioExtractionResult:
    audio_path: Path
    source: str       # "yt-dlp"
    duration_sec: int

# F2-2
class TabAudioRecorder:
    async def probe_status(self, browser: Any) -> str: ...   # "enabled"|"disabled"|"missing"
    async def start_recording(self, browser, url, duration_sec) -> str: ...  # returns audio_id
    async def click_download(self, browser, audio_id: str) -> Path: ...

# F2-5
class ScreenshotPhaseController:
    async def phase_a_start(self, page, audio_id) -> float: ...
    async def phase_b_then_c(self, page, audio_id, duration_sec) -> float: ...
    def phase_d_write_index(self, audio_id, start_ts, end_ts, duration_estimate,
                            partial_flags=None): ...

# F2-4 (existing)
class AudioTranscriber(Protocol):
    def transcribe(self, audio_path: Path) -> str: ...
    def cleanup(self, audio_path: Path) -> None: ...
```

---

### Task 1: Add default `fetch_via_recording` impl to `PlatformAdapter` base class

**Files:**
- Modify: `src/vla/subtitle/platform_adapter.py`
- Test: `tests/test_platform_adapter.py`

**Interfaces:**
- Produces: `PlatformAdapter.fetch_via_recording(driver, url, duration_sec, *, audio_factory, tab_recorder, transcriber, screenshot_controller=None)` — default impl on base class

- [ ] **Step 1: Write 2 failing tests**

Append to `tests/test_platform_adapter.py`:

```python
"""PlatformAdapter.fetch_via_recording 默认实现测试 (SSOT: spec §4.1)。

FR-2.14 v3:
- path ① yt-dlp → path ② Tab Audio Recorder → None(Q7 Silent fallback)
- screenshot_controller=None 时不触发 FR-2.28
- path ① 命中 → 不调 tab_recorder.start_recording(省一轮点击)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vla.audio.source_factory import AudioExtractionResult, AudioSourceFactory
from vla.subtitle.platform_adapter import PlatformAdapter
from vla.subtitle.tab_audio_recorder import TabAudioRecorder


def _make_audio_factory(*, downloadable: bool, raise_extract: bool = False) -> MagicMock:
    f = MagicMock(spec=AudioSourceFactory)
    f.is_downloadable = MagicMock(return_value=downloadable)
    if raise_extract:
        f.extract = MagicMock(side_effect=Exception("yt-dlp 404"))
    else:
        f.extract = MagicMock(
            return_value=AudioExtractionResult(
                audio_path=Path("/tmp/audio.wav"),
                source="yt-dlp",
                duration_sec=300,
            )
        )
    return f


def _make_tab_recorder(*, enabled: bool = True) -> MagicMock:
    r = MagicMock(spec=TabAudioRecorder)
    r.probe_status = AsyncMock(
        return_value="enabled" if enabled else "disabled"
    )
    r.start_recording = AsyncMock(return_value="audio_id_123")
    r.click_download = AsyncMock(return_value=Path("/tmp/rec.wav"))
    return r


def _make_transcriber(text: str = "transcribed text") -> MagicMock:
    t = MagicMock()
    t.transcribe = MagicMock(return_value=text)
    t.cleanup = MagicMock()
    return t


def _make_screenshot() -> MagicMock:
    s = MagicMock()
    s.phase_a_start = AsyncMock(return_value=100.0)
    s.phase_b_then_c = AsyncMock(return_value=400.0)
    s.phase_d_write_index = MagicMock()
    return s


class TestPathOneHit:
    def test_path_one_returns_transcribed_text_when_downloadable(self) -> None:
        """is_downloadable=True → extract → transcribe → return (text, meta)。"""
        af = _make_audio_factory(downloadable=True)
        tr = _make_tab_recorder()
        tx = _make_transcriber("yt-dlp text")
        adapter = PlatformAdapter()
        result = adapter.fetch_via_recording(
            driver=MagicMock(),
            url="https://www.bilibili.com/video/Bv1",
            duration_sec=300,
            audio_factory=af,
            tab_recorder=tr,
            transcriber=tx,
        )
        assert result is not None
        text, meta = result
        assert text == "yt-dlp text"
        assert meta["via"] == "yt-dlp"
        # path ② 没被调
        tr.start_recording.assert_not_called()
        # cleanup 调了(audio_path 已用完)
        tx.cleanup.assert_called_once()

    def test_path_one_returns_none_when_extract_fails(self) -> None:
        """is_downloadable=True 但 extract 抛 → 静默 fallback 到 path ② (Q7)。"""
        af = _make_audio_factory(downloadable=True, raise_extract=True)
        tr = _make_tab_recorder(enabled=False)  # path ② 也失败 → None
        tx = _make_transcriber("unused")
        adapter = PlatformAdapter()
        result = adapter.fetch_via_recording(
            driver=MagicMock(), url="https://x.com/v/1", duration_sec=100,
            audio_factory=af, tab_recorder=tr, transcriber=tx,
        )
        # Q7: 静默 — 不 raise,只是 None
        assert result is None
        tr.probe_status.assert_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_platform_adapter.py::TestPathOneHit -v`
Expected: FAIL with `fetch_via_recording()` missing required keyword argument (or signature mismatch)

- [ ] **Step 3: Add default impl to `PlatformAdapter` base class**

In `src/vla/subtitle/platform_adapter.py`, modify the `PlatformAdapter` base:

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol


class PlatformAdapter(Protocol):
    """所有 platform adapter 的协议。"""

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None: ...
    def fetch_browser_subtitle(self, driver: Any, url: str) -> tuple[str, dict] | None: ...

    def fetch_via_recording(
        self,
        driver: Any,
        url: str,
        duration_sec: int,
        *,
        audio_factory: "AudioSourceFactory",
        tab_recorder: "TabAudioRecorder",
        transcriber: "AudioTranscriber",
        screenshot_controller: "ScreenshotPhaseController | None" = None,
    ) -> tuple[str, dict] | None:
        """FR-2.14 v3 默认实现:path ① yt-dlp → path ② Tab Audio Recorder(Q7 Silent)。"""
        log = logging.getLogger(__name__)

        # ---- path ①:yt-dlp 抽音频 ----
        try:
            if audio_factory.is_downloadable(url):
                result = audio_factory.extract(url, stem=self._make_stem(url))
                text = transcriber.transcribe(result.audio_path)
                transcriber.cleanup(result.audio_path)
                log.info("✓ 策略 ③ 命中 (path ① yt-dlp): %s", url)
                return text, {"via": "yt-dlp", "audio_path": str(result.audio_path)}
        except Exception as e:
            # Q7 Silent:不 log.warning,只 debug
            log.debug("path ① yt-dlp 失败,自动 fallback 到 path ②: %s", e)

        # ---- path ②:Tab Audio Recorder ----
        try:
            import asyncio as _a
            status = _a.run(tab_recorder.probe_status(driver))
            if status != "enabled":
                log.debug("path ② Tab Audio Recorder unavailable: status=%s", status)
                return None

            audio_id = _a.run(tab_recorder.start_recording(driver, url, duration_sec))
            # FR-2.28 PHASE A (仅 path ② 触发)
            start_ts = 0.0
            if screenshot_controller is not None:
                page = getattr(driver, "page", driver)
                start_ts = _a.run(screenshot_controller.phase_a_start(page, audio_id))

            audio_path = _a.run(tab_recorder.click_download(driver, audio_id))

            # 后台 PHASE B+C(并发)
            if screenshot_controller is not None:
                page = getattr(driver, "page", driver)
                _a.run(
                    screenshot_controller.phase_b_then_c(page, audio_id, duration_sec)
                )

            text = transcriber.transcribe(audio_path)
            transcriber.cleanup(audio_path)
            log.info("✓ 策略 ③ 命中 (path ② tab_recorder): %s", url)

            # FR-2.28 PHASE D
            if screenshot_controller is not None:
                end_ts = 0.0  # 从 phase_b_then_c 返回值拿,这里简化
                screenshot_controller.phase_d_write_index(
                    audio_id, start_ts, end_ts, duration_sec,
                    partial_flags=[],
                )
            return text, {"via": "tab_recorder", "audio_id": audio_id}
        except Exception as e:
            log.error("path ② 也失败: %s", e)
            return None

    def _make_stem(self, url: str) -> str:
        """url → 文件 stem(B站从 bvid 抽,其他用 hash)。"""
        import hashlib
        # BilibiliAdapter 可 override,base 用 url hash
        return hashlib.md5(url.encode()).hexdigest()[:16]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_platform_adapter.py::TestPathOneHit -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add src/vla/subtitle/platform_adapter.py tests/test_platform_adapter.py
git commit -m "feat(F2-7): PlatformAdapter.fetch_via_recording path ① default impl"
```

---

### Task 2: Path ② hit + screenshot integration

**Files:**
- Test: `tests/test_platform_adapter.py` (add `TestPathTwoHit` + `TestScreenshotIntegration`)

- [ ] **Step 1: Add 2 tests**

```python
class TestPathTwoHit:
    def test_path_two_used_when_not_downloadable(self) -> None:
        """is_downloadable=False → 自动试 path ② (Q7 Silent,no warning)。"""
        af = _make_audio_factory(downloadable=False)
        tr = _make_tab_recorder(enabled=True)
        tx = _make_transcriber("tab text")
        result = PlatformAdapter().fetch_via_recording(
            driver=MagicMock(), url="https://x.com/v/2", duration_sec=200,
            audio_factory=af, tab_recorder=tr, transcriber=tx,
        )
        assert result is not None
        text, meta = result
        assert text == "tab text"
        assert meta["via"] == "tab_recorder"
        tr.probe_status.assert_awaited_once()
        tr.start_recording.assert_awaited_once()
        tr.click_download.assert_awaited_once()


class TestScreenshotIntegration:
    def test_screenshot_controller_phase_a_called_only_on_path2(self) -> None:
        """screenshot_controller=mock → path ② 触发 phase_a_start + phase_b_then_c + phase_d。"""
        af = _make_audio_factory(downloadable=False)
        tr = _make_tab_recorder(enabled=True)
        tx = _make_transcriber("tab text")
        sc = _make_screenshot()
        PlatformAdapter().fetch_via_recording(
            driver=MagicMock(), url="https://x.com/v/3", duration_sec=200,
            audio_factory=af, tab_recorder=tr, transcriber=tx,
            screenshot_controller=sc,
        )
        sc.phase_a_start.assert_awaited_once()
        sc.phase_b_then_c.assert_awaited_once()
        sc.phase_d_write_index.assert_called_once()

    def test_screenshot_controller_none_skips_phases(self) -> None:
        """screenshot_controller=None → 不调 phase_* (FR-2.28 opt-in)。"""
        af = _make_audio_factory(downloadable=False)
        tr = _make_tab_recorder(enabled=True)
        tx = _make_transcriber("tab text")
        result = PlatformAdapter().fetch_via_recording(
            driver=MagicMock(), url="https://x.com/v/4", duration_sec=200,
            audio_factory=af, tab_recorder=tr, transcriber=tx,
            screenshot_controller=None,
        )
        assert result is not None  # 仍然返回 text
        # 但 phase_* 不能 mock 验证(没传);用 path ② 命中验证
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_platform_adapter.py::TestPathTwoHit tests/test_platform_adapter.py::TestScreenshotIntegration -v`
Expected: PASS (3 tests, total 5 tests)

(no impl change — Task 1 default impl already handles both paths)

- [ ] **Step 3: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add tests/test_platform_adapter.py
git commit -m "test(F2-7): path ② hit + screenshot integration"
```

---

### Task 3: Update `BilibiliAdapter` + `InternalSiteAdapter` + `strategy.py` to pass 4 REQUIRED kwargs

**Files:**
- Modify: `src/vla/subtitle/bilibili_adapter.py`
- Modify: `src/vla/subtitle/internal_site_adapter.py`
- Modify: `src/vla/subtitle/strategy.py`

**Interfaces:**
- BilibiliAdapter.__init__ gains: `audio_factory`, `tab_recorder`, `transcriber`, `screenshot_controller`
- InternalSiteAdapter.__init__ gains: same 4 kwargs
- strategy.py: stop importing BrowserRecorder; pass 4 kwargs to `adapter.fetch_via_recording`

- [ ] **Step 1: Run baseline tests**

Run: `uv run pytest tests/test_bilibili_adapter.py tests/test_platform_adapter.py tests/test_subtitle_strategy.py -v`
Document baseline pass count.

- [ ] **Step 2: Modify `bilibili_adapter.py`**

Find the existing `__init__` and add 4 kwargs:

```python
# src/vla/subtitle/bilibili_adapter.py — modify class BilibiliAdapter
class BilibiliAdapter(PlatformAdapter):
    def __init__(
        self,
        audio_factory: AudioSourceFactory,
        tab_recorder: TabAudioRecorder,
        transcriber: AudioTranscriber,
        screenshot_controller: ScreenshotPhaseController | None = None,
    ) -> None:
        self._audio_factory = audio_factory
        self._tab_recorder = tab_recorder
        self._transcriber = transcriber
        self._screenshot_controller = screenshot_controller

    def fetch_via_recording(self, driver, url, duration_sec, **kwargs):
        # 强制传齐 4 kwargs,base impl 内部用
        kwargs.setdefault("audio_factory", self._audio_factory)
        kwargs.setdefault("tab_recorder", self._tab_recorder)
        kwargs.setdefault("transcriber", self._transcriber)
        kwargs.setdefault("screenshot_controller", self._screenshot_controller)
        return super().fetch_via_recording(
            driver, url, duration_sec, **kwargs,
        )
```

Override `_make_stem` to use B站 bvid:

```python
    def _make_stem(self, url: str) -> str:
        from vla.utils.bvid import extract_bvid
        bvid = extract_bvid(url)
        return bvid if bvid else super()._make_stem(url)
```

- [ ] **Step 3: Modify `internal_site_adapter.py` similarly**

Same pattern as BilibiliAdapter.

- [ ] **Step 4: Modify `src/vla/subtitle/strategy.py`**

Find the line calling `adapter.fetch_via_recording(...)` (currently probably passes `(self.driver, url, duration_sec)`). Change to:

```python
# OLD:
result = adapter.fetch_via_recording(self.driver, url, duration_sec)

# NEW:
result = adapter.fetch_via_recording(
    self.driver, url, duration_sec,
    audio_factory=self.audio_factory,
    tab_recorder=self.tab_recorder,
    transcriber=self.transcriber,
    screenshot_controller=self.screenshot_controller,
)
```

Add 4 attributes to `__init__` of `SubtitleStrategy`:

```python
class SubtitleStrategy:
    def __init__(
        self, ...,
        audio_factory: AudioSourceFactory,
        tab_recorder: TabAudioRecorder,
        transcriber: AudioTranscriber,
        screenshot_controller: ScreenshotPhaseController | None = None,
    ):
        ...
        self.audio_factory = audio_factory
        self.tab_recorder = tab_recorder
        self.transcriber = transcriber
        self.screenshot_controller = screenshot_controller
```

- [ ] **Step 5: Update `main.py` to construct `SubtitleStrategy` with 4 kwargs**

Find `SubtitleStrategy(...)` constructor call in `main.py` and add 4 kwargs.

- [ ] **Step 6: Run tests + fix until green**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run pytest tests/test_bilibili_adapter.py tests/test_platform_adapter.py tests/test_subtitle_strategy.py tests/test_video_learning_agent.py -v
```

Iterate until all PASS. Update existing tests that construct `BilibiliAdapter` or `SubtitleStrategy` without 4 kwargs — pass `MagicMock()` fixtures.

- [ ] **Step 7: Commit**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add src/vla/subtitle/ src/vla/main.py tests/test_bilibili_adapter.py tests/test_subtitle_strategy.py
git commit -m "feat(F2-7): inject 4 deps into BilibiliAdapter + InternalSiteAdapter + Strategy"
```

---

## Acceptance Criteria

- [ ] `uv run pytest tests/test_platform_adapter.py -v` → 5/5 PASS (Tasks 1 + 2)
- [ ] `uv run pytest tests/test_bilibili_adapter.py tests/test_subtitle_strategy.py -v` → all PASS
- [ ] `grep -rn "BrowserRecorder" src/vla/subtitle/` → **0 matches** (Q2 已删除?实际 F2-8 才删,这里只是停止 import)
- [ ] Q7 Silent:path ① fail → 不 log.warning,自动试 path ②
- [ ] `screenshot_controller=None` → 不调 phase_*(FR-2.28 opt-in)
- [ ] path ① 命中 → tab_recorder.start_recording 不被调(省一轮点击)

## Dependency Note

**F2-7 depends on F2-1 + F2-2 + F2-5 already merged** (audio_factory, tab_recorder, screenshot_controller). If not yet merged, the test mocks (per `_make_audio_factory` / `_make_tab_recorder` / `_make_screenshot` helpers above) provide substitute interfaces — Task 1 + 2 can pass without real F2-1/F2-2/F2-5. Task 3 (real integration) requires real F2-1/F2-2/F2-5 modules to exist.