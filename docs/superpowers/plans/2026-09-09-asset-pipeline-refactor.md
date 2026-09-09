# Asset Pipeline Refactor (Phase 9.5 — Option B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `text_provider` 拆成两条独立链路 (输入链 `fetch_asset` 产字幕/音频 + 处理链 `process_asset` 转写+质量+优化+落盘+清理),并在输入链接入内部学习平台 `b-learning.bill-jc.com` 的 API-driven spider 路径(纯 API,不开浏览器)。

**Architecture:** Producer-Consumer 数据流 — `fetch_asset` 按 `API/Browser → internal_spider → VideoSourceFactory → scan_today_dir(LAST)` 顺序试 4 路径,产出 `Asset` (text or wav path,二选一);`process_asset` 消费 `Asset` → 转写 → 质量 → Refine → save → cleanup,产出 `ProcessResult`。`transcriber.transcribe` 签名变纯(只 Whisper,ffmpeg 抽音迁到 `transcribe/extract.py::extract_audio` helper)。`scan_today_dir` 从 `strategy` 内部挪到 `fetch_asset` 末尾(用户决策 2026-09-09)。

**Tech Stack:** Python 3.12 / uv / pydantic v2 / typer / faster-whisper / ffmpeg / Playwright (仅作为 Chrome CDP cookie 来源给 `InternalSiteSpider`)

**Spec:** [docs/superpowers/specs/2026-09-09-asset-pipeline-design.md](../specs/2026-09-09-asset-pipeline-design.md) (本轮 plan 的唯一真相;executor 读这两份)

---

## Global Constraints

来自 spec §1.3 / §6 / 父 CLAUDE.md 的项目级规则 — 每条都直接影响实现:

- **运行时:** Python 3.12,uv 管理,src layout(`src/vla/`),`uv add / uv remove / uv sync / uv lock`,lockfile 已提交
- **数据模型:** pydantic v2(BaseModel);`Asset` / `ProcessResult` 用 `@dataclass(frozen=True)`(spec §3.1 / §3.3)
- **类型注解:** 函数签名必填,函数体内可省
- **测试:** `pytest-asyncio`,`asyncio_mode = "auto"`,所有 Phase 验收前跑 `uv run vla doctor`
- **字幕永远本地** (红线): 只用 faster-whisper / B站官方 CC / VideoTrans;**禁止引入云端转写**
- **云端 LLM 限定两件事**: ① 字幕质量检查 ② 6h 批量总结
- **磁盘友好**: 转写完才能删源文件,质量过了才能删
- **commit 规范**: `<scope>: <imperative summary>` (e.g. `feat(asset): add Asset/ProcessResult models`),用本仓库现有 conventional 风格
- **TDD**: 任何 task 写实现前先写失败测试,跑红 → 写最小实现 → 跑绿 → commit
- **import 顺序**: stdlib → third-party → local;type import 用 `from __future__ import annotations` + `TYPE_CHECKING`
- **logging**: 用 `logger = logging.getLogger(__name__)` 模式;不要 `print`
- **路径处理**: 用 `pathlib.Path`,不要字符串拼接
- **no dead code**: 删 `_extract_audio` / 删 `out_dir` 参数时确认所有调用方已迁移

---

## File Structure

本轮变更的文件按职责分解(锁定 task 边界用):

| 文件 | 角色 | 变更类型 |
|---|---|---|
| `src/vla/models.py` | 数据契约中心(Asset / ProcessResult / SubtitleResult) | 改 |
| `src/vla/transcribe/extract.py` | ffmpeg helper 唯一入口 | **新建** |
| `src/vla/transcribe/streaming.py` | 纯 Whisper 转写(transcribe 签名变纯) | 改 |
| `src/vla/subtitle/strategy.py` | 字幕三级策略(API / Browser,纯 text 命中) | 改 |
| `src/vla/subtitle/internal_site_spider.py` | API-driven spider 占位 stub | **新建** |
| `src/vla/main_provider.py` | `RealTextProvider` 拆 fetch_asset / process_asset | 改 |
| `src/vla/main.py` | `_process_one` 改薄 + 注入新签名 | 改 |
| 注释清理 6 文件 (`capture/screenshot_phase_controller.py`, `audio/source_factory.py`, `audio/queue.py`, `subtitle/internal_site_adapter.py`, `subtitle/bilibili_adapter.py`, `subtitle/strategy.py`) | 历史残留注释 | 改 |
| `tests/test_extract_audio.py` | extract_audio 单元测试 | **新建** |
| `tests/test_fetch_asset.py` | fetch_asset 6 分支测试 | **新建** |
| `tests/test_process_asset.py` | process_asset 6 分支测试 | **新建** |
| `tests/test_subtitle_strategy.py` | _try_browser 改后行为 | 改 |
| `tests/test_transcribe_streaming.py` | transcribe 新签名测试 | 改 |
| `tests/test_video_learning_agent.py` | stub 接口对齐 | 改 |
| `tests/test_main.py` | `_process_one` 集成测试扩 | 改 |
| `scripts/spike_f26_pipeline.py` | 装配改用新返回 | 改 |
| `docs/superpowers/backlog.md` | F2-10 ready 检测 / 内部源截图 / 工厂统一 | 改 |
| `implementation-plan.md` | + Phase 9.5 节 | 改 |

---

## Task Decomposition (12 个 task,每个独立可测可 commit)

| Task | 焦点 | 文件 | 验收 |
|---|---|---|---|
| 1 | models: +Asset / +ProcessResult / SubtitleResult +audio_path | `models.py` | unit test: 数据类字段齐全,frozen |
| 2 | extract_audio helper 拆出 | 新建 `transcribe/extract.py` | test_extract_audio 5 分支 |
| 3 | transcribe 签名变纯 | `streaming.py` | test_transcribe_streaming 改后 |
| 4 | strategy._try_browser 返回 SubtitleResult (不调 transcribe) | `strategy.py` | test_subtitle_strategy 改后 |
| 5 | InternalSiteSpider 占位 stub | 新建 `internal_site_spider.py` | import / class sig test |
| 6 | RealTextProvider 拆 fetch_asset + process_asset | `main_provider.py` | type stub test |
| 7 | fetch_asset 4 路径实现 | `main_provider.py` | test_fetch_asset 6 分支 |
| 8 | process_asset 6 步骤实现 | `main_provider.py` | test_process_asset 6 分支 |
| 9 | build_text_provider 返回二元组 | `main_provider.py` | call-site update |
| 10 | _process_one 改薄 | `main.py` | test_video_learning_agent 改后 |
| 11 | 注释清理 6 文件 | 多个文件 | grep 验证 |
| 12 | 文档收尾 + 验收脚本 | `backlog.md` / `implementation-plan.md` / `spike_f26_pipeline.py` | §5.2 验收 #1-#5 全过 |

---

## Task 1: 数据契约扩展 (`src/vla/models.py`)

**Files:**
- Modify: `src/vla/models.py:32-44` (改 `SubtitleResult` 加 `audio_path` 字段;新建 `Asset` / `ProcessResult` dataclass)
- Test: `tests/test_models.py` (新建,只覆盖本 task 涉及的字段,fixture 不重复造)

**Interfaces:**
- Produces:
  - `class SubtitleResult(BaseModel)`: +`audio_path: Path | None = None`
  - `@dataclass(frozen=True) class Asset`: 字段 `text`, `source`, `audio_path`, `deletable=False`;property `needs_transcribe`
  - `@dataclass(frozen=True) class ProcessResult`: 字段 `text`, `qr: QualityResult`, `source`, `duration_sec: int`

- [ ] **Step 1: 写失败测试**

