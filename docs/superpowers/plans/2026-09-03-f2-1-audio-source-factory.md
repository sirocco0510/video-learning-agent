# F2-1 — Audio Source Factory (yt-dlp audio extraction)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `AudioSourceFactory` class that extracts audio via `yt-dlp -x` for downloadable URLs (FR-2.14 path ①).

**Architecture:**
- New package: `src/vla/audio/__init__.py` (empty)
- New module: `src/vla/audio/source_factory.py` (`AudioExtractionResult` + `AudioSourceFactory`)
- New tests: `tests/test_audio_source_factory.py` (4 tests)

**Tech Stack:** Python 3.12, stdlib `subprocess`, `pathlib`, `dataclasses`

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md` §3.2 (FR-2.14 path ①)

## Global Constraints

- `tests/` is the test root; fixtures in `tests/fixtures/`
- TDD: write failing test → run → minimal impl → run → commit
- Subprocess wrapping uses `subprocess.run` with `check=True, capture_output=True, timeout=N`
- Filename convention: `<save_dir>/<stem>.wav` where stem = bvid or local hash
- No ffmpeg re-encoding after extraction (faster-whisper reads .wav directly)
- Disk cap: 256 GB machine, peak < 1 GB
- pydantic v2 for any models (AudioExtractionResult is `@dataclass` since simple value)
- LANG=en_US.UTF-8 prefix on all bash commands (Chinese path)

---

### Task 1: Create package skeleton + write 4 failing tests

**Files:**
- Create: `src/vla/audio/__init__.py` (empty package init)
- Create: `tests/test_audio_source_factory.py` (4 tests)

**Interfaces:**
- Consumes: `AudioSourceFactory(save_dir, audio_format, ffmpeg_postargs, simulate_timeout_sec)` constructor
- Consumes: `factory.is_downloadable(url) -> bool`
- Consumes: `factory.extract(url, stem) -> AudioExtractionResult`
- Produces: `AudioExtractionResult(audio_path: Path, source: str, duration_sec: int)`

- [ ] **Step 1: Create empty `audio/__init__.py`**

```python
# src/vla/audio/__init__.py
"""音频提取与队列(SSOT: spec 2026-09-03-fr2-fr3 §3.2 / §3.3)。"""
```

- [ ] **Step 2: Write 4 failing tests**

```python
# tests/test_audio_source_factory.py
"""AudioSourceFactory 测试(SSOT: spec 2026-09-03-fr2-fr3 §3.2)。

FR-2.14 path ①: yt-dlp -x --audio-format wav 下载可下载 URL 的音频。
- is_downloadable → yt-dlp --simulate
- extract → yt-dlp -x --audio-format wav → <save_dir>/<stem>.wav
- 失败 → 抛 subprocess.CalledProcessError
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vla.audio.source_factory import AudioExtractionResult, AudioSourceFactory


class TestIsDownloadable:
    def test_returns_true_when_simulate_succeeds(self, tmp_path: Path) -> None:
        """yt-dlp --simulate 返回 0 → True。"""
        factory = AudioSourceFactory(save_dir=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert factory.is_downloadable("https://example.com/watch?v=abc") is True
            mock_run.assert_called_once()
            args = mock_run.call_args.args[0]
            assert "yt-dlp" in args
            assert "--simulate" in args

    def test_returns_false_when_simulate_fails(self, tmp_path: Path) -> None:
        """yt-dlp --simulate 返回非 0 → False(不抛)。"""
        factory = AudioSourceFactory(save_dir=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert factory.is_downloadable("https://example.com/private") is False


class TestExtract:
    def test_creates_wav_file_in_save_dir(self, tmp_path: Path) -> None:
        """extract 后 <save_dir>/<stem>.wav 存在,返回 AudioExtractionResult。"""
        factory = AudioSourceFactory(save_dir=tmp_path)

        def fake_run(cmd, **kwargs):
            # 找到 yt-dlp 命令的 -o 参数对应的输出路径,创建空文件
            out_idx = cmd.index("-o")
            out_template = cmd[out_idx + 1]
            out_path = Path(out_template.replace("%(ext)s", "wav"))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"RIFF....")
            # ffprobe 用于 duration
            if "ffprobe" in cmd[0] or "ffprobe" in str(cmd):
                r = subprocess.CompletedProcess(cmd, 0, stdout=b"120.5", stderr=b"")
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with patch("subprocess.run", side_effect=fake_run):
            result = factory.extract("https://www.bilibili.com/video/BV1abc", "BV1abc")

        assert isinstance(result, AudioExtractionResult)
        assert result.source == "yt-dlp"
        assert result.audio_path == tmp_path / "BV1abc.wav"
        assert result.audio_path.exists()
        assert result.duration_sec == 120

    def test_raises_called_process_error_on_failure(self, tmp_path: Path) -> None:
        """yt-dlp -x 返回非 0 → 抛 subprocess.CalledProcessError(不静默吞)。"""
        factory = AudioSourceFactory(save_dir=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, ["yt-dlp"], stderr=b"404")
            with pytest.raises(subprocess.CalledProcessError):
                factory.extract("https://www.bilibili.com/video/BV_missing", "BV_missing")
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
LANG=en_US.UTF-8 uv run pytest tests/test_audio_source_factory.py -v
```

Expected: `ModuleNotFoundError: No module named 'vla.audio.source_factory'` (4 collection errors).

---

### Task 2: Implement `AudioSourceFactory` + `AudioExtractionResult`

**Files:**
- Create: `src/vla/audio/source_factory.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) AudioExtractionResult(audio_path: Path, source: str, duration_sec: int)`
- Produces: `class AudioSourceFactory` with `__init__`, `is_downloadable`, `extract`

- [ ] **Step 1: Implement module**

```python
# src/vla/audio/source_factory.py
"""yt-dlp 音频提取(SSOT: spec 2026-09-03-fr2-fr3 §3.2, FR-2.14 path ①)。

