# FR-2 + FR-3 Implementation Design (2026-09-03)

> **Status:** Draft for user review.
> **Scope:** Implementation strategy for realizing requirements.md §FR-2 (subtitle extraction) and §FR-3 (streaming transcription), given the current dirty-tree state.
> **Predecessor:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` (the FR SSOT). This doc adds **implementation-level clarity only** — does NOT redefine any FR.

---

## 1. Status Overview

After R-01 through R-15 (refactor consolidation) + dirty-tree work, **~70% of FR-2/FR-3 functionality exists** but is largely uncommitted. This doc identifies the gap and proposes a sequencing plan.

### 1.1 Module Inventory

| Spec module | Path | State | Source |
|---|---|---|---|
| `PlatformAdapter` Protocol + Registry | `src/vla/subtitle/platform_adapter.py` | ✅ committed | R-14 territory + dirty tree |
| `BilibiliAdapter` | `src/vla/subtitle/bilibili_adapter.py` | ✅ committed (dirty) | dirty tree |
| `InternalSiteAdapter` (stub) | `src/vla/subtitle/internal_site_adapter.py` | ✅ committed | dirty tree |
| `browser_driver.py` (Puppeteer) | `src/vla/subtitle/browser_driver.py` | ✅ committed (dirty) | dirty tree |
| `SubtitleStrategy` (3-tier orchestration) | `src/vla/subtitle/strategy.py` | ✅ committed (dirty) | dirty tree |
| `BrowserRecorder` (video-recording) | `src/vla/subtitle/browser_record.py` | ✅ committed (dirty) | dirty tree — **but per FR-2.14, video-recording is REMOVED**; needs rewrite |
| `bilibili_official.py` (old API) | `src/vla/subtitle/bilibili_official.py` | ✅ committed | legacy |
| `browser_plugin.py` (legacy) | `src/vla/subtitle/browser_plugin.py` | ✅ committed | legacy, `parse()` kept |
| `normalize.py` (SRT/VTT/ASS) | `src/vla/subtitle/normalize.py` | ✅ committed | R-12 |
| `probe_strategy.py` + `probes/` | `src/vla/subtitle/probe_strategy.py` | ✅ committed | R-14 |
| `StreamingTranscriber` (Whisper) | `src/vla/transcribe/streaming.py` | ⚠️ untracked | dirty tree |
| `postprocess.py` (Level 1 cleanup) | `src/vla/transcribe/postprocess.py` | ⚠️ untracked | dirty tree |
| `PluginStatus` (3-state machine) | `src/vla/state/plugin_status.py` | ⚠️ untracked | dirty tree — for VideoTrans (FR-2.9/2.10), NOT Tab Audio Recorder |
| `QuotaManager` (6h trigger) | `src/vla/state/quota.py` | ⚠️ untracked | dirty tree |
| `VideoLearningAgent` (main) | `src/vla/main.py` | ⚠️ untracked | dirty tree |
| `MacOSNotifier` (info/warning) | `src/vla/ui/macos_notify.py` | ⚠️ dirty | dirty tree |
| `extract_bvid` | `src/vla/utils/bvid.py` | ✅ committed | R-05 |
| `transcribed_file.py` (canonical write/read) | `src/vla/log/transcribed_file.py` | ✅ committed | R-06 |
| `SubtitleRefiner` (Level 4 LLM cleanup) | `src/vla/quality/refiner.py` | ✅ committed (dirty) | R-10 |
| **`TabAudioRecorder`** (Tab Audio Recorder trigger) | `src/vla/subtitle/tab_audio_recorder.py` | ❌ **MISSING** | NEW — FR-2.21/2.24/2.24a/2.25 |
| **`audio/source_factory.py`** (yt-dlp `-x`) | `src/vla/audio/source_factory.py` | ❌ **MISSING** | NEW — FR-2.14路径① |
| **`audio/queue.py`** (asyncio.Queue, cap 10) | `src/vla/audio/queue.py` | ❌ **MISSING** | NEW — FR-2.27 |
| **`audio/worker_pool.py`** (WhisperWorkerPool, 2 workers) | `src/vla/audio/worker_pool.py` | ❌ **MISSING** | NEW — FR-2.27 |
| **`capture/screen_capture.py`** (screencapture -x) | `src/vla/capture/screen_capture.py` | ❌ **MISSING** | NEW — FR-2.28 |

### 1.2 Existing capabilities (already in dirty tree, work but untracked)

**StreamingTranscriber** (`src/vla/transcribe/streaming.py`):
- Lazy-loads `faster_whisper.WhisperModel` on first `transcribe()` (FR-3.2 model size, FR-3.x startup speed)
- ffmpeg subprocess extracts 16kHz mono PCM s16le audio
- VAD filter enabled (FR-3.x speed)
- Auto-deletes video source after audio extraction (FR-3.3)
- Returns `\n`-joined segments

**postprocess.py** (`src/vla/transcribe/postprocess.py`):
- `merge_short_lines(min_chars=8, max_chars=80)` — fragment merging (FR-3.8)
- `dedupe_repeated_segments(min_overlap=6)` — LCS-based dedup of B站 auto-CC repeats
- `clean_transcript()` — entry point
- `PostprocessStats` dataclass for observability
- Pure local; 繁简转换 intentionally deferred to Level 4 (cloud LLM, FR-2.15c)

**SubtitleStrategy** (`src/vla/subtitle/strategy.py`):
- 3-tier degradation orchestration
- `PluginStatus` integration (session-level disable after timeout/skip)
- A-level blocking dialog on first ② miss (FR-2.5/2.6)
- `_safe_close_page` defensive helper (R-15)
- Returns `SubtitleResult(source="api"|"browser"|"whisper")` or `None`

**VideoLearningAgent** (`src/vla/main.py`):
- Dependency-injection constructor (checker / history / summarizer / quota / notifier via `*Like` Protocols)
- `text_provider: Callable[[VideoTask], SubtitleResult]` callback for subtitle fetch
- 6h quota trigger → `summarize_batch` from disk
- Audio retention: passed → cleanup; failed → retain

**PluginStatus** (`src/vla/state/plugin_status.py`):
- 3-state machine: UNKNOWN / AVAILABLE / UNAVAILABLE
- Session-level singleton
- Tracks legacy VideoTrans plugin (FR-2.9/2.10), NOT Tab Audio Recorder

---

## 2. The Core Migration: Video-Recording → Audio-Only (FR-2.14)

**Critical:** FR-2.14 (2026-09-03 重构 v3, 方案 A) explicitly states:
> **不再录屏 / 不再 Puppeteer 流式录音频,Whisper 只用音频。**
> Why: `navigator.mediaDevices.getUserMedia({audio: true})` only gets the microphone, not the tab audio (that's `chrome.tabCapture` exclusive). Microphone recording has ambient noise + notifications → Whisper accuracy drops.

This means the dirty-tree `BrowserRecorder` (which does video screen recording via Puppeteer) **must be REPLACED**, not just refined.

### 2.1 New Strategy ③ Architecture

```
adapter.fetch_via_recording(driver, url, duration_sec) -> tuple[str, dict] | None
  │
  ├─ ① yt-dlp audio extraction (NEW: audio/source_factory.py)
  │    ├─ yt-dlp --simulate first (FR-1.4) to confirm downloadable
  │    │    YES → yt-dlp -x --audio-format wav → logs/audio_raw/<bvid>.wav
  │    │    NO  → fallthrough to ②
  │    └─ Return audio_path → transcribe via StreamingTranscriber → SubtitleResult(source="whisper", via="yt-dlp")
  │
  └─ ② Tab Audio Recorder extension (NEW: subtitle/tab_audio_recorder.py)
       ├─ probe_status(browser) → "enabled"|"disabled"|"not_installed"  [FR-2.24a]
       │    enabled       → continue
       │    disabled      → MacOSNotifier.info("请启用 Tab Audio Recorder") + quality_skip.csv  [FR-2.21]
       │    not_installed → MacOSNotifier.warning("请从 Chrome Web Store 安装") + quality_skip.csv
       ├─ start_recording(driver, url, duration_sec) → audio_id  [FR-2.24]
       │    ├─ _resolve_ext_id(browser) → ext_id (NO hardcode)
       │    ├─ bg page evaluate → extension starts recording
       │    └─ poll bg_page.url → chrome-extension://<ext_id>/editor.html?id=<audio_id>
       ├─ click_download(driver, audio_id, ext_id, save_dir, timeout_sec=180) → Path  [FR-2.25]
       │    └─ context.on("download") FIRST, then click "Download" button → save_as audio_raw/<audio_id>.webm
       └─ audio_id → AudioQueue.push → WhisperWorkerPool worker processes