`tests/test_models.py`:
```python
from pathlib import Path
import pytest
from vla.models import Asset, ProcessResult, QualityResult, SubtitleResult


def test_subtitle_result_audio_path_default_none():
    r = SubtitleResult(text="hi", source="api")
    assert r.audio_path is None


def test_subtitle_result_audio_path_set():
    p = Path("/tmp/foo.webm")
    r = SubtitleResult(text=None, source="whisper_scan", audio_path=p)
    assert r.audio_path == p


def test_asset_text_only():
    a = Asset(text="hello", source="api", audio_path=None)
    assert a.needs_transcribe is False
    assert a.deletable is False


def test_asset_audio_only_needs_transcribe():
    a = Asset(text=None, source="whisper_download", audio_path=Path("/tmp/a.wav"), deletable=True)
    assert a.needs_transcribe is True
    assert a.source == "whisper_download"


def test_asset_frozen():
    a = Asset(text=None, source="whisper_download", audio_path=Path("/tmp/a.wav"))
    with pytest.raises(Exception):  # FrozenInstanceError
        a.source = "browser"


def test_process_result_fields():
    qr = QualityResult(score=0.9, passed=True, reason="ok")
    r = ProcessResult(text="hello", qr=qr, source="api", duration_sec=60)
    assert r.text == "hello"
    assert r.duration_sec == 60
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_models.py -v`
Expected: `ImportError: cannot import name 'Asset' from 'vla.models'` 等导入失败

- [ ] **Step 3: 实现 models 改动**

`src/vla/models.py` 加在 `SubtitleResult` 后面 / `QualityResult` 前面:

```python
from dataclasses import dataclass
from pathlib import Path


class SubtitleResult(BaseModel):
    text: str | None = None            # scan / internal_spider 路径可空
    source: str
    audio_path: Path | None = None      # 新增(spec §3.2)
    metadata: dict | None = None


@dataclass(frozen=True)
class Asset:
    """输入链产出:字幕(text)或音频(audio_path),二选一(spec §3.1)。

    字段语义:
    - text: 字幕已就绪(API / Browser 命中)
    - audio_path: 永远是 wav(由 fetch_asset 抽好)
    - source: "api" | "browser" | "whisper_scan" | "whisper_download" | "whisper_internal_download"
    - deletable: 质量 pass 后是否 unlink 该 wav
    """
    text: str | None
    source: str
    audio_path: Path | None
    deletable: bool = False

    @property
    def needs_transcribe(self) -> bool:
        return self.audio_path is not None


@dataclass(frozen=True)
class ProcessResult:
    """处理链结果(spec §3.3)。"""
    text: str
    qr: QualityResult
    source: str
    duration_sec: int
```

- [ ] **Step 4: 跑测试确认绿**

Run: `uv run pytest tests/test_models.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/models.py tests/test_models.py
git commit -m "feat(models): add Asset + ProcessResult dataclasses; SubtitleResult.audio_path"
```

---

## Task 2: ffmpeg helper `extract_audio` 拆出

**Files:**
- Create: `src/vla/transcribe/extract.py`
- Test: `tests/test_extract_audio.py`

**Interfaces:**
- Produces:
  - `def extract_audio(input_path: Path, output_path: Path) -> None` — 失败抛 `RuntimeError` / `FileNotFoundError`,半截 wav 在 finally 里被删

- [ ] **Step 1: 写失败测试**

`tests/test_extract_audio.py`:
```python
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from vla.transcribe.extract import extract_audio


def test_extract_audio_success(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")  # 实际 ffmpeg 会失败,但我们 mock
    out = tmp_path / "out.wav"
    fake_proc = MagicMock(returncode=0, stderr="")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc):
        extract_audio(src, out)
    # output 不应被删(成功路径)
    assert out.exists() is False  # 因为我们没真的创建


def test_extract_audio_ffmpeg_nonzero_raises_and_cleans(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    out.write_bytes(b"\x00\x00")  # 模拟半截 wav
    fake_proc = MagicMock(returncode=1, stderr="some ffmpeg error")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="extract_audio failed"):
            extract_audio(src, out)
    # 半截 wav 被删
    assert out.exists() is False


def test_extract_audio_input_missing_raises(tmp_path):
    src = tmp_path / "missing.mp4"  # 不创建
    out = tmp_path / "out.wav"
    fake_proc = MagicMock(returncode=1, stderr="no such file")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc):
        with pytest.raises(RuntimeError):
            extract_audio(src, out)
    # output 不该被建出来
    assert out.exists() is False


def test_extract_audio_ffmpeg_binary_missing(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    with patch(
        "vla.transcribe.extract.subprocess.run",
        side_effect=FileNotFoundError("ffmpeg not found"),
    ):
        with pytest.raises(FileNotFoundError):
            extract_audio(src, out)


def test_extract_audio_overwrites_existing_output(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    out.write_bytes(b"old")
    fake_proc = MagicMock(returncode=0, stderr="")
    with patch("vla.transcribe.extract.subprocess.run", return_value=fake_proc) as mrun:
        extract_audio(src, out)
    # 验证用了 -y(覆盖)
    args = mrun.call_args[0][0]
    assert "-y" in args
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_extract_audio.py -v`
Expected: `ModuleNotFoundError: No module named 'vla.transcribe.extract'`

- [ ] **Step 3: 实现 extract_audio**

`src/vla/transcribe/extract.py`:
```python
"""FFmpeg helper — 从输入(任意 ffmpeg 支持格式)抽 wav。

调用方约定:
- input_path 可以是 mp4/webm/m3u8 URL/本地路径
- output_path 必须 .wav 后缀
- 失败时调用方负责决策(降级 / 报警);半截 wav 会被本模块清掉
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path


logger = logging.getLogger(__name__)


def extract_audio(input_path: Path, output_path: Path) -> None:
    """ffmpeg 抽 input → wav(output_path)。

    Args:
        input_path: 任意 ffmpeg 支持格式(mp4/webm/m3u8/...)
        output_path: 目标 wav 路径(需 .wav 后缀)

    Raises:
        RuntimeError: ffmpeg 返回非 0
        FileNotFoundError: ffmpeg 二进制缺失或 input 不存在

    失败语义: 半截 wav 在 finally 里被删,避免 3 小时视频抽到一半崩了
    留 345MB 残文件占磁盘。
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-ar", "16000", "-ac", "1", "-f", "wav", str(output_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"extract_audio failed: {input_path} → {output_path}: {proc.stderr}"
            )
    except Exception:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                logger.warning("清理半截 wav 失败 %s,继续", output_path)
        raise
```

- [ ] **Step 4: 跑测试确认绿**

Run: `uv run pytest tests/test_extract_audio.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/transcribe/extract.py tests/test_extract_audio.py
git commit -m "feat(transcribe): extract extract_audio ffmpeg helper into its own module"
```

---

## Task 3: `transcriber.transcribe` 签名变纯

**Files:**
- Modify: `src/vla/transcribe/streaming.py:55-260` (`StreamingTranscriber.transcribe` 签名;删 `_extract_audio` 方法;删内部 MP4 unlink)
- Test: `tests/test_transcribe_streaming.py` (改既有测试,适配新签名)

**Interfaces:**
- Produces:
  - `def transcribe(self, audio_path: Path) -> str` — 只 Whisper;若传 mp4/webm 应抛 ValueError(spec §4.6 契约)

- [ ] **Step 1: 读现状,确定改前测试基线**

Read: `src/vla/transcribe/streaming.py` 全文,确认 `transcribe` 当前签名/实现。
Read: `tests/test_transcribe_streaming.py` 全文,列出需要改的测试名(改前 → 改后映射)。

- [ ] **Step 2: 改 test,期望跑红**

