# R.11 — Full Regression + Acceptance Sweep (FINAL)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After R.1 through R.10 + R.12 through R.15 are merged, run the full project regression: every test, every acceptance code block from `implementation-plan.md`, `vla doctor`, plus one real Bilibili end-to-end smoke. This plan MUST be the last to merge.

**Architecture:** Pure verification plan. No code changes. Every step is "run + assert + commit log entry".

**Tech Stack:** uv, pytest, typer, playwright

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §F (acceptance criteria)

## Global Constraints

- This plan blocks the refactor release
- Any failure → STOP, do not mark complete; re-open the responsible R.N plan
- Doctor + tests + 1 real run = the three gates

---

### Task 1: Run full pytest

- [ ] **Step 1: Full test run**

Run: `uv run pytest -v --tb=short 2>&1 | tee /tmp/r11-pytest.log`

Expected: All pass. Capture the count.

- [ ] **Step 2: Coverage check (if configured)**

Run: `uv run pytest --cov=vla --cov-report=term-missing 2>&1 | tail -50`

Expected: Coverage report exists (target: ≥ 70% on `src/vla/`).

- [ ] **Step 3: No skipped tests beyond the documented ones**

Run: `uv run pytest -v -rs 2>&1 | grep -E "SKIPPED|XPASS" | head -30`

Expected: only pre-existing skip markers (`@pytest.mark.skip(reason=...)`) appear.

---

### Task 2: Run every acceptance code block from `implementation-plan.md`

**Files:**
- Reference: `implementation-plan.md` — 9 Phases, each ends with a "验收代码" block

- [ ] **Step 1: Phase 1 — LLM client smoke**

Run: `uv run python -c "from vla.llm.client import LLMClient; print(LLMClient.from_env().chat('ping'))"`

Expected: short string response; no exception.

- [ ] **Step 2: Phase 2 — video source factory**

Run: `uv run python -c "from vla.source.video_source import VideoSourceFactory; print(VideoSourceFactory.from_config())"`

Expected: factory instance, no exception.

- [ ] **Step 3: Phase 3 — subtitle strategy**

Run: `uv run python -c "from vla.subtitle.strategy import SubtitleStrategyRegistry; print(SubtitleStrategyRegistry.default().platforms())"`

Expected: at least one registered strategy.

- [ ] **Step 4: Phase 4 — transcribe**

Run: `uv run python -c "from vla.transcribe.whisper_runner import WhisperRunner; print(WhisperRunner.from_config())"`

Expected: runner instance.

- [ ] **Step 5: Phase 5 — quality**

Run: `uv run python -c "from vla.quality.checker import QualityChecker; print(QualityChecker.from_config())"`

Expected: checker instance.

- [ ] **Step 6: Phase 6 — UI**

Run: `uv run python -c "from vla.ui.notifier import MacOSNotifier; print(MacOSNotifier())"`

Expected: notifier instance.

- [ ] **Step 7: Phase 7 — summary**

Run: `uv run python -c "from vla.summary.llm_summarizer import LLMSummarizer; print(LLMSummarizer.from_config())"`

Expected: summarizer instance.

- [ ] **Step 8: Phase 8 — main loop**

Run: `uv run python -c "from vla.main import run_session; print(run_session.__doc__)"`
Expected: docstring printed.

- [ ] **Step 9: Phase 9 — quota**

Run: `uv run python -c "from vla.state.quota import QuotaTracker; t = QuotaTracker.from_config(); print(t.status())"`

Expected: status dict with `seconds_used`, `threshold_sec`.

---

### Task 3: `vla doctor`

- [ ] **Step 1: Doctor green**

Run: `uv run vla doctor`

Expected: every line shows ✓ (green check); no ✗ or warnings.

- [ ] **Step 2: Doctor exit code**

Run: `uv run vla doctor; echo "exit=$?"`

Expected: `exit=0`.

---

### Task 4: Real Bilibili smoke

- [ ] **Step 1: Process one short video, end-to-end**

Run:
```bash
uv run vla process \
    --url "https://www.bilibili.com/video/BV1yyMQ6kEh6/" \
    --duration 60 \
    --output ./tmp/r11-smoke/
```

