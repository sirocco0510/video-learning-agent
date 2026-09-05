# F2-9 — Final FR-2/FR-3 Regression + Release Notes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Final regression for FR-2 + FR-3 + write release notes summarizing all F2-1 ~ F2-8 work. End-state readiness.

**Architecture:**
- Test only (regression)
- Create: `docs/release-notes/2026-09-03-fr2-fr3-refactor.md` (release notes)

**Tech Stack:** pytest, grep audit, typer, pydantic

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md` §7 (Acceptance Criteria) + §8 (Out of Scope)

**User rulings locked:** Q1=Polling / Q7=Silent / Q8=Warn / Q3+Q9=Keep generic

## Global Constraints

- F2-1 ~ F2-8 must be merged before F2-9 starts
- All 491 baseline tests + new F2-* tests must PASS
- `vla doctor` green (Q8: `--check-screenshot` may print TCC warning, exit 0)
- Grep audit per spec §7 must return expected counts
- Real Bilibili smoke: optional (manual, network-dependent); spec §7 says "if network available"

---

### Task 1: Full test suite regression

**Files:**
- Test only

- [ ] **Step 1: Run full pytest**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run pytest tests/ -v 2>&1 | tee /tmp/f2-9-pytest.log
tail -50 /tmp/f2-9-pytest.log
```

Document in release notes:
- Total tests
- Pass count (target: ≥491 baseline + ~30 new F2-* tests)
- Any skip / xfail (document reason)

- [ ] **Step 2: Coverage report**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run pytest tests/ --cov=vla --cov-report=term-missing 2>&1 | tee /tmp/f2-9-cov.log
```

Document coverage % in release notes (target: ≥80% for new modules).

- [ ] **Step 3: Lint check**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run ruff check src/ tests/ 2>&1 | tee /tmp/f2-9-ruff.log
uv run ruff format --check src/ tests/ 2>&1 | tee -a /tmp/f2-9-ruff.log
```

If errors, fix inline (do NOT mark plan complete with lint errors).

- [ ] **Step 4: Commit (if fixes applied)**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add -A
git commit -m "chore(F2-9): lint fixes"
```

---

### Task 2: `vla doctor` + `--check-screenshot` smoke

**Files:**
- Smoke test only

- [ ] **Step 1: Run `vla doctor`**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run vla doctor 2>&1 | tee /tmp/f2-9-doctor.log
```

Expected: exit 0, all green checks.

- [ ] **Step 2: Run `vla doctor --check-screenshot`** (FR-2.28.2c + Q8)

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run vla doctor --check-screenshot 2>&1 | tee /tmp/f2-9-doctor-screenshot.log
echo "exit_code=$?"
```

Expected (per Q8 user ruling):
- TCC granted → "屏幕录制权限 OK (FR-2.28.2c)", exit 0
- TCC denied → "WARN: 屏幕录制权限被拒 ...", exit 0 (NOT non-zero)

Document actual result in release notes.

- [ ] **Step 3: If exit code wrong (denied → exit 1), fix `cli.py`**

Verify `_check_screenshot_tcc` (from F2-5 Task 4) doesn't call `sys.exit(1)` on denial. If it does, remove the sys.exit and re-run.

---

### Task 3: Spec §7 audit grep gates

**Files:**
- Audit only

- [ ] **Step 1: Run all audit greps**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent

echo "=== Spec §7 Audit Gates ==="

echo "1. BrowserRecorder class:"
grep -rn "class BrowserRecorder" src/vla/ ; echo "(expect 0)"

echo "2. video.record / ffmpeg screen:"
grep -rn "video\.record\|ffmpeg.*screen" src/vla/ ; echo "(expect 0)"

echo "3. probe_status / chrome.management.getAll in subtitle/:"
grep -rn "def probe_status\|chrome.management.getAll" src/vla/subtitle/ ; echo "(expect TabAudioRecorder)"

echo "4. WhisperWorkerPool / AudioQueue in audio/:"
grep -rn "WhisperWorkerPool\|AudioQueue" src/vla/audio/ ; echo "(expect new modules exist)"

echo "5. ScreenCapture / ScreenshotPhaseController in capture/:"
grep -rn "class ScreenCapture\|class ScreenshotPhaseController" src/vla/capture/ ; echo "(expect FR-2.28 modules exist)"

echo "6. SubtitlePipeline in quality/:"
grep -rn "class SubtitlePipeline" src/vla/quality/ ; echo "(expect FR-2.15c orchestrator)"

echo "7. SubtitlePipeline wiring in main.py:"
grep -rn "SubtitlePipeline" src/vla/main.py ; echo "(expect wiring)"
```