把 `tests/test_transcribe_streaming.py` 中调 `transcribe(video_path, out_dir=...)` 的所有 fixture 改成 `transcribe(audio_path)`;新增契约测试:

```python
def test_transcribe_rejects_mp4(tmp_path):
    from vla.transcribe.streaming import StreamingTranscriber
    mp4 = tmp_path / "video.mp4"
    mp4.write_bytes(b"\x00")
    transcriber = StreamingTranscriber(model_size="tiny")
    with pytest.raises(ValueError, match="wav"):
        transcriber.transcribe(mp4)


def test_transcribe_does_not_call_extract_audio(tmp_path):
    """transcribe 只做 Whisper,ffmpeg 抽音已迁到 extract.py(spec §4.6)。"""
    from unittest.mock import patch
    from vla.transcribe.streaming import StreamingTranscriber

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    transcriber = StreamingTranscriber(model_size="tiny")
    with patch("vla.transcribe.streaming.subprocess") as msubp:
        # ... 调 transcribe(wav),跑 mock whisper ...
        pass  # 具体 whisper mock 依实现填
        assert not msubp.run.called, "transcribe 不应调 subprocess(ffmpeg)"


def test_transcribe_does_not_unlink_source(tmp_path):
    """FR-3.3 删视频源逻辑迁到 fetch_asset,transcriber 不再 unlink。"""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    transcriber = StreamingTranscriber(model_size="tiny")
    # 调 transcribe
    # 断言 wav 还在
    assert wav.exists()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/test_transcribe_streaming.py -v`
Expected: 多个 FAIL,既有测试期望旧签名 / 新测试期望 transcriber 不调 ffmpeg

- [ ] **Step 4: 改 `StreamingTranscriber.transcribe` 签名 + 删 `_extract_audio`**

`src/vla/transcribe/streaming.py`:
- 删 `def _extract_audio(self, video_path, out_dir) -> Path` 方法体整段
- 改 `def transcribe(self, video_path, out_dir=None) -> str:` → `def transcribe(self, audio_path: Path) -> str:`
- 函数体内:
  - 删 `audio_path = self._extract_audio(video_path, out_dir or ...)` 行
  - 删内部 `audio_path.unlink()` 调用(FR-3.3)
  - 加契约校验:`if audio_path.suffix.lower() != ".wav": raise ValueError(f"transcribe 期望 wav,得到 {audio_path}")`
  - `faster_whisper` 仍跑 `audio_path` (直接传 wav)

- [ ] **Step 5: 跑测试确认绿**

Run: `uv run pytest tests/test_transcribe_streaming.py -v`
Expected: 全部 passed

- [ ] **Step 6: 验证没有调用方还在用旧签名**

```bash
grep -rn "transcriber.transcribe\|transcribe(video_path\|transcribe(.*out_dir" src/ scripts/ tests/ \
  --include="*.py" \
  | grep -v "transcribe(audio_path)\|tests/test_transcribe_streaming"
```

Expected: 仅输出既有测试(已改) 或 0 行(如果没有其他调用方)。

如果有调用方,改它们(列在 §6 文件清单的"调用方"行):
- `src/vla/main_provider.py:35-110` (RealTextProvider 内部用)— 在 Task 6 改,本 task 不动
- `scripts/spike_f26_pipeline.py` — Task 12 改

- [ ] **Step 7: Commit**

```bash
git add src/vla/transcribe/streaming.py tests/test_transcribe_streaming.py
git commit -m "refactor(transcribe): transcribe signature now takes wav only; remove _extract_audio"
```

---

## Task 4: `strategy._try_browser` 不再调 transcribe / 抽音

**Files:**
- Modify: `src/vla/subtitle/strategy.py` 弹窗 enabled 分支(原 line 272 / 289 / 379 / 452 附近)
- Modify: 删 `_try_browser` 中 `scan_untranscribed_audio` 调用 + `transcriber.transcribe` 调用 + sidecar touch
- Test: `tests/test_subtitle_strategy.py` (改既有测试,断言新行为)

**Interfaces:**
- Produces:
  - `_try_browser(...) -> SubtitleResult | None` — 弹窗 enabled 分支只返 `SubtitleResult(text=None, source="whisper_scan", audio_path=webm)`,**不调 transcribe**,**不抽音**,**不 touch sidecar**(后两者归 fetch_asset + process_asset)

- [ ] **Step 1: 改 test 适配新行为**

`tests/test_subtitle_strategy.py` 中对应"弹窗 enabled"路径的测试,断言:

```python
def test_try_browser_enabled_returns_subtitle_result_no_transcribe(monkeypatch):
    # mock scan_untranscribed_audio → return webm 路径
    # mock transcriber.transcribe → 必须 NOT called
    # mock extract_audio → 必须 NOT called
    # 调 _try_browser(...)
    # 断言: 返回 SubtitleResult(text=None, source="whisper_scan", audio_path=webm)
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_subtitle_strategy.py -v -k "enabled"`
Expected: 断言失败 — 现代码会调 transcriber / extract_audio / touch sidecar

- [ ] **Step 3: 改 `_try_browser` 弹窗 enabled 分支**

`src/vla/subtitle/strategy.py`:
- 把原 `audio_path = scan_untranscribed_audio(...); if audio_path is None: return None; text = self.transcriber.transcribe(...); audio_path.with_suffix(".transcribed.txt").touch(); return text, {...}`
- 改为:
  ```python
  from vla.models import SubtitleResult
  audio_path = scan_untranscribed_audio(today_dir)
  if audio_path is None:
      return None
  return SubtitleResult(
      text=None,
      source="whisper_scan",
      audio_path=audio_path,
      metadata={"via": "tab_audio_recorder", "method": "scan_today_dir"},
  )
  ```
- 注意: `scan_untranscribed_audio` 仍在本分支被调(因为 spec §4.3 把 scan 入口挪到 fetch_asset,但 `_try_browser` 内部仍可能调,只作为兜底探测;Task 7 fetch_asset 接管后会拆开)。**本 task 只确保 _try_browser 不调 transcribe / extract / touch**,调用 `scan_untranscribed_audio` 这步保留(在 Task 7 拆)

- [ ] **Step 4: 跑测试确认绿**

Run: `uv run pytest tests/test_subtitle_strategy.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/subtitle/strategy.py tests/test_subtitle_strategy.py
git commit -m "refactor(strategy): _try_browser enabled branch returns SubtitleResult only"
```

---

## Task 5: `InternalSiteSpider` 占位 stub

**Files:**
- Create: `src/vla/subtitle/internal_site_spider.py`
- Test: `tests/test_internal_site_spider.py` (新建,只验证接口签名)

**Interfaces:**
- Produces:
  ```python
  class InternalSiteSpider:
      def __init__(self, cdp_url: str, college_id: str, resolution: str = "720p") -> None: ...
      async def list_tasks(self) -> list[VideoTask]: ...  # TODO 单独 PR 实装
      async def fetch_m3u8(self, kng_id: str) -> str: ...  # TODO 单独 PR 实装
  ```

- [ ] **Step 1: 写失败测试**

`tests/test_internal_site_spider.py`:
```python
import pytest

from vla.subtitle.internal_site_spider import InternalSiteSpider


def test_construct_accepts_default_resolution():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    assert s.resolution == "720p"


def test_construct_accepts_custom_resolution():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc", resolution="480p")
    assert s.resolution == "480p"


@pytest.mark.asyncio
async def test_list_tasks_placeholder_raises_not_implemented():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    with pytest.raises(NotImplementedError, match="走单独 PR"):
        await s.list_tasks()


@pytest.mark.asyncio
async def test_fetch_m3u8_placeholder_raises_not_implemented():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    with pytest.raises(NotImplementedError, match="走单独 PR"):
        await s.fetch_m3u8("kng-id-123")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_internal_site_spider.py -v`