Expected:
- Exit code 0
- Output directory contains `transcript.txt`
- Notification (B 级) shown

- [ ] **Step 2: Inspect transcript**

Run: `cat ./tmp/r11-smoke/transcript.txt | head -30`

Expected: title header + ≥ 1 line of non-empty Chinese text.

- [ ] **Step 3: Cleanup**

Run: `rm -rf ./tmp/r11-smoke/`

---

### Task 5: Spec coverage audit

**Files:**
- Reference: `docs/superpowers/specs/2026-09-03-refactor-consolidation.md`

- [ ] **Step 1: Walk through every R.N reference in the spec**

For each of R.1 through R.15, confirm:
1. The corresponding plan file exists in `docs/superpowers/plans/`
2. The plan was committed (run `git log --oneline -- docs/superpowers/plans/R-NN-*.md | head -1`)
3. The plan's acceptance criteria are met

Expected: 15/15 plans present + committed + criteria met.

- [ ] **Step 2: Run final grep audit**

```bash
# Stale wrappers
grep -rn "_pause_page_video\|_record_screen\|FailureAlert\|failure_alert" src/ tests/
# Stale duplicate Protocol / parser
grep -rn "LLMClientLike\|class .*Protocol" src/vla/llm/
# Stale hardcoded probe chain
grep -rn "head_request\|referer_check\|cookie_warmup" src/vla/subtitle/browser_record.py
# Stale inline page close in strategy
grep -n "page.close" src/vla/subtitle/browser_record.py
```

Expected: only legitimate references remain.

- [ ] **Step 3: Write release notes**

Create `docs/superpowers/releases/2026-09-03-refactor.md`:

```markdown
# Refactor Consolidation Release (2026-09-03)

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md`

## What changed

- **R.1** Parser hoisted to `llm/response.py` (brace-counting JSON)
- **R.2** Single `LLMClientLike` Protocol in `llm/client.py`
- **R.3** Shared prompt utilities (`build_chat_prompt`, `enforce_json_response`)
- **R.4** Deduplicated `models.py` (single `VideoTask` / `SubtitleResult` / `QualityResult`)
- **R.5** New `utils/bvid.py` (single bvid extraction)
- **R.6** New `log/transcribed_file.py` (canonical write/read)
- **R.7** Deleted `FailureAlert` (dead code)
- **R.8** Deleted `_record_screen` + `VideoSourceRecordConfig` (network fail = error exit)
- **R.9** Deleted `_pause_page_video` compat wrapper
- **R.10** New `LLMConfig` sub-config with YAML back-compat
- **R.12** New `subtitle/normalize.py` (SRT/VTT/ASS → `list[Segment]`)
- **R.13** New `utils/json_walk.py` (recursive string walker)
- **R.14** `ProbeStrategy` Protocol + default probes (head/referer/cookie)
- **R.15** Page lifecycle: caller-owned (no implicit `page.close()`)

## Verified

- `uv run pytest -v` — all green
- `uv run vla doctor` — all checks pass
- Real Bilibili smoke — transcript produced end-to-end

## Breaking changes

- `VideoSource.get()` now raises `DownloadError` on network failure (no fallback to screen recording)
- `SubtitleRefiner.refine()` consumes `list[Segment]`, not a `Path`
- `BrowserRecordStrategy.record()` does NOT close the page (caller does)
```

---

### Task 6: Final commit + tag

- [ ] **Step 1: Commit release notes**

```bash
git add docs/superpowers/releases/2026-09-03-refactor.md
git commit -m "docs(release): refactor consolidation release notes"
```

- [ ] **Step 2: Tag**

```bash
git tag -a "refactor-consolidation-2026-09-03" -m "Refactor consolidation complete (R.1–R.15)"
git push origin refactor-consolidation-2026-09-03
```

- [ ] **Step 3: Final summary**

Print:

```
Refactor consolidation complete.
- 15 R.N plans executed
- Doctor: ✓
- Tests: ✓ (count from Task 1)
- Real run: ✓ (transcript file path from Task 4)
- Release notes: docs/superpowers/releases/2026-09-03-refactor.md
- Tag: refactor-consolidation-2026-09-03
```