策略 ①:对可下载 URL(B站 / YouTube / 其他 yt-dlp 支持站点),用
`yt-dlp --simulate` 先探测,再用 `yt-dlp -x --audio-format wav` 抽音频。

输出:<save_dir>/<stem>.wav(单声道 16kHz PCM — 由 ffmpeg_postargs 控制),
faster-whisper 直接读 .wav,无需二次转码。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)

DEFAULT_SAVE_DIR = Path("./logs/audio_raw")
DEFAULT_AUDIO_FORMAT = "wav"
DEFAULT_FFMPEG_POSTARGS = "-ac 1 -ar 16000"  # 单声道 + 16kHz(Whisper 期望采样率)
DEFAULT_SIMULATE_TIMEOUT_SEC = 30
DEFAULT_EXTRACT_TIMEOUT_SEC = 600  # 10 分钟:60min 长视频 + 弱网络


@dataclass(frozen=True)
class AudioExtractionResult:
    """音频抽取结果(SSOT: spec §3.2)。"""

    audio_path: Path
    source: str  # 当前实现固定 "yt-dlp"(path ② TabAudioRecorder 会用别的 source)
    duration_sec: int


class AudioSourceFactory:
    """yt-dlp 抽音频工厂(SSOT: spec §3.2)。

    用法:
        factory = AudioSourceFactory(save_dir=Path("./logs/audio_raw"))
        if factory.is_downloadable(url):
            result = factory.extract(url, stem=bvid)
            transcriber.transcribe(result.audio_path)
    """

    def __init__(
        self,
        save_dir: Path = DEFAULT_SAVE_DIR,
        audio_format: str = DEFAULT_AUDIO_FORMAT,
        ffmpeg_postargs: str = DEFAULT_FFMPEG_POSTARGS,
        simulate_timeout_sec: int = DEFAULT_SIMULATE_TIMEOUT_SEC,
    ) -> None:
        self.save_dir = Path(save_dir)
        self.audio_format = audio_format
        self.ffmpeg_postargs = ffmpeg_postargs
        self.simulate_timeout_sec = simulate_timeout_sec

    # ---- 探测:URL 是否可下载? ----

    def is_downloadable(self, url: str) -> bool:
        """FR-1.4: yt-dlp --simulate 先验证 URL 可下载。

        返回 True / False,不抛。失败 = 不可下载(主调度走 path ② TabAudioRecorder)。
        ~2-5s typical;timeout 默认 30s。
        """
        try:
            proc = subprocess.run(
                ["yt-dlp", "--simulate", "--no-warnings", url],
                check=False,                  # 不抛,自己看 returncode
                capture_output=True,
                timeout=self.simulate_timeout_sec,
            )
            return proc.returncode == 0
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp --simulate 超时(>%ds):%s", self.simulate_timeout_sec, url)
            return False
        except FileNotFoundError:
            logger.error("yt-dlp 未安装或不在 PATH;path ① 不可用")
            return False

    # ---- 抽取:实际下载音频 ----

    def extract(self, url: str, stem: str) -> AudioExtractionResult:
        """yt-dlp -x --audio-format wav <url> → <save_dir>/<stem>.wav。

        Returns:
            AudioExtractionResult with path + duration。
        Raises:
            subprocess.CalledProcessError: yt-dlp 失败(网络 / 404 / 区域限制)。
        """
        self.save_dir.mkdir(parents=True, exist_ok=True)
        out_template = str(self.save_dir / f"{stem}.%(ext)s")
        cmd = [
            "yt-dlp",
            "-x",                          # 仅抽音频
            "--audio-format", self.audio_format,
            "--audio-quality", "0",        # best
            "--postprocessor-args", self.ffmpeg_postargs,
            "-o", out_template,
            "--no-warnings",
            url,
        ]
        logger.info("yt-dlp 抽音频:%s → %s", url, out_template)
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=DEFAULT_EXTRACT_TIMEOUT_SEC,
        )

        audio_path = self.save_dir / f"{stem}.{self.audio_format}"
        if not audio_path.exists():
            raise FileNotFoundError(
                f"yt-dlp 声称成功但 {audio_path} 不存在(可能格式不是 {self.audio_format})"
            )

        duration_sec = self._probe_duration(audio_path)
        return AudioExtractionResult(
            audio_path=audio_path,
            source="yt-dlp",
            duration_sec=duration_sec,
        )

    def _probe_duration(self, audio_path: Path) -> int:
        """用 ffprobe 拿时长(秒)。失败 fallback 到 0(主流程不阻塞)。"""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            logger.warning("ffprobe 未安装,duration_sec 退化为 0")
            return 0
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "json",
                    str(audio_path),
                ],
                check=True,
                capture_output=True,
                timeout=10,
            )
            data = json.loads(proc.stdout)
            return int(float(data["format"]["duration"]))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                KeyError, ValueError, json.JSONDecodeError) as e:
            logger.warning("ffprobe 解析时长失败:%s", e)
            return 0