- [ ] **Step 2: Document results**

Paste results into `/tmp/f2-9-audit.log` for inclusion in release notes.

- [ ] **Step 3: If any gate fails, STOP and report**

Don't proceed to Task 4 with broken gates — fix the underlying plan first.

---

### Task 4: Real Bilibili smoke test (optional, network-dependent)

**Files:**
- Manual smoke

- [ ] **Step 1: Try the existing e2e script**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
uv run python scripts/e2e_real_bilibili.py 2>&1 | tee /tmp/f2-9-e2e.log
```

- [ ] **Step 2: If network available**

Expected:
- yt-dlp audio extraction succeeds (path ①)
- Whisper transcription runs
- `.transcript.txt` + `.cleaned.txt` + (if `refine_enabled=true`) `.refined.txt` all written

Document actual behavior. Capture timing.

- [ ] **Step 3: If network unavailable**

Skip this task; document "skipped (no network)" in release notes.

- [ ] **Step 4: Commit any new files generated by smoke**

If `logs/` directory contains new artifacts worth committing:

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add logs/ 2>/dev/null || true
git diff --cached --stat
# Only commit if intentional artifacts (not noisy logs)
```

---

### Task 5: Write release notes

**Files:**
- Create: `docs/release-notes/2026-09-03-fr2-fr3-refactor.md`

- [ ] **Step 1: Use the standard release notes template**