```

### 2.2 What becomes of the old `BrowserRecorder`?

The old `BrowserRecorder.record_and_transcribe()` (committed at `b0d3eb5` per R-15) does video recording via `ffmpeg -f avfoundation` style path. Per FR-2.14, this is removed.

**Proposal:** Replace `BrowserRecorder.record_and_transcribe()` with a thin wrapper that:
1. Calls `audio/source_factory.extract_audio()` (path ①) OR
2. Returns None → strategy ② falls through to Tab Audio Recorder

The video-recording ffmpeg invocation gets deleted; the audio-only path is the new SSOT.

**Action:** Treat `BrowserRecorder` as dead code; create `audio/source_factory.py` as the new SSOT for path ①. Keep `BrowserRecorder` only as a thin adapter that delegates to the new module (for back-compat with existing tests).

---

## 3. New Module Designs

### 3.1 `subtitle/tab_audio_recorder.py` (FR-2.21/2.24/2.24a/2.25)

**Purpose:** Encapsulate all interaction with the "Tab Audio Recorder" Chrome extension.

**Public API:**

```python
from typing import Literal, Any
from pathlib import Path

class ExtensionNotFoundError(Exception): ...
class RecorderTriggerError(Exception): ...
class DownloadTimeoutError(Exception): ...


class TabAudioRecorder:
    """Tab Audio Recorder 触发器 (SSOT: spec §FR-2.21/2.24/2.24a/2.25)."""

    def __init__(
        self,
        match_keyword: str = "tab audio",
        save_dir: Path = Path("./logs/audio_raw"),
        match_timeout_sec: float = 5.0,
    ) -> None:
        ...

    async def probe_status(self, browser: Any) -> Literal["enabled", "disabled", "not_installed"]:
        """无状态探测 (FR-2.24a): 每次调用即时查 chrome.management.getAll()。
        找不到 → 'not_installed'; 找到了但 enabled=False → 'disabled'; enabled=True → 'enabled'.
        失败 (timeout / permission denied) → 'not_installed'.
        ~50ms, 无缓存."""
        ...

    async def _resolve_ext_id(self, browser: Any) -> str:
        """从 chrome.management.getAll() 匹配 name/description.contains(match_keyword).
        找不到 → raise ExtensionNotFoundError."""
        ...

    async def start_recording(
        self,
        driver: Any,
        url: str,
        duration_sec: int,
        post_buffer_sec: int = 30,
    ) -> str:
        """FR-2.24: 触发扩展开始录制, 轮询 bg_page.url 直到 editor.html?id=<audio_id>.
        Returns: audio_id (extension-assigned numeric string)."""
        ...

    async def click_download(
        self,
        driver: Any,
        audio_id: str,
        ext_id: str,
        save_dir: Path | None = None,
        timeout_sec: int = 180,
    ) -> Path:
        """FR-2.25: 直接 goto editor.html?id=<audio_id>, 注册 download 监听,
        点下载按钮, save_as <save_dir>/<audio_id>.webm.
        超时 → raise DownloadTimeoutError."""
        ...