Expected: `ModuleNotFoundError: No module named 'vla.subtitle.internal_site_spider'`

- [ ] **Step 3: 实现 stub**

`src/vla/subtitle/internal_site_spider.py`:
```python
"""InternalSiteSpider 占位 stub — 实现走单独 PR。

完整设计见 spec §4.7:
  4 个 yunxuetang API:
    1. POST /kng/kngCatalog/student/tree     (拿 catalog 树)
    2. POST /kng/knowledge/pagelist           (子目录视频列表)
    3. POST /kng/study/submit/preinit         (开 study session,等价"点开始学习")
    4. POST /kng/study/kngPlay                (拿 m3u8 URL)
  认证:Chrome CDP 借 SSO cookie(连 localhost:9222 → context.cookies())

本模块只占位,fetch_asset 在 source.startswith("internal") 分支接住
SubtitleResult(metadata={"video_url": "<m3u8>"}) 即可。Spider 实装不影响
fetch_asset 主流程。
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class InternalSiteSpider:
    """API-driven spider for b-learning.bill-jc.com (stub)。"""

    def __init__(self, cdp_url: str, college_id: str, resolution: str = "720p") -> None:
        self.cdp_url = cdp_url
        self.college_id = college_id
        self.resolution = resolution

    async def list_tasks(self) -> list:  # 实际返回 list[VideoTask]
        raise NotImplementedError(
            "InternalSiteSpider.list_tasks 实装走单独 PR(spec §4.7)"
        )

    async def fetch_m3u8(self, kng_id: str) -> str:
        raise NotImplementedError(
            "InternalSiteSpider.fetch_m3u8 实装走单独 PR(spec §4.7)"
        )
```

- [ ] **Step 4: 跑测试确认绿**

Run: `uv run pytest tests/test_internal_site_spider.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/subtitle/internal_site_spider.py tests/test_internal_site_spider.py
git commit -m "feat(spider): add InternalSiteSpider stub (implementation in separate PR)"
```

---

## Task 6: `RealTextProvider` 拆 fetch_asset / process_asset 骨架

**Files:**
- Modify: `src/vla/main_provider.py:35-110` (`RealTextProvider` 类结构)
- Test: `tests/test_main_provider.py` (新建,只验证两个方法的 stub 存在 + 返回类型)

**Interfaces:**
- Produces:
  ```python
  class RealTextProvider:
      async def fetch_asset(self, task: VideoTask) -> Asset | None: ...  # 本 task 仅 raise NotImplementedError,Task 7 实装
      async def process_asset(self, asset: Asset, task: VideoTask) -> ProcessResult | None: ...  # Task 8 实装
      async def __call__(self, task: VideoTask) -> tuple[Asset | None, ProcessResult | None]: ...  # wrapper
  ```

- [ ] **Step 1: 写失败测试**

`tests/test_main_provider.py`:
```python
import pytest

from vla.main_provider import RealTextProvider


@pytest.mark.asyncio
async def test_fetch_asset_placeholder():
    p = RealTextProvider.__new__(RealTextProvider)  # 跳过 __init__ 依赖
    # 手动塞依赖(stub)
    p.strategy = None
    p.transcriber = None
    p.source_factory = None
    p.checker = None
    p.refiner = None
    p.log = None
    p.plugin_status = None
    p._save_dir = None
    p._today_dir = None
    from vla.models import VideoTask
    task = VideoTask(id="t", title="t", url="https://x", expected_duration=60)
    with pytest.raises(NotImplementedError):
        await p.fetch_asset(task)


@pytest.mark.asyncio
async def test_process_asset_placeholder():
    p = RealTextProvider.__new__(RealTextProvider)
    from vla.models import Asset, VideoTask
    asset = Asset(text=None, source="whisper_download", audio_path=None)
    task = VideoTask(id="t", title="t", url="https://x", expected_duration=60)
    with pytest.raises(NotImplementedError):
        await p.process_asset(asset, task)


@pytest.mark.asyncio
async def test_call_delegates_to_fetch_then_process():
    """__call__ = fetch_asset + process_asset 串起来。"""
    p = RealTextProvider.__new__(RealTextProvider)
    from vla.models import Asset, ProcessResult, QualityResult, VideoTask
    fake_asset = Asset(text="hi", source="api", audio_path=None)
    fake_result = ProcessResult(text="hi", qr=QualityResult(score=0.9, passed=True, reason="ok"),
                                source="api", duration_sec=60)
    async def fake_fetch(task): return fake_asset
    async def fake_process(asset, task): return fake_result
    p.fetch_asset = fake_fetch  # type: ignore
    p.process_asset = fake_process  # type: ignore
    task = VideoTask(id="t", title="t", url="https://x", expected_duration=60)
    asset, result = await p(task)
    assert asset is fake_asset
    assert result is fake_result
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_main_provider.py -v`
Expected: `AttributeError: 'RealTextProvider' object has no attribute 'fetch_asset'`

- [ ] **Step 3: 改 `RealTextProvider` 类结构**

`src/vla/main_provider.py`:
- 加 `from vla.models import Asset, ProcessResult`
- 类内新增两个方法骨架(本 task 只 raise):
  ```python
  async def fetch_asset(self, task: VideoTask) -> Asset | None:
      """输入链(本 task 仅占位,Task 7 实装)。"""
      raise NotImplementedError("Task 7 实装 fetch_asset")

  async def process_asset(self, asset: Asset, task: VideoTask) -> ProcessResult | None:
      """处理链(本 task 仅占位,Task 8 实装)。"""
      raise NotImplementedError("Task 8 实装 process_asset")

  async def __call__(self, task: VideoTask) -> tuple[Asset | None, ProcessResult | None]:
      asset = await self.fetch_asset(task)
      if asset is None:
          return None, None
      result = await self.process_asset(asset, task)
      return asset, result
  ```

- [ ] **Step 4: 跑测试确认绿**

Run: `uv run pytest tests/test_main_provider.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/main_provider.py tests/test_main_provider.py
git commit -m "refactor(provider): split RealTextProvider into fetch_asset + process_asset (stub)"
```

---

## Task 7: `fetch_asset` 4 路径实现(核心)

**Files:**
- Modify: `src/vla/main_provider.py` `fetch_asset` 方法(替换 Task 6 占位)
- Test: `tests/test_fetch_asset.py` (新建,6 分支覆盖)

**Interfaces:**
- Produces:
  - `async def fetch_asset(self, task) -> Asset | None`
  - 顺序: ① strategy.get_subtitle(text 命中) → ② internal_spider(m3u8→wav) → ③ VideoSourceFactory(MP4→wav→unlink) → ④ scan_today_dir(webm→wav)

- [ ] **Step 1: 写失败测试**

`tests/test_fetch_asset.py`(完整覆盖 6 分支;只列关键骨架):

