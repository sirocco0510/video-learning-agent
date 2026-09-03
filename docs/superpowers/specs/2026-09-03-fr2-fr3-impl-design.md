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
| ~~`BrowserRecorder` (video-recording)~~ | ~~`src/vla/subtitle/browser_record.py`~~ | ❌ **TO DELETE** | dirty tree — per FR-2.14 v3 video-recording removed; **user ruling (2026-09-03): BrowserRecorder 与 VideoTrans 一并删,不再保留 wrapper** |
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

**SubtitleRefiner** (`src/vla/quality/refiner.py`, 275 lines, R-10 era):
- `SubtitleRefiner(config, llm=None).refine(text, title="") -> RefinementResult`
- 4 prompt components: system prompt (繁简统一 + 同音字 + 碎片 + 段落) + user template (title + char_count + text)
- Length skip: `len(text) > refine_max_chars` → returns original + notes
- LLM failure fallback: returns original + notes (no exception, 主流程不中断)
- `RefinementResult` / `Correction` dataclasses in `vla.models`
- `write_cleaned_transcript(cleaned_path, result)` helper for `<stem>.refined.txt` format
- `refine_enabled=false` default (FR-3.9: 省钱 + 主流程 Level 1 足够)
- **MISSING:** pipeline wiring (no caller invokes `refine()` between postprocess and quality check) — this is F2-6's job

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

**Decision (user ruling 2026-09-03): Delete entirely.** VideoTrans 已废弃,`BrowserRecorder.record_and_transcribe()` 的 video-recording 路径不再需要 wrapper。