```

**Key design rules:**
- Stateless (FR-2.21): no module-level singleton. Caller creates `TabAudioRecorder(cfg)` per process.
- `match_keyword` from `cfg.extension.tab_audio_recorder.match_keyword` (default `"tab audio"`).
- macOS TCC: doesn't require screen recording permission (Tab Audio Recorder uses `chrome.tabCapture`, not `getUserMedia`).
- All async (playwright async API).
- Defensive: any `chrome.management.getAll` failure → return `"not_installed"`; never raises to caller.

**Tests (TDD):**
- `test_probe_status_enabled` — mock `chrome.management.getAll` returning `[{name: "Tab Audio Recorder", enabled: True, id: "abc"}]` → `"enabled"`
- `test_probe_status_disabled` — same but `enabled=False` → `"disabled"`
- `test_probe_status_not_installed` — empty list → `"not_installed"`
- `test_probe_status_timeout` — `getAll` raises → `"not_installed"`
- `test_resolve_ext_id_match_keyword` — case-insensitive match on name OR description
- `test_start_recording_polls_url` — mock bg_page.url transitions → returns audio_id from URL
- `test_click_download_registers_before_click` — verify download listener order
- `test_click_download_timeout` — 180s timeout → raises DownloadTimeoutError

### 3.2 `audio/source_factory.py` (FR-2.14路径①)

**Purpose:** Extract audio via `yt-dlp -x` for download-able URLs.

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AudioExtractionResult:
    audio_path: Path
    source: str  # "yt-dlp"
    duration_sec: int


class AudioSourceFactory:
    """yt-dlp 抽音频 (FR-2.14路径①)."""

    def __init__(
        self,
        save_dir: Path = Path("./logs/audio_raw"),
        audio_format: str = "wav",
        ffmpeg_postargs: str = "-ac 1 -ar 16000",
        simulate_timeout_sec: int = 30,
    ) -> None:
        ...

    def is_downloadable(self, url: str) -> bool:
        """FR-1.4: yt-dlp --simulate first to confirm download works.
        Returns: True/False. ~2-5s typical."""
        ...

    def extract(self, url: str, stem: str) -> AudioExtractionResult:
        """yt-dlp -x --audio-format wav <url> → <save_dir>/<stem>.wav.
        Returns: AudioExtractionResult with path + duration.
        Raises: subprocess.CalledProcessError on failure."""
        ...
```