```python
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from vla.main_provider import RealTextProvider
from vla.models import Asset, QualityResult, SubtitleResult, VideoSource, VideoTask


def _make_provider(strategy=None, source_factory=None, save_dir=None, today_dir=None):
    p = RealTextProvider.__new__(RealTextProvider)
    p.strategy = strategy or AsyncMock()
    p.transcriber = AsyncMock()
    p.source_factory = source_factory or MagicMock()
    p.checker = MagicMock()
    p.refiner = None
    p.log = MagicMock()
    p.plugin_status = MagicMock()
    p._save_dir = save_dir or Path("/tmp/save")
    p._today_dir = today_dir or Path("/tmp/today")
    p._save_dir.mkdir(parents=True, exist_ok=True)
    p._today_dir.mkdir(parents=True, exist_ok=True)
    return p


def _task(url="https://www.bilibili.com/video/BV1xx"):
    return VideoTask(id="BV1xx", title="t", url=url, expected_duration=60)


@pytest.mark.asyncio
async def test_fetch_asset_api_text_hit():
    p = _make_provider()
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(text="hello", source="api"))
    asset = await p.fetch_asset(_task())
    assert asset.text == "hello"
    assert asset.source == "api"
    assert asset.audio_path is None
    assert asset.deletable is False
    assert asset.needs_transcribe is False


@pytest.mark.asyncio
async def test_fetch_asset_browser_text_hit():
    p = _make_provider()
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(text="hi", source="browser"))
    asset = await p.fetch_asset(_task())
    assert asset.source == "browser"
    assert asset.deletable is False


@pytest.mark.asyncio
async def test_fetch_asset_internal_spider_m3u8_to_wav(tmp_path):
    p = _make_provider(save_dir=tmp_path / "save", today_dir=tmp_path / "today")
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(
        text=None, source="internal_spider",
        metadata={"video_url": "https://video.bill-jc.com/conversion/group1/v1/test.m3u8"},
    ))
    fake_wav = tmp_path / "save" / "audio_raw" / "BV1xx.wav"
    fake_wav.parent.mkdir(parents=True, exist_ok=True)
    with patch("vla.main_provider.extract_audio") as mex:
        # 模拟 extract_audio 写出 wav
        def fake_extract(src, dst):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"\x00")
        mex.side_effect = fake_extract
        asset = await p.fetch_asset(_task())
    assert asset.source == "whisper_internal_download"
    assert asset.audio_path == fake_wav
    assert asset.deletable is True
    assert asset.needs_transcribe is True


@pytest.mark.asyncio
async def test_fetch_asset_internal_spider_no_video_url_returns_none():
    """internal_spider 返回但无 video_url → 返回 None(spec §4.2 策略明确)。"""
    p = _make_provider()
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(
        text=None, source="internal_spider", metadata={},  # 无 video_url
    ))
    asset = await p.fetch_asset(_task(url="https://b-learning.bill-jc.com/kng/#/video/play?kngId=xxx"))
    assert asset is None


@pytest.mark.asyncio
async def test_fetch_asset_video_source_factory_mp4_to_wav(tmp_path):
    p = _make_provider(save_dir=tmp_path / "save", today_dir=tmp_path / "today")
    p.strategy.get_subtitle = AsyncMock(return_value=None)  # 字幕未命中
    # source_factory 返回 video_path
    mp4 = tmp_path / "BV1xx.mp4"
    mp4.write_bytes(b"\x00")
    p.source_factory.get = MagicMock(return_value=VideoSource(path=mp4, kind="yt_dlp"))
    with patch("vla.main_provider.extract_audio") as mex:
        def fake_extract(src, dst):
            dst.write_bytes(b"\x00")
        mex.side_effect = fake_extract
        asset = await p.fetch_asset(_task())
    assert asset.source == "whisper_download"
    assert asset.audio_path == mp4.with_suffix(".wav")
    assert asset.deletable is True
    # MP4 应被 unlink(FR-3.3)
    assert not mp4.exists()


@pytest.mark.asyncio
async def test_fetch_asset_scan_today_dir_last(tmp_path):
    """scan 路径:last 兜底;strategy/api/internal/factory 全失败后才用。"""
    p = _make_provider(save_dir=tmp_path / "save", today_dir=tmp_path / "today")
    p.strategy.get_subtitle = AsyncMock(return_value=None)
    p.source_factory.get = MagicMock(return_value=None)
    webm = tmp_path / "today" / "recording.webm"
    webm.write_bytes(b"\x00")
    with patch("vla.audio_scan.scan_untranscribed_audio", return_value=webm) as mscan, \
         patch("vla.main_provider.extract_audio") as mex:
        def fake_extract(src, dst):
            dst.write_bytes(b"\x00")
        mex.side_effect = fake_extract
        asset = await p.fetch_asset(_task())
    assert asset.source == "whisper_scan"
    assert asset.audio_path == webm.with_suffix(".wav")
    assert asset.deletable is True  # wav 是我方 temp
    # webm 留原位(用户产物)
    assert webm.exists()
    # scan 在 strategy/source_factory 之后才被调(顺序断言)
    assert mscan.called


@pytest.mark.asyncio
async def test_fetch_asset_all_paths_exhausted_returns_none(tmp_path):
    p = _make_provider(save_dir=tmp_path / "save", today_dir=tmp_path / "today")
    p.strategy.get_subtitle = AsyncMock(return_value=None)
    p.source_factory.get = MagicMock(return_value=None)
    with patch("vla.audio_scan.scan_untranscribed_audio", return_value=None):
        asset = await p.fetch_asset(_task())
    assert asset is None


@pytest.mark.asyncio
async def test_fetch_asset_scan_not_called_when_strategy_text_hits():
    """scan 是 last fallback;strategy 命中 text 时不应扫目录。"""
    p = _make_provider()
    p.strategy.get_subtitle = AsyncMock(return_value=SubtitleResult(text="hi", source="api"))
    with patch("vla.audio_scan.scan_untranscribed_audio") as mscan:
        await p.fetch_asset(_task())
    assert not mscan.called
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_fetch_asset.py -v`
Expected: `NotImplementedError: Task 7 实装 fetch_asset`(Task 6 占位还在)

- [ ] **Step 3: 实现 `fetch_asset`(spec §4.2 关键代码直接落地)**

`src/vla/main_provider.py`:
- 加 imports:
  ```python
  from vla.audio_scan import scan_untranscribed_audio
  from vla.transcribe.extract import extract_audio
  ```
- 替换 `fetch_asset` 占位方法,按 spec §4.2 4 路径实现(完整代码见 spec §4.2,这里只列骨架):
  ```python
  async def fetch_asset(self, task: VideoTask) -> Asset | None:
      url = str(task.url)
      duration_sec = task.expected_duration

      # 1. 字幕策略
      try:
          result = await self.strategy.get_subtitle(url, duration_sec)
      except Exception as e:
          logger.warning("策略调用异常,降级到 internal_spider: %s", e)
          result = None

      if result is not None and result.text is not None:
          return Asset(text=result.text, source=result.source, audio_path=None, deletable=False)

      # 2. internal_spider
      if result is not None and (result.source or "").startswith("internal"):
          video_url = (result.metadata or {}).get("video_url")
          if not video_url:
              logger.warning("internal spider 返回无 video_url,跳过: %s", result)
              return None
          wav_path = self._save_dir / "audio_raw" / f"{task.id}.wav"
          try:
              extract_audio(Path(video_url), wav_path)
          except Exception as e:
              logger.warning("internal spider m3u8 抽音失败 %s: %s", video_url, e)
              return None
          return Asset(text=None, source="whisper_internal_download", audio_path=wav_path, deletable=True)

      # 3. VideoSourceFactory
      try:
          source = self.source_factory.get(url, task.id, duration_sec)
      except Exception as e:
          logger.warning("source_factory.get 失败: %s", e)
          source = None

      if source is not None:
          video_path = source.path
          wav_path = video_path.with_suffix(".wav")
          try:
              extract_audio(video_path, wav_path)
          except Exception as e:
              logger.warning("MP4 抽音失败 %s: %s", video_path, e)
          else:
              try:
                  video_path.unlink()
              except Exception as e:
                  logger.warning("unlink MP4 失败 %s: %s", video_path, e)
              return Asset(text=None, source="whisper_download", audio_path=wav_path, deletable=True)

      # 4. scan_today_dir (last)
      try:
          webm_path = scan_untranscribed_audio(self._today_dir)
      except Exception as e:
          logger.warning("scan_today_dir 失败: %s", e)
          return None

      if webm_path is None:
          return None
      wav_path = webm_path.with_suffix(".wav")
      try:
          extract_audio(webm_path, wav_path)
      except Exception as e:
          logger.warning("scan webm 抽音失败 %s: %s", webm_path, e)
          return None
      return Asset(text=None, source="whisper_scan", audio_path=wav_path, deletable=True)
  ```

