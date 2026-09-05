# R.8 — Delete `_record_screen` + Screen-Recording Config

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove screen-recording fallback from `VideoSourceFactory`. Network failure = `DownloadError` (no fallback). User decision 2026-09-03: completely delete the ffmpeg + avfoundation path; this aligns with FR-8 ("recording deleted").

**Architecture:** `VideoSourceFactory.get()` becomes a 2-step flow: simulate → download. Failure raises `DownloadError`. The `VideoSourceRecordConfig` and its fields are removed from `config.py` and `vla.yaml`.

**Tech Stack:** subprocess, ffmpeg (only invoked as a subprocess in tests for cleanup verification)

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §A #5

## Global Constraints

- macOS screen recording permission no longer needed (R.8 后)
- Pipeline network failure → exit with error (no silent fallback to silent recording)
- `VideoSource.mode` enum still allows `"record"` for backward compat in tests, but no adapter writes that mode

---

### Task 1: Add failing tests for the new failure path

**Files:**
- Create (if missing): `tests/test_video_source.py`

**Interfaces:**
- `VideoSourceFactory.get(url, video_id, duration)` returns `VideoSource` on success, raises `DownloadError` on failure

- [ ] **Step 1: Write tests**

```python
# tests/test_video_source.py (new file or add to existing)
from pathlib import Path
from unittest.mock import patch

import pytest

from vla.source.video_source import DownloadError, VideoSourceFactory


@pytest.fixture
def factory(tmp_path: Path) -> VideoSourceFactory:
    from vla.log.transcription_log import TranscriptionLog
    log = TranscriptionLog(tmp_path)
    from vla.config import VLAConfig
    cfg = VLAConfig.from_yaml("config/vla.yaml")
    return VideoSourceFactory(tmp_dir=tmp_path, log=log, config=cfg)


class TestDownloadSuccess:
    def test_returns_download_video_source(self, factory: VideoSourceFactory, tmp_path: Path):
        fake_path = tmp_path / "video.mp4"
        with patch.object(factory, "_is_downloadable", return_value=True), \
             patch.object(factory, "_download", return_value=fake_path):
            source = factory.get("https://www.bilibili.com/video/BV1xxx", "BV1xxx", 100)
        assert source.mode == "download"
        assert source.path == fake_path
        assert source.duration_sec == 100.0


class TestDownloadFailure:
    def test_raises_download_error_on_simulate_fail(self, factory: VideoSourceFactory):
        with patch.object(factory, "_is_downloadable", return_value=False):
            with pytest.raises(DownloadError, match="无法下载"):
                factory.get("https://www.bilibili.com/video/BV1xxx", "BV1xxx", 100)

    def test_raises_download_error_on_subprocess_fail(self, factory: VideoSourceFactory):
        with patch.object(factory, "_is_downloadable", return_value=True), \
             patch.object(factory, "_download", side_effect=DownloadError("yt-dlp failed")):
            with pytest.raises(DownloadError, match="yt-dlp failed"):
                factory.get("https://www.bilibili.com/video/BV1xxx", "BV1xxx", 100)


class TestNoScreenRecording:
    def test_no_record_screen_method(self, factory: VideoSourceFactory):
        """Verify _record_screen method has been removed."""
        assert not hasattr(factory, "_record_screen"), "_record_screen should be deleted"
```

- [ ] **Step 2: Run tests to verify they fail (against current code)**

Run: `uv run pytest tests/test_video_source.py -v`
Expected: `TestNoScreenRecording::test_no_record_screen_method` fails (because the method still exists); success cases also fail because current `get()` always falls back to screen recording on simulate-fail

---

### Task 2: Remove `_record_screen` from `VideoSourceFactory`

**Files:**
- Modify: `src/vla/source/video_source.py:78-109, 113-123`

**Interfaces:**
- `get(url, video_id, expected_duration)` now: `_is_downloadable(url)` → `_download(url, video_id)` → return `VideoSource`; on any failure raise `DownloadError`

- [ ] **Step 1: Delete `_record_screen` method**

Delete lines 78-109 from `video_source.py` (the `_record_screen` method including its `subprocess.Popen` call).

- [ ] **Step 2: Rewrite `get()` to fail loudly**

Replace `get()` body:

```python
def get(self, url: str, video_id: str, expected_duration: int) -> VideoSource:
    """下载视频源。失败抛 DownloadError(上层 main_provider 不再 fallback 到 ffmpeg)。

    决策(2026-09-03 spec §A #5):
    - 屏幕录制路径已删除(FR-8)
    - 网络/yt-dlp 失败 = 报错退出,不静默走 ffmpeg 录屏
    """
    if not self._is_downloadable(url):
        raise DownloadError(f"无法下载(yt-dlp simulate failed): {url}")
    path = self._download(url, video_id)
    return VideoSource(
        path=path, mode="download", duration_sec=float(expected_duration)
    )
```

- [ ] **Step 3: Run new tests**

Run: `uv run pytest tests/test_video_source.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/vla/source/video_source.py tests/test_video_source.py
git commit -m "refactor(source): delete _record_screen; get() raises on failure"
```

---

### Task 3: Remove `VideoSourceRecordConfig`

**Files:**
- Modify: `src/vla/config.py:38-49` (the `VideoSourceRecordConfig` class and its reference in `VideoSourceConfig`)
- Modify: `config/vla.yaml` (the `video_source.record:` block)

**Interfaces:** (no change to public `VLAConfig`; just one fewer sub-config field)

- [ ] **Step 1: Delete the config class**

In `config.py`, delete the `VideoSourceRecordConfig` class entirely.

- [ ] **Step 2: Update `VideoSourceConfig`**

Replace `VideoSourceConfig` body to drop `record`:

```python
class VideoSourceConfig(BaseModel):
    prefer_download: bool
    download: VideoSourceDownloadConfig
```

- [ ] **Step 3: Remove `video_source.record:` from vla.yaml**

Open `config/vla.yaml`, delete the entire `record:` block under `video_source:`. The resulting structure:

```yaml
video_source:
  prefer_download: true
  download:
    format: "bestvideo[height<=360]+bestaudio/best[height<=360]/best"
```

- [ ] **Step 4: Verify `vla doctor` still passes**

Run: `uv run vla doctor`
Expected: All checks pass; `config/vla.yaml` validated

- [ ] **Step 5: Run full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/vla/config.py config/vla.yaml
git commit -m "refactor(config): remove VideoSourceRecordConfig (R.8)"
```

---

### Task 4: Audit no remaining `_record_screen` references

- [ ] **Step 1: Final grep**

Run: `grep -rn "_record_screen\|VideoSourceRecordConfig\|video_source.record\|screen_index\|audio_input" src/vla/ config/`
Expected: no output

- [ ] **Step 2: Final commit if straggler**

```bash
git status  # clean
```