**Key design rules:**
- `simulate` first → saves wasted disk if URL is not download-able.
- Filename: `<stem>.wav` where `stem = bvid` (or `local_<hash>` for non-B站 URLs).
- No ffmpeg re-encoding after extraction (faster-whisper reads .wav directly).
- Disk: ~60 MB/hour for wav, ≤ 1GB cap (FR-3.4).

**Tests:**
- `test_is_downloadable_yes` — mock subprocess return success
- `test_is_downloadable_no` — mock subprocess return non-zero
- `test_extract_creates_file` — verify `save_dir/<stem>.wav` written
- `test_extract_failure_raises` — subprocess fail → CalledProcessError

### 3.3 `audio/queue.py` (FR-2.27)

```python
import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioTask:
    audio_id: str  # 或 bvid for yt-dlp path
    audio_path: Path
    video_meta: dict  # title, bvid, duration_sec, group_id


class AudioQueue:
    """asyncio.Queue, 容量 10 (FR-2.27). 满则阻塞避免内存爆."""

    def __init__(self, max_size: int = 10) -> None:
        self._q: asyncio.Queue[AudioTask] = asyncio.Queue(maxsize=max_size)

    async def push(self, task: AudioTask) -> None:
        await self._q.put(task)  # 满了自动 await

    async def pull(self) -> AudioTask:
        return await self._q.get()
```

### 3.4 `audio/worker_pool.py` (FR-2.27)

```python
import asyncio
import logging
from typing import Any

from vla.audio.queue import AudioQueue, AudioTask


logger = logging.getLogger(__name__)


class WhisperWorkerPool:
    """N 个 worker 并发从 AudioQueue 拿任务, 调 StreamingTranscriber.

    Default 2 workers (FR-2.27 — Apple Silicon GPU single-card 上限).
    配置项: whisper.concurrent_workers.
    """

    def __init__(
        self,
        queue: AudioQueue,
        transcriber: Any,  # StreamingTranscriber (avoid circular import)
        concurrency: int = 2,
    ) -> None:
        ...

    async def run(self) -> None:
        """启动 N 个 worker, 阻塞. 外部 signal 停止时 graceful exit."""
        self._tasks = [
            asyncio.create_task(self._worker_loop())
            for _ in range(self.concurrency)
        ]
        await asyncio.gather(*self._tasks)

    async def _worker_loop(self) -> None:
        while True:
            task = await self.queue.pull()
            try:
                text = self.transcriber.transcribe(task.audio_path)
                # ... 后续走 quality check, success → cleanup, fail → audio_failed/
            except Exception as e:
                logger.error("worker failed: %s", e)
```