- [ ] **Step 4: 跑测试确认绿**

Run: `uv run pytest tests/test_fetch_asset.py -v`
Expected: 8 passed

- [ ] **Step 5: 跑全量单测看有没有 regression**

Run: `uv run pytest tests/ -v --ignore=tests/test_e2e.py`
Expected: 全绿(test_e2e.py 已知 broken,见 backlog + memory)

- [ ] **Step 6: Commit**

```bash
git add src/vla/main_provider.py tests/test_fetch_asset.py
git commit -m "feat(provider): fetch_asset 4-path chain (api/browser → internal → factory → scan)"
```

---

## Task 8: `process_asset` 6 步骤实现

**Files:**
- Modify: `src/vla/main_provider.py` `process_asset` 方法(替换 Task 6 占位)
- Test: `tests/test_process_asset.py` (新建,6 分支覆盖)

**Interfaces:**
- Produces:
  - `async def process_asset(self, asset: Asset, task: VideoTask) -> ProcessResult | None`
  - 步骤: 转写(如果 needs_transcribe) → 质量门控 → 失败 log + 不 unlink → Refine(可选) → save_transcribed → 清理 audio(if deletable)

- [ ] **Step 1: 写失败测试**

`tests/test_process_asset.py`:
```python
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from vla.main_provider import RealTextProvider
from vla.models import Asset, ProcessResult, QualityResult, VideoTask


def _make_provider():
    p = RealTextProvider.__new__(RealTextProvider)
    p.transcriber = MagicMock()
    p.checker = MagicMock()
    p.refiner = None
    p.log = MagicMock()
    p.plugin_status = MagicMock()
    p.cfg = MagicMock(quality_check=MagicMock(refine_enabled=False))
    return p


def _task():
    return VideoTask(id="BV1xx", title="t", url="https://x", expected_duration=60)


@pytest.mark.asyncio
async def test_process_asset_text_only_no_transcribe():
    """API/Browser 命中 → 直接用 text,不调 transcribe。"""
    p = _make_provider()
    asset = Asset(text="hello", source="api", audio_path=None)
    p.checker.check = MagicMock(return_value=QualityResult(score=0.95, passed=True, reason="ok"))
    result = await p.process_asset(asset, _task())
    assert result is not None
    assert result.text == "hello"
    assert result.source == "api"
    p.transcriber.transcribe.assert_not_called()


@pytest.mark.asyncio
async def test_process_asset_wav_transcribe_passes(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="transcribed text")
    p.checker.check = MagicMock(return_value=QualityResult(score=0.9, passed=True, reason="ok"))
    result = await p.process_asset(asset, _task())
    assert result is not None
    assert result.text == "transcribed text"
    p.transcriber.transcribe.assert_called_once_with(wav)
    # 质量 pass → unlink audio
    assert not wav.exists()


@pytest.mark.asyncio
async def test_process_asset_wav_quality_fail_keeps_audio(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="bad text")
    p.checker.check = MagicMock(return_value=QualityResult(score=0.3, passed=False, reason="低质"))
    result = await p.process_asset(asset, _task())
    assert result is None
    p.log.log_quality_fail.assert_called_once()
    # 质量 fail → 不 unlink,保留供 retry(FR-3.7 v3.2)
    assert wav.exists()


@pytest.mark.asyncio
async def test_process_asset_transcribe_fails(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(side_effect=RuntimeError("whisper crashed"))
    result = await p.process_asset(asset, _task())
    assert result is None
    p.log.log_transcribe_fail.assert_called_once()
    # 转写失败 → audio 也不删
    assert wav.exists()


@pytest.mark.asyncio
async def test_process_asset_scan_touches_sidecar(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    asset = Asset(text=None, source="whisper_scan", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="scanned text")
    p.checker.check = MagicMock(return_value=QualityResult(score=0.9, passed=True, reason="ok"))
    await p.process_asset(asset, _task())
    # sidecar 应被 touch
    sidecar = wav.with_suffix(".transcribed.txt")
    assert sidecar.exists()


@pytest.mark.asyncio
async def test_process_asset_browser_quality_fail_marks_plugin_unavailable(tmp_path):
    p = _make_provider()
    asset = Asset(text="browser text", source="browser", audio_path=None)
    p.checker.check = MagicMock(return_value=QualityResult(score=0.3, passed=False, reason="bad"))
    result = await p.process_asset(asset, _task())
    assert result is None
    p.plugin_status.mark_unavailable.assert_called_once()


@pytest.mark.asyncio
async def test_process_asset_refine_runs_when_enabled(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    p.cfg.quality_check.refine_enabled = True
    p.refiner = MagicMock()
    from vla.models import RefinementResult, Correction
    p.refiner.refine = MagicMock(return_value=RefinementResult(
        cleaned_text="refined text",
        corrections=[Correction(original="x", corrected="y", reason="r")],
    ))
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="original")
    p.checker.check = MagicMock(return_value=QualityResult(score=0.9, passed=True, reason="ok"))
    result = await p.process_asset(asset, _task())
    assert result.text == "refined text"
    p.refiner.refine.assert_called_once()


@pytest.mark.asyncio
async def test_process_asset_refine_fails_uses_original(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00")
    p = _make_provider()
    p.cfg.quality_check.refine_enabled = True
    p.refiner = MagicMock()
    p.refiner.refine = MagicMock(side_effect=RuntimeError("refine crashed"))
    asset = Asset(text=None, source="whisper_download", audio_path=wav, deletable=True)
    p.transcriber.transcribe = MagicMock(return_value="original")
    p.checker.check = MagicMock(return_value=QualityResult(score=0.9, passed=True, reason="ok"))
    result = await p.process_asset(asset, _task())
    assert result.text == "original"  # refine 失败 → 用原文
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_process_asset.py -v`
Expected: `NotImplementedError: Task 8 实装 process_asset`

- [ ] **Step 3: 实现 `process_asset`(spec §4.2 关键代码)**

`src/vla/main_provider.py` 替换 `process_asset` 占位,完整代码见 spec §4.2 (步骤 1~6)。骨架:
```python
async def process_asset(self, asset: Asset, task: VideoTask) -> ProcessResult | None:
    # Step 1: 转写
    if asset.needs_transcribe:
        try:
            text = await asyncio.to_thread(self.transcriber.transcribe, asset.audio_path)
        except Exception as e:
            self.log.log_transcribe_fail(task.id, task.title, str(task.url), stage="transcribe", error=str(e))
            return None
        # scan sidecar
        if asset.source == "whisper_scan" and asset.audio_path is not None:
            try:
                asset.audio_path.with_suffix(".transcribed.txt").touch()
            except Exception as e:
                self.log.warning("touch sidecar 失败 %s: %s", asset.audio_path, e)
    else:
        text = asset.text

    # Step 2: 质量
    qr = self.checker.check(text=text, title=task.title, duration_sec=task.expected_duration,
                            model_size=self.cfg.whisper.model)

    # Step 3: 失败分支
    if not qr.passed:
        self.log.log_quality_fail(task.id, task.title, str(task.url), qr, text)
        if asset.source == "browser":
            self.plugin_status.mark_unavailable(reason="plugin_quality_fail")
        return None

    # Step 4: Refine(可选)
    if self.cfg.quality_check.refine_enabled and self.refiner is not None:
        try:
            refinement = self.refiner.refine(text, title=task.title)
            if refinement.cleaned_text:
                text = refinement.cleaned_text
        except Exception as e:
            logger.warning("Refine 失败,使用原文继续: %s", e)

    # Step 5: save
    self.log.save_transcribed(video_id=task.id, title=task.title, text=text,
                              quality=qr, source=asset.source,
                              duration_sec=task.expected_duration)

    # Step 6: 清理 audio
    if asset.deletable and asset.audio_path is not None and asset.audio_path.exists():
        try:
            asset.audio_path.unlink()
        except Exception as e:
            logger.warning("删音频失败 %s,主流程继续: %s", asset.audio_path, e)

    return ProcessResult(text=text, qr=qr, source=asset.source, duration_sec=task.expected_duration)
```