```

- [ ] **Step 2: Run new tests**

```bash
LANG=en_US.UTF-8 uv run pytest tests/test_audio_source_factory.py -v
```

Expected: All 4 tests pass.

- [ ] **Step 3: Run full regression to confirm no breakage**

```bash
LANG=en_US.UTF-8 uv run pytest -v
```

Expected: All previously-passing tests still pass.

- [ ] **Step 4: Commit**

```bash
git add src/vla/audio/__init__.py src/vla/audio/source_factory.py tests/test_audio_source_factory.py
git commit -m "feat(audio): AudioSourceFactory for yt-dlp audio extraction (FR-2.14 path ①)"
```

---

### Task 3: `vla doctor` integration check

**Files:**
- Modify: `src/vla/cli.py` (add a doctor probe for yt-dlp availability)

**Interfaces:**
- Consumes: `AudioSourceFactory.is_downloadable()` (smoke-test yt-dlp binary)
- Produces: doctor report line showing yt-dlp availability

- [ ] **Step 1: Locate existing doctor probes**

```bash
LANG=en_US.UTF-8 grep -n "def.*doctor\|class.*Doctor\|yt-dlp" src/vla/cli.py
```

- [ ] **Step 2: Add yt-dlp availability probe to `cli.py`**

In the `doctor` command body (or a dedicated probe function), add a check that creates a `AudioSourceFactory()` and runs `is_downloadable("https://www.youtube.com/watch?v=dQw4w9WgXcQ")` with a 10s timeout, printing:

```
audio_source_factory: yt-dlp available (simulate OK in 2.3s)
```

or on failure:

```
audio_source_factory: yt-dlp MISSING — path ① 不可用,所有 URL 走 path ②
```

- [ ] **Step 3: Run doctor and verify new probe appears**

```bash
LANG=en_US.UTF-8 uv run vla doctor
```

Expected: New `audio_source_factory:` line in output; other probes still pass.

- [ ] **Step 4: Run full regression once more**

```bash
LANG=en_US.UTF-8 uv run pytest -v
```

Expected: All pass (doctor probe doesn't pollute test suite).

- [ ] **Step 5: Commit**

```bash
git add src/vla/cli.py
git commit -m "feat(cli): doctor probe for yt-dlp availability (AudioSourceFactory)"
```

---

## Acceptance Criteria

1. `tests/test_audio_source_factory.py` has 4 tests, all pass
2. `src/vla/audio/source_factory.py` exposes `AudioExtractionResult` + `AudioSourceFactory` matching design doc §3.2 exactly
3. `AudioSourceFactory.is_downloadable(url)` returns `True`/`False` without raising (timeout / missing yt-dlp / non-zero returncode)
4. `AudioSourceFactory.extract(url, stem)` writes `<save_dir>/<stem>.wav` and returns `AudioExtractionResult`
5. `uv run vla doctor` reports `audio_source_factory: yt-dlp ...` line
6. `grep -rn "class AudioSourceFactory\|class AudioExtractionResult" src/vla/audio/` → 2 matches
7. No new regressions in existing 491+ tests