**Concurrency safety:**
- Same `audio_id` never processed twice (queue.guarantees FIFO + at-most-once consumption)
- `QuotaManager.add` atomic via `asyncio.Lock` (or use `current: int` since += on int is atomic in CPython)
- `probe_status` stateless, no lock

### 3.5 `capture/screen_capture.py` (FR-2.28)

```python
import asyncio
import subprocess
from pathlib import Path
from typing import Any


class ScreenCapture:
    """FR-2.28: 同步系统截图 (macOS screencapture -x, Windows PowerShell)."""

    def __init__(
        self,
        save_dir: Path = Path("./logs/screenshots"),
    ) -> None:
        ...

    async def capture_full_screen(self, save_path: Path) -> bool:
        """macOS: screencapture -x <save_path> (~0.3-0.5s, 需屏幕录制 TCC).
        Windows: PowerShell + System.Drawing (~1.5-3s, 无需特殊权限).
        Returns: True on success, False on failure (权限拒 / 超时)."""
        ...

    async def prepare_for_screenshot(self, page: Any) -> None:
        """FR-2.28.2a: 抢焦点 + 窗口归位.
        ① page.bring_to_front()
        ② page.evaluate("window.focus()")
        ③ page.evaluate("window.moveTo(0,0); window.resizeTo(screen.width, screen.height)")
        ④ asyncio.sleep(0.3)
        失败兜底: 部分截图 (partial=menu_bar_only)."""
        ...

    def write_index_entry(
        self,
        bvid: str,
        start_ts: float,
        end_ts: float,
        duration_estimate: int,
        partial_flags: list[str] | None = None,
    ) -> None:
        """FR-2.28.2e: logs/screenshots/index.jsonl 每行 {bvid, start_ts, end_ts,
        duration_estimate, partial_flags}. end_ts - start_ts ≈ duration_sec ±5s."""
        ...
```

**Failure semantics:** Any capture failure → log warning + skip that shot, do NOT block audio recording or video playback. Partial screenshot (only menu bar time, no video frame) still saved for audit (FR-2.28 partial_flags).

**Tests:**
- `test_capture_macos_uses_screencapture` — mock subprocess.run, verify `-x` flag passed
- `test_capture_windows_uses_powershell` — verify System.Drawing referenced
- `test_prepare_for_screenshot_brings_to_front` — verify order: bring_to_front → focus → moveTo/resizeTo → sleep
- `test_write_index_entry_appends_jsonl` — verify file format

---

## 4. Interface Contracts & Integration

### 4.1 Updated `PlatformAdapter.fetch_via_recording` contract

```python
class PlatformAdapter(Protocol):
    # ... existing fetch_api_subtitle, fetch_browser_subtitle ...

    def fetch_via_recording(
        self,
        driver: Any,
        url: str,
        duration_sec: int,
        *,
        audio_factory: "AudioSourceFactory | None" = None,
        tab_recorder: "TabAudioRecorder | None" = None,
    ) -> tuple[str, dict] | None:
        """策略 ③ (FR-2.14 v3): 音频二级降级.

        Default implementation:
        1. audio_factory.is_downloadable(url) → True?
           → audio_factory.extract(url, stem) → transcribe → return
        2. tab_recorder.probe_status(browser) → "enabled"?
           → tab_recorder.start_recording + click_download → audio_id → queue
        3. else → return None (主调度 → quality_skip)
        """
        ...
```

**Why keyword-only `audio_factory` / `tab_recorder`:** Lets `BilibiliAdapter` inherit default behavior without constructor injection.

### 4.2 `AudioTranscriber` Protocol