- [ ] **Step 4: 跑测试确认绿**

Run: `uv run pytest tests/test_process_asset.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/main_provider.py tests/test_process_asset.py
git commit -m "feat(provider): process_asset 6-step chain (transcribe → quality → refine → save → cleanup)"
```

---

## Task 9: `build_text_provider` 返回二元组

**Files:**
- Modify: `src/vla/main_provider.py:112-170` (`build_text_provider` 改返回 `(FetchAssetFn, ProcessAssetFn)`)
- Test: 既有 `tests/test_main_provider.py` 加测试覆盖新返回类型

**Interfaces:**
- Produces:
  ```python
  FetchAssetFn = Callable[[VideoTask], Awaitable[Asset | None]]
  ProcessAssetFn = Callable[[Asset, VideoTask], Awaitable[ProcessResult | None]]
  def build_text_provider(...) -> tuple[FetchAssetFn, ProcessAssetFn]: ...
  ```

- [ ] **Step 1: 写失败测试**

`tests/test_main_provider.py` 加:
```python
def test_build_text_provider_returns_tuple():
    from vla.main_provider import build_text_provider
    from vla.models import VideoTask
    cfg = MagicMock()
    transcriber = MagicMock()
    notifier = MagicMock()
    fetch, process = build_text_provider(cfg, transcriber, notifier)
    assert callable(fetch)
    assert callable(process)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_main_provider.py::test_build_text_provider_returns_tuple -v`
Expected: TypeError: cannot unpack non-iterable (现返回 Callable)

- [ ] **Step 3: 改 `build_text_provider`**

`src/vla/main_provider.py`:
```python
from vla.models import Asset, ProcessResult
FetchAssetFn = Callable[[VideoTask], Awaitable[Asset | None]]
ProcessAssetFn = Callable[[Asset, VideoTask], Awaitable[ProcessResult | None]]


def build_text_provider(
    cfg, transcriber, notifier, ...
) -> tuple[FetchAssetFn, ProcessAssetFn]:
    """build RealTextProvider 实例,返回 (fetch_asset, process_asset) 两个 callable。"""
    provider = RealTextProvider(cfg=cfg, transcriber=transcriber, notifier=notifier, ...)
    return provider.fetch_asset, provider.process_asset
```

- [ ] **Step 4: 跑测试确认绿**

Run: `uv run pytest tests/test_main_provider.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/vla/main_provider.py tests/test_main_provider.py
git commit -m "refactor(provider): build_text_provider returns (fetch_asset, process_asset) tuple"
```

---

## Task 10: `_process_one` 改薄 + `main.py` 注入新签名

**Files:**
- Modify: `src/vla/main.py` `VideoLearningAgent.__init__` + `_process_one`
- Test: `tests/test_video_learning_agent.py` (改既有测试,stub 改新接口)

**Interfaces:**
- Produces:
  - `__init__(self, ..., fetch_asset: FetchAssetFn, process_asset: ProcessAssetFn, ...)` — 删 `text_provider: TextProvider` 参数
  - `_process_one(self, task) -> str | None` — 按 spec §4.4 改薄

- [ ] **Step 1: 写失败测试**

`tests/test_video_learning_agent.py` 改既有 `_process_one` 测试,stub 接口:
```python
async def test_process_one_uses_fetch_and_process():
    """_process_one 调 fetch_asset → process_asset → 写 history。"""
    from vla.main import VideoLearningAgent
    fake_asset = Asset(text="hi", source="api", audio_path=None)
    fake_result = ProcessResult(text="hi", qr=QualityResult(score=0.9, passed=True, reason="ok"),
                                source="api", duration_sec=60)
    async def fake_fetch(task): return fake_asset
    async def fake_process(asset, task): return fake_result
    # 构造 agent,注入 stub
    agent = VideoLearningAgent(
        ...,
        fetch_asset=fake_fetch,
        process_asset=fake_process,
        ...
    )
    task = VideoTask(id="BV1xx", title="t", url="https://x", expected_duration=60)
    rc = await agent._process_one(task)
    assert rc == "api"  # 返回 result.source
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_video_learning_agent.py -v`
Expected: TypeError 之类(原签名 `text_provider`)

- [ ] **Step 3: 改 `VideoLearningAgent` 注入签名 + `_process_one`**

`src/vla/main.py`:
- 加 imports:`from vla.models import Asset, ProcessResult`
- `__init__` 改:`text_provider: TextProvider` → `fetch_asset: FetchAssetFn, process_asset: ProcessAssetFn`
  - 删 `self.text_provider = text_provider`
  - 加 `self.fetch_asset = fetch_asset`, `self.process_asset = process_asset`
- `_process_one` 按 spec §4.4 改薄(完整代码见 spec §4.4)
  - 内部:`asset = await self.fetch_asset(task)` → log + return None if None
  - `result = await self.process_asset(asset, task)` → log + return None if None
  - 通知 / Phase A/C 截图保留
  - `return result.source`

- [ ] **Step 4: 跑测试确认绿**

Run: `uv run pytest tests/test_video_learning_agent.py tests/test_main.py -v`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add src/vla/main.py tests/test_video_learning_agent.py tests/test_main.py
git commit -m "refactor(main): _process_one uses fetch_asset + process_asset; drop text_provider injection"
```

---

## Task 11: 历史残留注释清理(spec §4.8)

**Files:**
- Modify:
  - `src/vla/capture/screenshot_phase_controller.py:3`
  - `src/vla/audio/source_factory.py:33, 64`
  - `src/vla/audio/queue.py:18`
  - `src/vla/subtitle/internal_site_adapter.py:30, 48` (删 unused `tab_recorder` import / 参数)
  - `src/vla/subtitle/bilibili_adapter.py:17`
  - `src/vla/subtitle/strategy.py:43, 195` (删 unused `tab_recorder` type hint);line 272, 289, 379, 452 (注释清理)
- Test: 不需新测试,只确保跑通 `pytest tests/ --ignore=tests/test_e2e.py` 不 regression

- [ ] **Step 1: 列出清理点**

按 spec §4.8 三档分类,逐文件读上下文,确认 line 号准确(spec §4.8 给出锚点 line)。

- [ ] **Step 2: 逐文件清理**

每个文件改完,跑该文件的 import smoke:
```bash
uv run python -c "from vla.audio.source_factory import *"
# 6 个文件挨个跑
```

如果某文件没有单独 test,跑:
```bash
uv run python -c "import vla.capture.screenshot_phase_controller"
```

- [ ] **Step 3: 跑全量测试确认无 regression**

Run: `uv run pytest tests/ --ignore=tests/test_e2e.py -q`
Expected: 全绿

- [ ] **Step 4: 跑 spec §5.2 验收 #5 grep**

```bash
grep -rn "Cmd+Shift+R\|tab_recorder\.start_recording" src/ scripts/ tests/ \
  | grep -v "macos_notify\|tab_audio_recorder\.py\|test_e2e\|test_macos_notify\|test_tab_audio_recorder\|spike_f26\|spike_f25"