```markdown
<!-- docs/release-notes/2026-09-03-fr2-fr3-refactor.md -->

# Release Notes — FR-2 / FR-3 Refactor (2026-09-03)

**Spec:** `docs/superpowers/specs/2026-09-03-fr2-fr3-impl-design.md`
**Plans:** `docs/superpowers/plans/2026-09-03-f2-{1..9}-*.md`
**User rulings locked:** Q1=Polling / Q7=Silent / Q8=Warn / Q3+Q9=Keep generic

## Summary

Replace video-screen-recording-based ③ audio capture with **audio-only dual fallback**:
- **Path ①:** `yt-dlp -x` 抽音频 (FR-2.14)
- **Path ②:** Tab Audio Recorder Chrome extension via `chrome.tabCapture` (FR-2.21/2.24/2.24a/2.25)
- Silent fallback between paths (Q7)
- FR-2.15c Level 4 云端 LLM 字幕清理(SubtitlePipeline wiring)
- FR-2.28 4-phase 截图管线 (Q8 TCC deny → continue, exit 0)
- 完全删除 `BrowserRecorder` (Q2 resolved: 无 wrapper,无回退)

## New Modules

| Module | Purpose | Spec § |
|---|---|---|
| `src/vla/audio/source_factory.py` | yt-dlp `-x` 抽音频 (path ①) | §3.2 |
| `src/vla/audio/queue.py` + `worker_pool.py` | FR-2.27 worker pool (Polling model) | §3.3 + §3.4 |
| `src/vla/subtitle/tab_audio_recorder.py` | Chrome tab Capture API wrapper | §3.1 |
| `src/vla/capture/screen_capture.py` | FR-2.28 sync 系统截图 base | §3.5 |
| `src/vla/capture/screenshot_phase_controller.py` | FR-2.28 4-phase trigger (A/B/C/D) | §3.7 |
| `src/vla/quality/pipeline.py` | FR-2.15c Level 1 → Level 4 → Quality 协调 | §3.6 |

## Deleted Modules

| Module | Replaced by |
|---|---|
| `src/vla/subtitle/browser_record.py` | F2-1 (audio/source_factory) + F2-2 (tab_audio_recorder) + F2-5 (screenshot_phase_controller) |
| `tests/test_browser_record.py` | F2-1 tests + F2-2 tests + F2-5 tests |
| `src/vla/subtitle/probes/` | (如果 F2-8 Task 4 决定删) |

## Modified Modules

| Module | Change |
|---|---|
| `src/vla/subtitle/platform_adapter.py` | 默认 `fetch_via_recording` 实现 path ① → path ② (Q7 Silent) |
| `src/vla/subtitle/bilibili_adapter.py` | 注入 audio_factory / tab_recorder / transcriber / screenshot_controller |
| `src/vla/subtitle/internal_site_adapter.py` | 同上 |
| `src/vla/subtitle/strategy.py` | 调用更新后的 default impl |
| `src/vla/quality/__init__.py` | export `SubtitlePipeline` + `PipelineResult` |
| `src/vla/main.py` | 调 `SubtitlePipeline.run()` 替换 3-step 串行调用 |
| `src/vla/cli.py` | 新增 `--check-screenshot` 选项 (FR-2.28.2c + Q8 Warn) |

## Test Counts

<!-- Fill from Task 1 Step 1 / Step 2 -->
- Total tests: <N>
- New F2-* tests: <N>
- Coverage: <N>%
- Lint: pass / fail

## Audit Gates (spec §7)

<!-- Fill from Task 3 Step 1 -->
- `class BrowserRecorder` 引用: 0 ✅
- `video.record / ffmpeg.*screen` 引用: 0 ✅
- `TabAudioRecorder.probe_status`: src/vla/subtitle/tab_audio_recorder.py ✅
- `WhisperWorkerPool / AudioQueue`: src/vla/audio/{queue,worker_pool}.py ✅
- `ScreenCapture / ScreenshotPhaseController`: src/vla/capture/ ✅
- `SubtitlePipeline`: src/vla/quality/pipeline.py ✅
- `SubtitlePipeline` wired in main.py ✅

## User-facing behavior changes (NONE — spec §2 invariant)

- FR-1 ~ FR-10 可观测行为不变
- 失败的 url 仍走 `transcribe_fail` 日志
- 字幕质量不过仍走 `quality_skip`
- 6h 累计仍触发 batch summary + session stop
- macOS 屏幕录制权限:首次 Tab Audio Recorder 触发时仍需授权;若拒绝 → warn,继续(Q8)

## Migration Notes

- `BilibiliAdapter` / `InternalSiteAdapter` 必须注入 4 个新依赖(breaking for direct callers)
- `main.py` 内部调用方无感(已自动 wiring)
- `VideoSourceRecordConfig` 引用已清(Q2 + R-8 of refactor-consolidation)
- 旧 `BrowserRecorder` users:迁移到 `AudioSourceFactory` (path ①) 或 `TabAudioRecorder` (path ②)

## Out of Scope (spec §8)

- requirements.md 不改(SSOT)
- implementation-plan.md 不改(冻结历史快照)
- scripts/ 清理(后续)
- Tab Audio Recorder extension 开发(三方,只 trigger)
- 真实网络测试在 CI(手动 scripts/e2e_real_bilibili.py)
- Internal site adapter 真实实现(保持 NotImplementedError stub)

## Follow-ups

- F2-4 计划已写但未执行的部分(具体见 plans/R-9-.../...)
- Q3+Q9 决定的 PluginStatus generic 化(可能在 R-17+ plan 中)
- 真实 B 站 smoke 结果(如果 Task 4 执行了)
```

- [ ] **Step 2: Replace placeholders with actual data from Tasks 1-4**

Fill in `<N>` with real test counts, coverage %, audit gate results.

- [ ] **Step 3: Commit release notes**

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git add docs/release-notes/2026-09-03-fr2-fr3-refactor.md
git commit -m "docs(F2-9): FR-2/FR-3 refactor release notes"
```

---

## Acceptance Criteria (spec §7 Final)

- [ ] All 491 baseline tests + new F2-* tests PASS
- [ ] `vla doctor` exit 0
- [ ] `vla doctor --check-screenshot` exit 0 even when TCC denied (Q8)
- [ ] All spec §7 audit gates return expected counts
- [ ] Release notes committed with real data
- [ ] No lint errors
- [ ] Real Bilibili smoke (optional, network-dependent) — pass or skipped with reason

## Final Sign-off

After all 5 tasks green:

```bash
cd /Users/mima1234/sirocco的知识库/01_Projects/video-learning-agent
git log --oneline -20
```

Show user the commit history covering F2-1 → F2-9 (each plan should produce 1+ commits) + this plan's commit.

**FR-2 + FR-3 refactor complete. Ready for next spec or feature work.**