Already exists in `subtitle/browser_record.py` (duck-typed). `StreamingTranscriber` implements it. **Action:** Move Protocol to `transcribe/__init__.py` (canonical home), keep `browser_record.py` re-export for back-compat.

### 4.3 `VideoLearningAgent` integration points

Current `text_provider` returns `(text, source, audio_path)`. After FR-2.27:

```python
# New: text_provider is async, may queue audio instead of returning text immediately
TextProvider = Callable[[VideoTask], Awaitable[SubtitleResult]]

# Worker pool runs concurrently, calls into VideoLearningAgent's quality-check pipeline
# Either via callback or by polling transcribed_dir
```

**Two viable designs:**
1. **Polling model (simpler):** Worker writes to `transcribed_dir` directly; `VideoLearningAgent` already reads disk via `TranscriptionLog`. Add 6h quota trigger to scan `transcribed_dir`.
2. **Callback model (more coupled):** Worker takes a callback `(audio_path, text) -> None`; `VideoLearningAgent` handles quality-check immediately.

**Recommendation:** Polling model — keeps worker pool decoupled, matches existing `transcribed_dir` design.

---

## 5. Sequencing / Phasing

Six execution plans, ordered by dependency. Each plan follows the same TDD + subagent-driven-development pattern as R-01 through R-15.

### Plan F2-1: `audio/source_factory.py` (FR-2.14路径①)
**New module:** `src/vla/audio/__init__.py`, `audio/source_factory.py`
**Tests:** `tests/test_audio_source_factory.py` (4 tests)
**Tasks:** 3 (test → impl → integrate with `BilibiliAdapter.fetch_via_recording` as default fallback)
**Dependency:** none
**Risk:** low (mechanical subprocess wrapping)

### Plan F2-2: `subtitle/tab_audio_recorder.py` (FR-2.21/2.24/2.24a/2.25)
**New module:** `src/vla/subtitle/tab_audio_recorder.py` + 3 exception classes
**Tests:** `tests/test_tab_audio_recorder.py` (8 tests, async)
**Tasks:** 4 (test → impl → `probe_status` → `start_recording` → `click_download`)
**Dependency:** none (but uses playwright async API)
**Risk:** high (Chrome extension integration — needs extensive mocking)

### Plan F2-3: `audio/queue.py` + `audio/worker_pool.py` (FR-2.27)
**New module:** `src/vla/audio/queue.py`, `audio/worker_pool.py`
**Tests:** `tests/test_audio_queue.py` (3 tests), `tests/test_audio_worker_pool.py` (4 tests)
**Tasks:** 3 (queue + worker pool + integration smoke)
**Risk:** low (asyncio patterns)

### Plan F2-4: `capture/screen_capture.py` (FR-2.28)
**New module:** `src/vla/capture/__init__.py`, `capture/screen_capture.py`
**Tests:** `tests/test_screen_capture.py` (4 tests, includes macOS/Win path mocking)
**Tasks:** 3 (test → impl + `prepare_for_screenshot` + `write_index_entry`)
**Risk:** medium (TCC permissions, platform-specific commands)

### Plan F2-5: Update `PlatformAdapter.fetch_via_recording` default implementation (FR-2.14)
**Modify:** `src/vla/subtitle/platform_adapter.py` (default impl), `src/vla/subtitle/bilibili_adapter.py`, `src/vla/subtitle/internal_site_adapter.py`, `src/vla/subtitle/strategy.py`
**Tests:** `tests/test_platform_adapter.py` (add 3 tests for default impl)
**Tasks:** 3 (default impl + BilibiliAdapter integration + e2e smoke)
**Risk:** medium (touches multiple files; backward compat with `browser_record.py`)

### Plan F2-6: Video-recording → audio-only migration (FR-2.14 cleanup)
**Modify:** `src/vla/subtitle/browser_record.py` (remove video recording, keep audio path)
**Modify:** `src/vla/main.py` (remove VideoSourceRecordConfig references if any remain)
**Modify:** `src/vla/config.py` (remove `record` block if any)
**Tests:** `tests/test_browser_record.py` (rewrite to test audio-only path)
**Tasks:** 4 (audit + remove + tests + verify nothing else references)
**Risk:** high (touches largest file; possible regressions in dirty-tree tests)