**Action items:**
- Delete `src/vla/subtitle/browser_record.py` entirely (file + class)
- Delete `tests/test_browser_record.py` (50 tests will be replaced by:
  - `tests/test_audio_source_factory.py` (4 tests for path ①)
  - `tests/test_tab_audio_recorder.py` (8 tests for path ②)
  - `tests/test_screen_capture.py` (4 tests for FR-2.28)
- `Strategy.fetch_via_recording` calls `AudioSourceFactory` (path ①) directly; on None falls through to `TabAudioRecorder` (path ②) directly. No intermediate wrapper.
- `tests/test_probe_strategy.py` (8 tests) needs to be re-anchored — `BrowserRecorder` is the only consumer of `ProbeRegistry`. New consumer: `BilibiliAdapter.fetch_via_recording` (via `TabAudioRecorder`'s `_resolve_ext_id` callsite?). TBD during plan F2-6.

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

### 3.6 FR-2.15c — Level 4 云端 LLM 字幕语义清理 (verify wiring)

**Status:** `SubtitleRefiner` class already implemented in `src/vla/quality/refiner.py` (275 lines, R-10 era). What needs design = the **pipeline wiring** between `clean_transcript` (Level 1) → `SubtitleRefiner.refine` (Level 4) → `QualityChecker.check`.

**What's in `refiner.py` (already done):**
- `SubtitleRefiner(config, llm=None).refine(text, title="") -> RefinementResult`
- `RefinementResult` dataclass: `original_text, cleaned_text, corrections, notes, model, prompt_tokens, completion_tokens`
- `Correction` dataclass: `original, fixed, reason`
- `write_cleaned_transcript(cleaned_path, result)` helper (FR-2.15b format)
- Failure fallback: 任何 LLM 异常/解析失败/超长 → 返回原 text + notes(主流程不中断)
- 默认 `refine_enabled=false`(省钱)

**What's MISSING (design gap):**

1. **Pipeline orchestration module** — currently nothing calls `SubtitleRefiner.refine()` between Level 1 and QualityChecker. Need:
   ```python
   # src/vla/quality/pipeline.py (NEW)
   class SubtitlePipeline:
       """FR-2.15c / FR-3.9 字幕清理管线:Level 1 → Level 4 → 质量门控."""

       def __init__(self, config, postprocessor, refiner, checker): ...

       async def run(
           self,
           text: str,
           title: str,
           duration_sec: int,
           model_size: str,
           output_dir: Path,
           stem: str,
       ) -> PipelineResult:
           # 1. Level 1 (always on): clean_transcript → write .cleaned.txt
           cleaned_text, post_stats = self.postprocessor.clean_transcript(text)

           # 2. Level 4 (optional): SubtitleRefiner.refine → write .refined.txt
           refined_text = cleaned_text
           refine_result = None
           if self.refiner.enabled:
               refine_result = self.refiner.refine(cleaned_text, title=title)
               if refine_result.cleaned_text != cleaned_text:
                   refined_text = refine_result.cleaned_text
                   write_cleaned_transcript(output_dir / f"{stem}.refined.txt", refine_result)

           # 3. QualityChecker on .refined.txt (or .cleaned.txt if no refine)
           quality = self.checker.check(refined_text, title, duration_sec, model_size)

           return PipelineResult(
               cleaned_text=cleaned_text,
               refined_text=refined_text,
               quality=quality,
               post_stats=post_stats,
               refine_result=refine_result,
           )
   ```

2. **File output convention** — FR-3.8/3.9 already declares `<bvid>.transcript.txt` (raw) + `<bvid>.cleaned.txt` (Level 1) + `<bvid>.refined.txt` (Level 4). Verify these filenames match `transcribe/streaming.py` and `quality/refiner.py::write_cleaned_transcript`.

3. **`vla doctor` validation** — needs to:
   - Verify `refine_max_chars` config present
   - Verify `llm.refine_model` resolves (fallback chain)
   - Verify `write_cleaned_transcript` is importable (smoke test)

4. **Telemetry/log hook** — `RefinementResult.notes` should propagate to `log/transcribed_file.py` audit trail.

**Configuration (already in `VLAConfig.quality_check`):**
```yaml
quality_check:
  refine_enabled: false  # FR-2.15c master switch
  refine_model: null     # null → fallback to llm.quality_model
  refine_max_chars: 6000 # 超过这个长度的文本跳过 LLM
  refine_max_output_tokens: 2000
  min_score_to_pass: 70
```

**Tests (verifying wiring, not new refiner logic):**
- `test_pipeline_runs_level1_only_by_default` — `refine_enabled=false` → no LLM call, `refined_text == cleaned_text`
- `test_pipeline_runs_level4_when_enabled` — mock LLMClient.complete → `refine_result is not None`, `refined.txt` written
- `test_pipeline_quality_check_uses_refined` — after Level 4, QualityChecker receives `refined_text` not `cleaned_text`
- `test_pipeline_refiner_failure_continues` — mock LLMClient raises → pipeline continues with `cleaned_text`, `quality` still runs
- `test_pipeline_writes_all_three_files` — `.transcript.txt` + `.cleaned.txt` + `.refined.txt` all exist after run
- `test_pipeline_long_text_skips_llm` — text > `refine_max_chars` → skip Level 4, use Level 1

### 3.7 FR-2.28 — 视频开头 + 末尾双截图 (NEW MODULE)

**Status:** Completely unimplemented. Spec (requirements.md:194-198) is detailed; the challenge is the **4-phase trigger** that coordinates page state + system screenshot + audio recording start.

**Architecture:**

```
┌────────────────────────────────────────────────────────────────┐
│ PHASE A: 开头截图(前置硬约束)                                  │
├────────────────────────────────────────────────────────────────┤
│ page.goto(url)                                                 │
│   ↓                                                            │
│ page.evaluate("video.currentTime=0; video.pause();            │
│                 video.requestFullscreen()")                    │
│   ↓                                                            │
│ await sleep(2)                  # 等全屏动画                  │
│   ↓                                                            │
│ await notifier.notify_info("准备截图,请稍候")  # B 级(FR-2.28.2d)│
│   ↓                                                            │
│ await screen_capture.prepare_for_screenshot(page)             │
│   ↓ (FR-2.28.2a: bring_to_front + focus + 归位 + sleep(0.3))  │
│                                                                │
│   ↓ ★ 此时才启动音频录制(FR-2.28.2b 前置硬约束)             │
│   ↓ (返回 audio_id 后续异步处理)                              │
│   ↓                                                            │
│ screen_capture.capture_full_screen(save_dir /                 │
│                                     f"{audio_id}_start.png")  │
│   ↓ ~0.3-0.5s                                                  │
│ record_start_ts = now()                                         │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ PHASE B: 后台监听(asyncio task,每 1s poll currentTime)        │
├────────────────────────────────────────────────────────────────┤
│ while not done:                                                │
│   await asyncio.sleep(1)                                       │
│   current_time = await page.evaluate("video.currentTime")    │
│   if current_time >= duration_sec - 33:                        │
│       break                                                    │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ PHASE C: 末尾截图(同步,非前置 — 录制仍在进行)                │
├────────────────────────────────────────────────────────────────┤
│ await screen_capture.prepare_for_screenshot(page)             │
│   ↓                                                            │
│ await page.evaluate("video.pause();                           │
│                       video.currentTime = duration_sec - 30") │
│   ↓                                                            │
│ await sleep(0.5)               # 等画面稳定                    │
│   ↓                                                            │
│ screen_capture.capture_full_screen(save_dir /                 │
│                                     f"{audio_id}_end.png")    │
│   ↓ ~0.3-0.5s                                                  │
│ record_end_ts = now()                                          │
│   ↓                                                            │
│ await page.evaluate("video.play()")  # 恢复播放                │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ PHASE D: 落盘审计(FR-2.28.2e)                                  │
├────────────────────────────────────────────────────────────────┤
│ index_entry = {                                                │
│   bvid: ...,                                                   │
│   start_ts: record_start_ts,                                   │
│   end_ts: record_end_ts,                                       │
│   duration_estimate: end_ts - start_ts,                        │
│   partial_flags: [],  # 或 ["menu_bar_only"] 等                │
│ }                                                              │
│ screen_capture.write_index_entry(index_entry)                  │
│   → logs/screenshots/index.jsonl (append)                      │
└────────────────────────────────────────────────────────────────┘
```

**Module: `src/vla/capture/__init__.py` + `src/vla/capture/screen_capture.py`**

```python
import asyncio
import platform
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any
import json


@dataclass(frozen=True)
class ScreenshotIndexEntry:
    bvid: str
    start_ts: float
    end_ts: float
    duration_estimate: int
    partial_flags: list[str]


class ScreenCapture:
    """FR-2.28: 视频开头 + 末尾双截图, 系统级菜单栏/任务栏时间.

    触发条件: screenshot.enabled=true + 视频走策略 ③ Whisper.
    """

    def __init__(
        self,
        save_dir: Path = Path("./logs/screenshots"),
        platform_name: str | None = None,
    ) -> None:
        self.save_dir = save_dir
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._platform = platform_name or platform.system()  # "Darwin" | "Windows"

    async def prepare_for_screenshot(self, page: Any) -> None:
        """FR-2.28.2a: 抢焦点 + 窗口归位.

        顺序:bring_to_front → window.focus → moveTo(0,0) + resizeTo → sleep(0.3).
        失败兜底:partial=menu_bar_only(只截到菜单栏时间,无视频画面).
        """
        try:
            await page.bring_to_front()
            await page.evaluate("window.focus()")
            await page.evaluate(
                "window.moveTo(0, 0); window.resizeTo(screen.width, screen.height);"
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning("⚠️ prepare_for_screenshot 部分失败: %s", e)
            # 不抛错 — 后续 capture 会写 partial_flags

    async def capture_full_screen(self, save_path: Path) -> bool:
        """FR-2.28: 同步系统截图.

        macOS:   screencapture -x <save_path>  (~0.3-0.5s, 需屏幕录制 TCC)
        Windows: PowerShell + System.Drawing    (~1.5-3s, 无需特殊权限)
        Returns: True 成功 / False 失败 (权限拒 / 超时).
        """
        if self._platform == "Darwin":
            return await self._capture_macos(save_path)
        elif self._platform == "Windows":
            return await self._capture_windows(save_path)
        else:
            logger.warning("⚠️ 不支持的平台: %s", self._platform)
            return False

    async def _capture_macos(self, save_path: Path) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "screencapture", "-x", str(save_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode != 0:
                logger.warning("⚠️ screencapture 失败: %s", stderr.decode())
                return False
            return save_path.exists()
        except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
            logger.warning("⚠️ screencapture 异常: %s", e)
            return False

    async def _capture_windows(self, save_path: Path) -> bool:
        ps_script = f"""
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save("{save_path}", [System.Drawing.Imaging.ImageFormat]::Png)
"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-Command", ps_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            return proc.returncode == 0 and save_path.exists()
        except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
            logger.warning("⚠️ PowerShell 截图异常: %s", e)
            return False

    def write_index_entry(self, entry: ScreenshotIndexEntry) -> None:
        """FR-2.28.2e: logs/screenshots/index.jsonl append."""
        index_path = self.save_dir / "index.jsonl"
        with index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


class ScreenshotPhaseController:
    """FR-2.28: 4-phase 触发器.由 Strategy.fetch_via_recording 调用."""

    def __init__(
        self,
        screen_capture: ScreenCapture,
        notifier: "MacOSNotifierLike",
        bvid: str,
        duration_sec: int,
        save_dir: Path,
    ) -> None:
        self.capture = screen_capture
        self.notifier = notifier
        self.bvid = bvid
        self.duration_sec = duration_sec
        self.save_dir = save_dir

    async def phase_a_start(self, page: Any, audio_id: str) -> float:
        """PHASE A: 开头截图. 返回 record_start_ts (录音开始时间)."""
        await self.notifier.notify_info("准备截图,请稍候")  # FR-2.28.2d
        await self.capture.prepare_for_screenshot(page)
        start_path = self.save_dir / f"{audio_id}_start.png"
        await self.capture.capture_full_screen(start_path)
        return asyncio.get_event_loop().time()

    async def phase_b_poll_until_near_end(self, page: Any) -> None:
        """PHASE B: 后台 poll currentTime 到 duration_sec - 33."""
        while True:
            current = await page.evaluate("video.currentTime")
            if current >= self.duration_sec - 33:
                return
            await asyncio.sleep(1.0)

    async def phase_c_end(self, page: Any, audio_id: str) -> float:
        """PHASE C: 末尾截图. 返回 record_end_ts."""
        await self.capture.prepare_for_screenshot(page)
        await page.evaluate(
            f"video.pause(); video.currentTime = {self.duration_sec - 30};"
        )
        await asyncio.sleep(0.5)
        end_path = self.save_dir / f"{audio_id}_end.png"
        await self.capture.capture_full_screen(end_path)
        await page.evaluate("video.play()")  # 恢复播放
        return asyncio.get_event_loop().time()

    def phase_d_write_index(
        self, audio_id: str, start_ts: float, end_ts: float, partial_flags: list[str] | None = None
    ) -> None:
        """PHASE D: 落盘审计."""
        entry = ScreenshotIndexEntry(
            bvid=self.bvid,
            start_ts=start_ts,
            end_ts=end_ts,
            duration_estimate=int(end_ts - start_ts),
            partial_flags=partial_flags or [],
        )
        self.capture.write_index_entry(entry)
```

**Platform TCC handling:**
- macOS 首次 `screencapture` 会弹"屏幕录制"权限请求(TCC)
- 用户拒绝 → `capture_full_screen` returns False → `partial_flags=["permission_denied"]`
- `vla doctor` 主动 pre-warm `screencapture` 测试权限(避免首个视频才发现)

**`vla doctor` 验证 (FR-2.28.2c):**
- 在 `doctor` 阶段创建临时 `<video>` 元素 + `requestFullscreen()` + 立即 `document.exitFullscreen()`
- 验证 Promise resolve 不报错 → 提前暴露 fullscreen 权限问题

**Integration with `WhisperWorkerPool` worker:**
- Worker 调用 `ScreenshotPhaseController.phase_a_start(page, audio_id)` BEFORE `TabAudioRecorder.start_recording`
- Worker 调用 `phase_c_end(page, audio_id)` AFTER `TabAudioRecorder.start_recording` returns
- Worker 调用 `phase_d_write_index(...)` AFTER `click_download` returns audio_path

**Tests:**
- `test_capture_macos_uses_screencapture_x` — mock subprocess, verify `-x` flag
- `test_capture_windows_uses_powershell` — verify System.Drawing referenced
- `test_capture_returns_false_on_failure` — subprocess exit 1 → False
- `test_capture_returns_false_on_timeout` — subprocess hangs → False
- `test_prepare_for_screenshot_order` — verify bring_to_front → focus → moveTo → sleep
- `test_prepare_for_screenshot_continues_on_partial_failure` — focus() raises → still proceeds
- `test_phase_a_emits_b_level_notification` — notifier.notify_info called with correct text (FR-2.28.2d)
- `test_phase_b_polls_until_threshold` — mock currentTime progression → exit loop at duration-33
- `test_phase_c_pauses_then_resumes` — verify video.pause/play sequence
- `test_phase_d_writes_index_jsonl` — verify file format + append semantics
- `test_phase_controller_partial_failure_continues` — capture fails → index entry has partial_flags
- `test_doctor_validates_requestfullscreen` — mock page.evaluate, verify doctor pre-warm succeeds

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
        audio_factory: "AudioSourceFactory",       # REQUIRED: path ①
        tab_recorder: "TabAudioRecorder",          # REQUIRED: path ②
        transcriber: "AudioTranscriber",           # REQUIRED: Whisper
        screenshot_controller: "ScreenshotPhaseController | None" = None,  # FR-2.28
    ) -> tuple[str, dict] | None:
        """策略 ③ (FR-2.14 v3): 音频二级降级.

        Default implementation:
        1. audio_factory.is_downloadable(url) → True?
           → audio_factory.extract(url, stem) → transcriber.transcribe → return
        2. tab_recorder.probe_status(browser) → "enabled"?
           → screenshot_controller.phase_a_start(page, audio_id)  # FR-2.28 前置
           → tab_recorder.start_recording + click_download → audio_path
           → transcriber.transcribe(audio_path) → return
           → screenshot_controller.phase_c_end + phase_d  # FR-2.28 末尾 + 审计
        3. else → return None (主调度 → quality_skip)
        """
        ...
```

**Why REQUIRED kwargs:** Per user ruling (2026-09-03), `BrowserRecorder` is deleted entirely — no wrapper fallback. `PlatformAdapter` adapters must inject both path ① and path ② at construction. `screenshot_controller` optional because FR-2.28 is opt-in via `screenshot.enabled=true`.

### 4.2 `AudioTranscriber` Protocol

`AudioTranscriber` was previously defined (duck-typed) in `subtitle/browser_record.py`. After deletion:
- Move Protocol to `transcribe/__init__.py` (canonical home)
- `StreamingTranscriber` already implements it
- No back-compat shim needed (BrowserRecorder is the only consumer, also deleted)

```python
# src/vla/transcribe/__init__.py
class AudioTranscriber(Protocol):
    """Subtitle text provider via Whisper.

    输入:audio file path (post AudioSourceFactory.extract / TabAudioRecorder.click_download)
    输出:transcript text (Level 1 postprocess already applied internally)
    """
    def transcribe(self, audio_path: Path) -> str: ...
    def cleanup(self, audio_path: Path) -> None: ...
```

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

### 4.4 `SubtitlePipeline` integration (FR-2.15c)

```python
# src/vla/quality/pipeline.py (NEW in F2-8)
class SubtitlePipeline:
    """Level 1 (本地) → Level 4 (云端 LLM,可选) → QualityChecker."""

    def __init__(
        self,
        config: VLAConfig,
        postprocessor: PostProcessor,        # from transcribe.postprocess
        refiner: SubtitleRefiner | None,     # None if refine_enabled=false and no LLM
        checker: QualityChecker,
    ) -> None: ...

    async def run(
        self,
        text: str,
        title: str,
        duration_sec: int,
        model_size: str,
        output_dir: Path,
        stem: str,
    ) -> PipelineResult: ...
```

**Called by:** `VideoLearningAgent` after Whisper returns text, BEFORE cleanup decision (transcribe vs delete audio).

**File output (FR-3.8/3.9 contract):**
- `<stem>.transcript.txt` — raw Whisper output (existing, written by `StreamingTranscriber`)
- `<stem>.cleaned.txt` — Level 1 (postprocess) output (NEW — written by `SubtitlePipeline.run`)
- `<stem>.refined.txt` — Level 4 (LLM) output (NEW — written by `SubtitlePipeline.run` when `refine_enabled=true`)
- Quality score → `transcribed_dir/index.jsonl` (existing)

### 4.5 `ScreenshotPhaseController` integration (FR-2.28)

Triggered by `PlatformAdapter.fetch_via_recording` ONLY on path ② (Tab Audio Recorder fallback — not on path ① yt-dlp which has no browser interaction).

```python
# Inside fetch_via_recording path ②:
async def fetch_via_recording_path_2(driver, url, duration_sec, tab_recorder, transcriber, screenshot):
    audio_id = await tab_recorder.start_recording(driver, url, duration_sec)

    # FR-2.28 PHASE A: 开头截图(前置)
    start_ts = await screenshot.phase_a_start(driver, audio_id) if screenshot else 0.0

    audio_path = await tab_recorder.click_download(driver, audio_id)

    # FR-2.28 PHASE B+C: 后台 poll + 末尾截图(并发)
    if screenshot:
        asyncio.create_task(screenshot.phase_b_then_c(driver, audio_id, duration_sec))
        # 注意:audio 下载 → 立即 transcribe → phase_c 异步进行

    text = transcriber.transcribe(audio_path)

    # FR-2.28 PHASE D: 落盘审计(phase_b_then_c 完成后)
    if screenshot:
        end_ts = ...  # from phase_c completion
        screenshot.phase_d_write_index(audio_id, start_ts, end_ts)

    return text, {"source": "tab_recorder", "audio_id": audio_id}
```

**Why async phase_b+c:** Audio download + transcribe can run in parallel with the screenshot phase B (polling) and phase C (end capture). `phase_d_write_index` must wait for phase C completion (synchronous barrier).

---

## 5. Sequencing / Phasing

**Nine execution plans**, ordered by dependency. Each plan follows the same TDD + subagent-driven-development pattern as R-01 through R-15. F2-1 through F2-4 are independent and can be parallelized if resources allow; F2-5+ are sequential.

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

### Plan F2-4: `capture/screen_capture.py` base (FR-2.28 part 1)
**New module:** `src/vla/capture/__init__.py`, `capture/screen_capture.py` (ScreenCapture class only)
**Tests:** `tests/test_screen_capture.py` (4 tests for ScreenCapture: macOS/Win path mocking, timeout, partial failure)
**Tasks:** 3 (test → impl + `prepare_for_screenshot` + `write_index_entry`)
**Risk:** medium (TCC permissions, platform-specific commands)
**Output:** `ScreenCapture` + `prepare_for_screenshot` + `write_index_entry`; **no 4-phase trigger yet** (that comes in F2-7).

### Plan F2-5: `capture/screenshot_phase_controller.py` (FR-2.28 part 2)
**New module:** `src/vla/capture/screenshot_phase_controller.py` (ScreenshotPhaseController + ScreenshotIndexEntry)
**Tests:** `tests/test_screenshot_phase_controller.py` (8 tests: phase A/B/C/D, async poll, B-level notifier hook, partial failure → index entry)
**Tasks:** 4 (PHASE A test → impl → PHASE B+C test → impl → PHASE D test → impl → doctor pre-warm test → impl)
**Dependency:** F2-4 (ScreenCapture base)
**Risk:** medium (page.evaluate sequence correctness, asyncio cancellation semantics)

### Plan F2-6: `quality/pipeline.py` (FR-2.15c wiring)
**New module:** `src/vla/quality/pipeline.py` (SubtitlePipeline orchestrator)
**Modify:** `src/vla/quality/__init__.py` (export SubtitlePipeline)
**Modify:** `src/vla/main.py` (call `SubtitlePipeline.run()` between Whisper and quality decision)
**Tests:** `tests/test_pipeline.py` (6 tests for Level 1 + Level 4 + QualityChecker wiring)
**Tasks:** 3 (test → impl → main.py integration)
**Dependency:** none (refiner.py already exists per R-10)
**Risk:** low (existing refiner is already 275 lines; just wire it)

### Plan F2-7: `PlatformAdapter.fetch_via_recording` integration (FR-2.14)
**Modify:** `src/vla/subtitle/platform_adapter.py` (default impl + Protocol signature with REQUIRED kwargs)
**Modify:** `src/vla/subtitle/bilibili_adapter.py` (inject audio_factory + tab_recorder + transcriber + screenshot_controller)
**Modify:** `src/vla/subtitle/internal_site_adapter.py` (same)
**Modify:** `src/vla/subtitle/strategy.py` (call updated default impl)
**Tests:** `tests/test_platform_adapter.py` (add 4 tests: path ①, path ②, screenshot integration, partial failure)
**Dependency:** F2-1 + F2-2 + F2-5 (audio_factory, tab_recorder, screenshot_controller all exist)
**Risk:** medium (touches multiple files)

### Plan F2-8: Delete `BrowserRecorder` (FR-2.14 cleanup)
**Delete:** `src/vla/subtitle/browser_record.py` (entire file)
**Delete:** `tests/test_browser_record.py` (entire file — 50 tests replaced by F2-1/F2-2/F2-5 tests)
**Delete:** `src/vla/subtitle/probes/` if `BrowserRecorder` was sole consumer (TBD during plan)
**Modify:** any caller still references `BrowserRecorder` (R-15 already wrapped `_safe_close_page` in owners; verify with grep)
**Modify:** `src/vla/main.py` (remove VideoSourceRecordConfig references if any remain)
**Modify:** `src/vla/config.py` (remove `record` block if any)
**Tests:** all (verify 491 baseline + new F2-* tests still pass)
**Tasks:** 4 (audit + delete + tests + verify)
**Dependency:** F2-7 (default impl in PlatformAdapter must be ready)
**Risk:** high (touches largest file; possible regressions in dirty-tree tests)

### Plan F2-9: Final FR-2/FR-3 regression + release notes
**Tests:** all
**Acceptance:** 8 plans complete + clean grep audit
**Tasks:** standard SDD final-regression pattern (pytest + doctor + grep audit + release notes)
**Risk:** low

---

## 6. Open Questions (need user input)

### Q1. Polling vs callback for for worker pool?
- **Polling** (recommended): worker writes to `transcribed_dir`; `VideoLearningAgent` polls disk. Lower coupling.
- **Callback**: worker calls `video_learning_agent.handle_transcribed(text, ...)`. Lower latency.

### Q2. ~~Does `BrowserRecorder` survive as a wrapper, or get deleted?~~
- ✅ **Resolved (user ruling 2026-09-03): Delete entirely.** No VideoTrans → no wrapper needed. F2-8 deletes `subtitle/browser_record.py` + `tests/test_browser_record.py` + `probes/` if sole consumer.

### Q3. Should `PluginStatus` (VideoTrans tracker) coexist with `TabAudioRecorder.probe_status`?
- **Coexist** (recommended): `PluginStatus` is session-level state for legacy VideoTrans; `probe_status` is stateless per-call. Different concerns, different scopes.
- **Unify**: make `PluginStatus` track Tab Audio Recorder too. But loses the stateless-per-call guarantee from FR-2.21.

### Q4. FR-2.15c (Level 4 LLM cleanup) — implement now or defer?
- ✅ **Resolved (user ruling 2026-09-03): Implement now via F2-6.** `SubtitleRefiner` class already exists in `quality/refiner.py` (275 lines); what's missing is the `SubtitlePipeline` orchestrator that wires `clean_transcript` → `refine` → `QualityChecker.check`. F2-6 plan covers this.

### Q5. FR-2.28 (screenshots) — actually needed for MVP?
- ✅ **Resolved (user ruling 2026-09-03): Implement now via F2-4 + F2-5.** Strong requirement (P0) per requirements.md:194. Split into 2 plans: F2-4 = base `ScreenCapture`; F2-5 = `ScreenshotPhaseController` 4-phase trigger.

### Q6. ~~FR-3.9 (Level 4 cloud LLM cleanup) — already wired?~~
- ✅ **Resolved**: Per FR-3.9 spec, `SubtitleRefiner.refine()` is the implementation; default `refine_enabled=false`. Wiring into pipeline is F2-6's job. No separate plan needed.

### Q7. Path ① → Path ② fallback — silent or loud?
- **Silent (recommended):** Path ① failure (yt-dlp returns 404 / non-zero) → automatically try path ②. User experience is "transcription works or doesn't"; no extra noise.
- **Loud:** Log warning + retry counter. Useful for debugging but adds noise.

### Q8. FR-2.28 doctor pre-warm (FR-2.28.2c) — block or warn on permission deny?
- **Warn (recommended):** `vla doctor` calls `requestFullscreen()` once; if TCC denies, print warning + suggest fix path, but don't exit non-zero. User can proceed.
- **Block:** Exit non-zero; require TCC grant before any video processing. More defensive but breaks developer workflow when TCC not yet granted.

### Q9. `PluginStatus` cleanup — delete entirely since VideoTrans gone?
- **Keep (recommended):** `PluginStatus` is generic "session-level plugin tracker" infrastructure; can be reused if any future plugin needs session-level state. Just remove VideoTrans registration.
- **Delete entirely:** One less module. `TabAudioRecorder.probe_status` is stateless; no need for session singleton.

---

## 7. Acceptance Criteria

After all F2-* plans execute:

1. **Functional:**
   - `BilibiliAdapter.fetch_via_recording(url, duration_sec)` returns SubtitleResult for B站 URLs via yt-dlp audio (path ①)
   - Unknown-platform URLs fall through to `TabAudioRecorder` (path ②)
   - `TabAudioRecorder.probe_status(browser)` returns correct 3-state per call (no singleton)
   - Multiple concurrent audio tasks processed by WhisperWorkerPool
   - Screenshots captured (PHASE A 开头 + PHASE C 末尾 + PHASE D index.jsonl)
   - `SubtitlePipeline` orchestrates Level 1 → Level 4 → QualityChecker; writes `.cleaned.txt` + `.refined.txt` + quality score

2. **Test gates:**
   - All existing tests still pass (491 baseline + new F2-* tests)
   - `vla doctor` green (incl. FR-2.28.2c `requestFullscreen()` pre-warm)
   - Real Bilibili smoke (if network available)

3. **Audit gates:**
   - `ls src/vla/subtitle/browser_record.py` → **file not found** (F2-8 deleted)
   - `ls tests/test_browser_record.py` → **file not found** (F2-8 deleted)
   - `grep -rn "class BrowserRecorder" src/vla/` → **0 matches**
   - `grep -rn "video.record\|ffmpeg.*screen" src/vla/` → **0 matches** (video-recording path removed)
   - `grep -rn "def probe_status\|chrome.management.getAll" src/vla/subtitle/` confirms `TabAudioRecorder` is the SSOT
   - `grep -rn "WhisperWorkerPool\|AudioQueue" src/vla/audio/` confirms new modules exist
   - `grep -rn "class ScreenCapture\|class ScreenshotPhaseController" src/vla/capture/` confirms FR-2.28 modules exist
   - `grep -rn "class SubtitlePipeline" src/vla/quality/` confirms FR-2.15c orchestrator exists
   - `grep -rn "SubtitlePipeline" src/vla/main.py` confirms wiring

---

## 8. Out of Scope

- `requirements.md` updates (FR SSOT stays unchanged)
- `implementation-plan.md` updates (frozen per user decision)
- `scripts/` directory cleanup
- Tab Audio Recorder extension development (third-party; we only trigger it)
- Real-network testing in CI (kept manual via `scripts/e2e_real_bilibili.py`)
- Internal site adapter real implementation (stays as `NotImplementedError` stub per FR-2.18)

---

**Next step:** User reviews this doc → approves → I generate 9 implementation plan files (F2-1 through F2-9) via writing-plans skill → execute via subagent-driven-development (same as R-*).