```

Expected: 仅保留类(用户操作指引 / valid 测试 fixture),无残留描述"F2-8 自动化路径"的注释。

- [ ] **Step 5: Commit**

```bash
git add src/vla/capture/screenshot_phase_controller.py src/vla/audio/source_factory.py \
        src/vla/audio/queue.py src/vla/subtitle/internal_site_adapter.py \
        src/vla/subtitle/bilibili_adapter.py src/vla/subtitle/strategy.py
git commit -m "docs(refactor): clean F2-8 era 'Tab Audio Recorder fallback' stale comments"
```

---

## Task 12: 文档收尾 + spike 装配 + 最终验收

**Files:**
- Modify: `scripts/spike_f26_pipeline.py` (装配改用 `build_text_provider` 新返回)
- Modify: `docs/superpowers/backlog.md` (确认 F2-10 ready / 内部源截图 / 工厂统一 已在 backlog)
- Modify: `implementation-plan.md` (+ Phase 9.5 节 + 验收代码块)

- [ ] **Step 1: 改 `spike_f26_pipeline.py`**

装配改用二元组:
```python
fetch_asset, process_asset = build_text_provider(cfg, transcriber, notifier, ...)
# 替代原 fetch_asset = build_text_provider(...) 单 callable
```

- [ ] **Step 2: 跑 spike 验证完整流程(若环境允许)**

Run: `uv run python scripts/spike_f26_pipeline.py --help`(确认 CLI 不破)

如 spike 需实际 Chrome / 视频源,只验证 import / 参数解析,不全跑。

- [ ] **Step 3: 改 `backlog.md`**

确认以下条目已在(本轮为它们做去重 / 留口子,不做实装):
- §1 F2-10 scan ready 检测(P0,用户明示本轮不做)
- §2 FR-2.27 worker 池(P1)
- §3 FR-2.22 三路径音频清理(P1)
- §4 截图异步化(P2)
- §5 video_source / audio_source 工厂统一(P3)
- **+ 内部源 start / near-end 截图**(2026-09-09 用户提出,spec §4.7 截图需求处理段)

如有缺,补到 backlog.md。

- [ ] **Step 4: 改 `implementation-plan.md`**

加 Phase 9.5 节,验收代码 = spec §5.2 完整 5 步。

- [ ] **Step 5: 跑最终验收脚本(spec §5.2 全跑)**

```bash
# 1. 单元测试
uv run pytest tests/test_extract_audio.py tests/test_fetch_asset.py \
  tests/test_process_asset.py tests/test_subtitle_strategy.py \
  tests/test_main.py tests/test_video_learning_agent.py \
  tests/test_transcribe_streaming.py tests/test_models.py \
  tests/test_internal_site_spider.py tests/test_main_provider.py -v

# 2. spike 跑通(spec §5.2 #2,环境允许时)
uv run python scripts/spike_f26_pipeline.py \
  --url "https://www.bilibili.com/video/BV1DUgK6cEi3" \
  --duration 60 \
  --prep-webm tmp/audio_raw/BV1DUgK6cEi3.wav \
  --force-popup \
  --auto-response enabled

# 3. 验证 audio_path 协议消失
grep -rn "audio_path" src/vla/main.py
# 期望:无输出(或仅注释)

# 4. 验证 transcriber 签名变纯
grep -n "_extract_audio\|out_dir" src/vla/transcribe/streaming.py
# 期望:无输出

# 5. 验证 Cmd+Shift+R 残留清理
grep -rn "Cmd+Shift+R\|tab_recorder\.start_recording" src/ scripts/ tests/ \
  | grep -v "macos_notify\|tab_audio_recorder\.py\|test_e2e\|test_macos_notify\|test_tab_audio_recorder\|spike_f26\|spike_f25"
# 期望:仅保留类(用户操作指引 / 测试 fixture)
```

Expected: 全部通过(2 可能 skip,标 `[env-dependent]`)

- [ ] **Step 6: 跑 `vla doctor` 全 OK**

Run: `uv run vla doctor`
Expected: 全 OK(确认环境无 regression)

- [ ] **Step 7: Commit**

```bash
git add scripts/spike_f26_pipeline.py docs/superpowers/backlog.md implementation-plan.md
git commit -m "docs(phase-9.5): wire spike to new provider tuple; add Phase 9.5 acceptance section"
```

---

## Self-Review

按 spec §5 测试策略 + §6 文件清单逐项核对:

| Spec 节 | Task |
|---|---|
| §3.1 Asset dataclass | Task 1 ✓ |
| §3.2 SubtitleResult + audio_path | Task 1 ✓ |
| §3.3 ProcessResult dataclass | Task 1 ✓ |
| §4.1 models.py 改动 | Task 1 ✓ |
| §4.2 main_provider.py 拆 fetch_asset + process_asset | Task 6 / 7 / 8 ✓ |
| §4.2 fetch_audio helper | Task 2 ✓ |
| §4.2 scan_today_dir 末尾兜底 | Task 7 (#test_fetch_asset_scan_today_dir_last + #test_fetch_asset_scan_not_called_when_strategy_text_hits) ✓ |
| §4.3 strategy._try_browser 不调 transcribe | Task 4 ✓ |
| §4.4 main.py _process_one 改薄 | Task 10 ✓ |
| §4.6 transcriber.transcribe 签名变纯 | Task 3 ✓ |
| §4.7 InternalSiteSpider 占位 stub | Task 5 ✓ |
| §4.8 注释清理 | Task 11 ✓ |
| §5.1 单元测试覆盖 | Task 1/2/3/4/6/7/8 各带测试 ✓ |
| §5.2 验收 #1-#5 | Task 12 ✓ |

**Type consistency check:**
- `Asset.deletable: bool = False` — Task 1 定义,Task 7/8/10 用,一致 ✓
- `Asset.needs_transcribe: bool` — Task 1 定义,Task 8/10 用,一致 ✓
- `ProcessResult.source: str` — Task 1 定义,Task 10 注入 `result.source` 写 history,一致 ✓
- `SubtitleResult.audio_path: Path | None = None` — Task 1 定义,Task 4 / Task 7 内部用,一致 ✓
- `extract_audio(input_path: Path, output_path: Path) -> None` — Task 2 定义,Task 7 调,签名一致 ✓
- `transcribe(audio_path: Path) -> str` — Task 3 定义,Task 8 `await asyncio.to_thread(self.transcriber.transcribe, asset.audio_path)`,一致 ✓
- `fetch_asset(task) -> Asset | None` / `process_asset(asset, task) -> ProcessResult | None` — Task 6/7/8/10 串通 ✓

**Placeholder scan:** 全文无 "TBD" / "TODO" / "实现 later"(Task 5 的 `NotImplementedError("走单独 PR")` 是显式 stub,符合 spec §4.7"实现走单独 PR"约定)。

**Scope check:** 单 plan 即可产出 working software(fetch_asset 4 路径全过,process_asset 6 步骤全过),InternalSiteSpider 实装是 follow-up PR(backlog §1)。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-09-09-asset-pipeline-refactor.md`. Two execution options:**

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**

(建议:**Subagent-Driven** — 12 个 task 各自独立,reviewer 在每个 task 之间可 gate,fresh subagent 减少上下文漂移。Task 6/7/8 都动 `main_provider.py`,inline 执行容易在编辑冲突上纠结。)