### Plan F2-7 (parallel with F2-5): Final FR-2/FR-3 regression + release notes
**Tests:** all
**Acceptance:** 6 plans complete + clean grep audit
**Tasks:** standard SDD final-regression pattern
**Risk:** low

---

## 6. Open Questions (need user input)

### Q1. Polling vs callback for for worker pool?
- **Polling** (recommended): worker writes to `transcribed_dir`; `VideoLearningAgent` polls disk. Lower coupling.
- **Callback**: worker calls `video_learning_agent.handle_transcribed(text, ...)`. Lower latency.

### Q2. Does `BrowserRecorder` survive as a wrapper, or get deleted?
- **Survive as wrapper** (recommended): keeps existing tests; `record_and_transcribe()` delegates to `audio/source_factory.extract()`.
- **Delete entirely**: cleaner; forces tests to update.

### Q3. Should `PluginStatus` (VideoTrans tracker) coexist with `TabAudioRecorder.probe_status`?
- **Coexist** (recommended): `PluginStatus` is session-level state for legacy VideoTrans; `probe_status` is stateless per-call. Different concerns, different scopes.
- **Unify**: make `PluginStatus` track Tab Audio Recorder too. But loses the stateless-per-call guarantee from FR-2.21.

### Q4. FR-2.15c (Level 4 LLM cleanup) — implement now or defer?
- **Defer** (recommended): `quality/refiner.py` already exists per R-10 with bidirectional config; `postprocess.py` Level 1 is sufficient for MVP.
- **Implement now**: write `RefinementResult` dataclass + parser + tests. ~1 plan.

### Q5. FR-2.28 (screenshots) — actually needed for MVP?
- **Skip** (recommended): adds platform-specific code paths (macOS TCC, Windows PowerShell); high risk for low value (audit trail only).
- **Implement now**: as specified.

### Q6. FR-3.9 (Level 4 cloud LLM cleanup) — already wired?
- Looking at `quality/refiner.py` (R-10 modified), the bidirectional config supports `refine_enabled=true`. Tests exist (`tests/test_refiner.py`).
- **Probably already done** — verify with audit before planning.

---

## 7. Acceptance Criteria

After all F2-* plans execute:

1. **Functional:**
   - `BilibiliAdapter.fetch_via_recording(url, duration_sec)` returns SubtitleResult for B站 URLs via yt-dlp audio (path ①)
   - Unknown-platform URLs fall through to `TabAudioRecorder` (path ②)
   - `TabAudioRecorder.probe_status(browser)` returns correct 3-state per call (no singleton)
   - Multiple concurrent audio tasks processed by WhisperWorkerPool
   - Screenshots captured (if FR-2.28 in scope)

2. **Test gates:**
   - All existing tests still pass (491 baseline + new F2-* tests)
   - `vla doctor` green
   - Real Bilibili smoke (if network available)

3. **Audit gates:**
   - `grep -rn "ffmpeg.*screen\|screencapture" src/vla/subtitle/browser_record.py` returns 0 (video-recording deleted)
   - `grep -rn "def probe_status\|chrome.management.getAll" src/vla/subtitle/` confirms `TabAudioRecorder` is the SSOT
   - `grep -rn "WhisperWorkerPool\|AudioQueue" src/vla/audio/` confirms new modules exist

---

## 8. Out of Scope

- `requirements.md` updates (FR SSOT stays unchanged)
- `implementation-plan.md` updates (frozen per user decision)
- `scripts/` directory cleanup
- Tab Audio Recorder extension development (third-party; we only trigger it)
- Real-network testing in CI (kept manual via `scripts/e2e_real_bilibili.py`)
- Internal site adapter real implementation (stays as `NotImplementedError` stub per FR-2.18)

---

**Next step:** User reviews this doc → approves → I generate 7 implementation plan files (F2-1 through F2-7) via writing-plans skill → execute via subagent-driven-development (same as R-*).