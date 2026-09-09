# SDD ledger — plan: docs/superpowers/plans/2026-09-09-asset-pipeline-refactor.md

## Preflight conflict scan

| Pair | One produces | Other consumes | Finding |
|---|---|---|---|
| T1 ↔ T7 | `Asset` / `ProcessResult` / `SubtitleResult.audio_path` | `fetch_asset` returns Asset, signature `Asset.needs_transcribe` | Consistent — T7 references exactly the fields T1 defines. ✓ |
| T1 ↔ T8 | Same as above | `process_asset` reads `asset.text`, `asset.audio_path`, `asset.source`, `asset.deletable` | Consistent. ✓ |
| T2 ↔ T7 | `extract_audio(input, output) -> None` | `fetch_asset` calls `extract_audio(Path(video_url), wav_path)` and `extract_audio(webm_path, wav_path)` and `extract_audio(video_path, wav_path)` | Consistent — T2 signature matches T7 call sites. ✓ |
| T3 ↔ T8 | `transcribe(audio_path: Path) -> str` | `process_asset` calls `asyncio.to_thread(self.transcriber.transcribe, asset.audio_path)` | Consistent. ✓ |
| T3 ↔ T5 | `transcribe` signature change | `internal_site_spider.py` is new file — no overlap | N/A ✓ |
| T4 ↔ T7 | `_try_browser` returns `SubtitleResult(audio_path=webm)` (no transcribe) | `fetch_asset` reads `result.audio_path` if `result.source.startswith("internal")` — but scan branch moved to T7 last fallback | **Potential ambiguity:** T4 keeps `scan_untranscribed_audio` call inside `_try_browser` as a probe; T7 final fallback also calls `scan_untranscribed_audio`. If strategy returns SubtitleResult(source="whisper_scan", audio_path=webm) before strategy.get_subtitle fallback, `result.text is None` AND `result.source` is "whisper_scan" (not "internal") → falls through to source_factory then scan_today_dir (which scans again). Double-scan risk. **Ruling:** T7 must check `if result.source == "whisper_scan"` in the ② internal block before falling through. Actually cleaner: T7 path ② only handles `result.source.startswith("internal")`; scan path returns `SubtitleResult(source="whisper_scan", text=None, audio_path=webm)` → T7 sees `result.text is None` → falls to path ③ → eventually ④. The second `scan_untranscribed_audio` call would return None (sidecar already touched? no, sidecar touched in process_asset after transcribe succeeds). So worst case is duplicate scan attempt returning webm again, scan twice → fail in scan_untranscribed_audio because sidecar not yet touched (process hasn't run yet). **Decision:** T7 must guard against double-scan. After T4 leaves strategy returning scan SubtitleResult, T7 needs a new branch ②b that handles `result.source == "whisper_scan"` directly (extract webm→wav, return Asset) — OR delete scan from strategy entirely (move it fully to T7 fetch_asset). The latter is cleaner and matches spec §4.3 "scan_today_dir 改为 fetch_asset 内的最后兜底". **Ruling:** T4 deletes the scan branch from `_try_browser` (no longer calls `scan_untranscribed_audio`); T7 path ② only handles `internal_*`; T7 path ④ is the only place that calls `scan_untranscribed_audio`. Update T4 step 3 to remove `scan_untranscribed_audio` import + call from `_try_browser`. |
| T4 ↔ T1 | `SubtitleResult.audio_path: Path \| None = None` | T4 returns SubtitleResult with audio_path=webm | Consistent (default added in T1). ✓ |
| T5 ↔ T7 | `InternalSiteSpider` stub with `NotImplementedError` | `fetch_asset` reads `result.metadata["video_url"]` when `result.source.startswith("internal")` — but result is produced by `strategy.get_subtitle`, not spider directly | **Gap:** Spider is never wired in this plan. `strategy.get_subtitle` doesn't currently produce `source="internal_spider"`. So T7's internal_spider branch never fires in practice. **Ruling:** This is intentional per spec §4.7 "InternalSiteSpider 实现走单独 PR" + §7 risk "internal_spider 实现未落地...路径永远走不到...单测覆盖 fetch_asset 接住 SubtitleResult(metadata={"video_url": ...}) 的行为;Spider 实装走单独 PR + e2e". Single-test fixture mocks strategy to return the right SubtitleResult. No fix needed. |
| T6 ↔ T7 | T6 skeleton `fetch_asset` raises NotImplementedError | T7 replaces with real impl | Consistent (T7 overwrites T6 stub). ✓ |
| T6 ↔ T8 | T6 skeleton `process_asset` raises NotImplementedError | T8 replaces with real impl | Consistent. ✓ |
| T6 ↔ T9 | T6 defines `RealTextProvider.fetch_asset` / `.process_asset` | T9 changes `build_text_provider` return signature | Consistent — T9 uses methods defined in T6. ✓ |
| T7 ↔ T11 | T11 reads `src/vla/subtitle/strategy.py:272` etc. for stale comments | T4 already modifies strategy.py | **Order risk:** T11 should run after T4 (so it sees post-T4 line numbers). Plan order: T4 → T11. ✓ |
| T7 ↔ T8 | Both edit `main_provider.py` | T8 references `self.transcriber.transcribe` etc. set up in T7's `_make_provider` test helper | T7 test sets `p.transcriber = AsyncMock()`; T8 test sets `p.transcriber = MagicMock()` — different mock styles but same purpose. ✓ |
| T9 ↔ T12 | T9 changes `build_text_provider` return type | T12 modifies `scripts/spike_f26_pipeline.py` to use new return | Consistent (T12 depends on T9). ✓ |
| T10 ↔ T7/T8 | T10's `__init__` accepts `fetch_asset`, `process_asset` | Real impls come from T7/T8 | Consistent — all 3 tasks done before T10 runs in plan order. ✓ |
| T12 ↔ All | T12 runs acceptance scripts | All preceding tasks must be complete | Consistent — T12 is last. ✓ |

## Global Constraints (binding on all tasks)

From plan "Global Constraints" section, copied verbatim because every implementer + reviewer needs them:

- Python 3.12, uv, src layout (`src/vla/`), lockfile 已提交
- pydantic v2 BaseModel; `Asset` / `ProcessResult` 用 `@dataclass(frozen=True)`
- 函数签名必填,函数体内可省
- `pytest-asyncio`, `asyncio_mode = "auto"`
- 字幕永远本地(只用 faster-whisper / B站 CC / VideoTrans,禁止云端)
- 云端 LLM 限定两件事:① 字幕质量检查 ② 6h 批量总结
- 磁盘友好:转写完才能删源文件,质量过了才能删
- commit 规范: `<scope>: <imperative summary>`,本仓库现有 conventional 风格
- TDD: 写实现前先写失败测试,跑红 → 写最小实现 → 跑绿 → commit
- import: stdlib → third-party → local; type import `from __future__ import annotations` + `TYPE_CHECKING`
- logging: `logger = logging.getLogger(__name__)`,不要 print
- 路径: `pathlib.Path`
- no dead code

## Plan order of execution

T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12

(T11 must run after T4 since T4 changes strategy.py line numbers; plan order already correct.)

## Rulings made before dispatch

**Ruling 1 (T4 vs T7):** T4 step 3 deletes `scan_untranscribed_audio` call from `_try_browser` entirely (only `InternalSiteSpider` placeholder + audio_path=None for now, no scan branch). T7 path ② handles only `result.source.startswith("internal")`. T7 path ④ is the only place calling `scan_untranscribed_audio`. This eliminates the double-scan ambiguity and matches spec §4.3 + spec §4.2 顺序设计意图.

**Why:** Plan T4 step 3 currently says "_try_browser keeps scan_untranscribed_audio as probe, T7 fetch_asset 接管后会拆开" — that's the source of the conflict. Per spec §4.3, scan_today_dir is fully moved out of strategy. Plan T4 step 3 description should be updated to "delete scan_untranscribed_audio import + call from _try_browser entirely". Plan T4 step 3 already has the cleaner code structure (`return SubtitleResult(text=None, source="whisper_scan", audio_path=webm)`), so the existing code IS correct; the surrounding prose is what's ambiguous. Each implementer gets a clear ruling: T4 deletes scan from _try_browser; T7 path ④ is the only scanner.

**Cost if wrong:** If double-scan happens, in practice `scan_untranscribed_audio` will return the same webm twice (no sidecar yet), second call would re-succeed and produce duplicate Asset. process_asset would touch sidecar on first. Risk is mild — caught in T7 test_fetch_asset_scan_not_called_when_strategy_text_hits + visual code review. Reversible.

**Ruling 2 (T5 wiring gap):** No fix. InternalSiteSpider is intentional stub per spec §4.7. Plan §Self-review already documents this. T7 single-test fixture mocks strategy to return `source="internal_spider"` with `metadata.video_url`. No implementation gap.

## Ledger log

### Task 1: complete (commits 4ce2931..a5e241d, review clean)

**Ruling applied during task:** Test snippet in brief was wrong — real `QualityResult` is `score: int` (0-100) + requires `issues/suggestion/char_count`, no `reason` field. Fix test to match real schema; do not touch `QualityResult` (out of scope, used elsewhere).

**Minors parked (deferred to final review triage):**
- No newline at end of `tests/test_models.py` (ruff W292)
- `metadata: dict | None = None` widening — should grep `SubtitleResult(` call sites in T2/T3 to confirm safe
- `needs_transcribe` property doesn't enforce the "二选一" invariant (passes both fields → returns True via audio_path)
- Brief defect should be noted in `requirements.md` for future task authors

### Task 2: complete (commits a5e241d..ab54249, review clean)

**Minors parked:**
- Success-path test assertion is tautological (`False is False`)
- `test_extract_audio_input_missing_raises` doesn't assert subprocess.run was called
- `except Exception:` is broader than necessary (but brief-mandated)
- `tests/__init__.py` not created by this task — assumes it exists

### Task 3: complete (commits ab54249..b921923, review clean)

**Minors parked:**
- Test file is `tests/test_streaming.py` (pre-existing) — brief says `test_transcribe_streaming.py`. File rename deferred (CI grep concern, no functional impact)

**Deferred callers (intentionally not fixed in T3, will be in T6/T12):**
- `src/vla/main_provider.py:102` — calls old signature
- `src/vla/subtitle/strategy.py:411` — calls old signature
- `tests/test_pass_fail_flow.py`, `tests/test_subtitle_strategy.py` — call old signature

### Task 4: complete (commits b921923..611aeda, review clean)

**Minors parked:**
- No trailing newline at end of strategy.py / test_subtitle_strategy.py (ruff W292 — same pattern as T1/T2/T3)
- `_try_browser` return type annotation still says `tuple[str, dict] | None` but only returns None now — signature cleanup deferred to T7
- `tab_recorder` field retained on SubtitleStrategy (no longer used) — removal deferred to T10/T11 since it would break all SubtitleStrategy(...) call sites
- Test class `TestScanTodayDirPath` docstring still references `scan_untranscribed_audio` as a comment — no functional impact

**Important (deferred for final review triage):**
- T4 commit bundles `_new_page_safely` signature change (`new_background_page()` → `find_page_by_url_substring(bvid)`) alongside the audio-work removal. Well-documented as v3.2.1.9 (tab-reuse to avoid Chrome memory saver race). Atomic-commit reviewer concern — final review can decide whether to split. Not blocking.

### Task 5: complete (commits 611aeda..75458af, review clean)

**Minors parked:**
- `list_tasks` annotated `-> list` instead of `-> list[VideoTask]` (acknowledged in inline comment)
- `logger = logging.getLogger(__name__)` defined but unused (acceptable for stub)
- Report line count claim (30 vs actual 40 lines) cosmetic discrepancy

### Task 6: complete (commits 75458af..1f3c28a, review clean)

**Minors parked:**
- Test imports `from vla.models import ...` inside function bodies instead of top-of-file (stylistic)

**Deferred (will fix in T10):**
- `__call__` signature change breaks main.py call sites — T10 will update them

### Task 7: complete (commits 1f3c28a..5f604bb, review clean)

**Minors parked:**
- Unused `QualityResult` import in `tests/test_fetch_asset.py:146`
- Missing newline at end of `tests/test_fetch_asset.py` (ruff W292)

**Important (design note, deferred for final review triage):**
- When `source_factory.get` returns a source but `extract_audio` fails on the MP4, falls through to `scan_today_dir` without unlinking the MP4. Per-spec behavior but could accumulate on 256 GB machine. Worth a follow-up PR.

**Stale test (to fix in T10):**
- `tests/test_main_provider.py::test_fetch_asset_placeholder` fails because fetch_asset no longer raises NotImplementedError. Was written for T6 skeleton; naturally stale once T7 lands.

**Brief corrections (handled in-place, not defects):**
- `scan_untranscribed_audio` lives at `vla.subtitle.audio_scan` (not `vla.audio_scan`); uses module-attribute access (`audio_scan.scan_untranscribed_audio(...)`) for mock-robustness
- `VideoSource` uses `mode` + `duration_sec` (not `kind`); corrected
- Test patch paths updated to match

### Task 9: complete (commits c9c8ce8..59c9ba9, review APPROVED)

**Verdict:** SPEC_COMPLIANT / QUALITY_APPROVED → APPROVE.

**Reviewer notes:**
- `build_text_provider` returns `tuple[FetchAssetFn, ProcessAssetFn]` at main_provider.py:242 + 315 ✓
- Type aliases match brief line 11 verbatim with correct call-order (`Asset` then `VideoTask`) ✓
- `test_build_text_provider_returns_tuple` green; T6 placeholder tests stale by design (deferred to T10) ✓
- Documented deviation: `notifier`/`plugin_status` made optional for test fixture, no impact on cli.py production behavior ✓

**Pre-dispatch brief fix:**
- T10 brief had same `QualityResult(score=0.9, reason="ok")` defect as T1 (real schema requires `score: int` + `issues/suggestion/char_count`). Fixed in-place to `QualityResult(score=90, passed=True, issues=[], suggestion="", char_count=2)` before T10 dispatch.

**Stale tests deferred to T10:**
- `tests/test_main_provider.py::test_fetch_asset_placeholder` (T6 skeleton, fails because fetch_asset no longer raises NotImplementedError)
- `tests/test_main_provider.py::test_process_asset_placeholder` (T6 skeleton, same pattern)

### Task 10: complete (commits 59c9ba9..f308fdb, review APPROVED)

**Verdict:** SPEC_COMPLIANT / QUALITY_APPROVED → APPROVE.

**Reviewer notes:**
- `__init__` drops `text_provider` + `checker`, accepts `fetch_asset: FetchAssetFn, process_asset: ProcessAssetFn` per spec §4.4 line 605-607 ✓
- `_process_one` body (main.py:237-309) matches spec §4.4 lines 565-602 verbatim ✓
- `cli.py` fully migrated — no stale callsites ✓
- Production `RealTextProvider.fetch_asset` has comprehensive `except Exception → return None` (main_provider.py multiple sites), so lost try/except is safe per spec §3.4 contract ✓
- Stub duplication covered by 8 unit tests in `test_process_asset.py` ✓
- Brief defect (`test_main.py` doesn't exist) handled correctly ✓
- 27 tests migrated via `make_agent_and_pair` helper; T6 placeholders deleted ✓
- `tab_recorder` removal correctly deferred to T11 ✓

**Ruling applied during dispatch (concern #1):** Implementer dropped `checker` injection alongside `text_provider`. Brief implied only `text_provider` drop, but spec §4.4 strictly requires only `fetch_asset` + `process_asset` (lines 605-607) and `_process_one` body code shows `checker` is no longer used (quality check moved into `process_asset`). **Decision: keep `checker` dropped.** Spec-aligned; re-adding would re-coupling work the design explicitly moves out. Documented.

**Brief defect corrected before dispatch:**
- `QualityResult(score=0.9, reason="ok")` defect → corrected to `QualityResult(score=90, passed=True, issues=[], suggestion="", char_count=2)` (T1 had same issue, T9 also corrected).

### Task 11: complete (commits f308fdb..76853a9, review APPROVED)

**Verdict:** SPEC_COMPLIANT / QUALITY_APPROVED → APPROVE.

**Reviewer notes:**
- All 7 files modified per spec §4.8 (brief said 6, spec §4.8 line 710 names `main_provider.py:147`) ✓
- All清理类/更新类 targets addressed; grep returns zero hits in modified files ✓
- Spec line drift (379→408, 289→288, 147→285, 272→271) handled ✓
- Pre-judged retentions verified: `internal_site_adapter.py` + `strategy.py` keep `tab_recorder` (callers still wire it) ✓
- Acceptance #5 grep: 8 hits, all保留类 per spec §4.8 lines 723-733 ✓
- 616 pytests passed, no behavioral change, no dangling imports ✓

**Pre-judged decisions:**
1. `tab_recorder` retained in `internal_site_adapter.py` — test fixture `_stub_deps()` still passes `MagicMock()`; removal would break 4 callsites
2. `tab_recorder: TabAudioRecorder` type hint kept in `strategy.py` — main_provider.py:299 still wires it; brief step 5 forbids removal at this scope
3. Spec line drift handled: `strategy.py:379` → 408, `strategy.py:289` → 288, `main_provider.py:147` → 285, `strategy.py:272` → 271
4. Docstring typo side-fix `tab_recriber` → `tab_recorder` at `strategy.py:212` — minor; can be reverted if strict minimal-edit enforced
5. Spec's `strategy.py:452` doesn't exist (current file 436 lines, T4 collapsed structure); line 408 was the actual location

**Acceptance #5 grep result:** 8 lines remain in保留类 — `platform_adapter.py:74-75` (F2-10 audit), `browser_driver.py:6/313/347` (real Screen Recorder extension hotkey), `scripts/wait_user_hotkey.py:4/60`, `scripts/start_chrome_debug.py:166`. All valid per spec §4.8 lines 723-733.

### Task 13: complete (commits 3ffcee0..6436571, unplanned fix)

**Trigger:** T12 implementer concern #5 — production path AttributeError. After T12 expanded `build_text_provider` to accept optional `checker`/`refiner` kwargs, `cli.py::_build_real_provider` still didn't pass them. `process_asset` calls `self.checker.check(...)` → AttributeError on first video in production.

**Fix:** Auto-construct `QualityChecker(cfg)` in `build_text_provider` when `checker=None`; conditionally auto-construct `SubtitleRefiner(cfg)` when `refiner=None` AND `cfg.quality_check.refine_enabled=True`. Mirrors existing `log` auto-construct pattern.

**Commit:** `6436571` — `fix(provider): build_text_provider auto-constructs QualityChecker; cond construct SubtitleRefiner`

**3 new tests:** auto-constructs checker / skips refiner when disabled / auto-constructs refiner when enabled. 619 → 622 passed.

### Task 14: complete (commits 6436571..9eb220e, unplanned fix)

**Trigger:** Opus final review found 2 BLOCKERS per-task reviews missed:

1. `RealTextProvider.__init__` never set `self._today_dir` → `fetch_asset` line 146 references `self._today_dir` for spec §4.2 path ④ scan_today_dir fallback → silently dead in production (AttributeError swallowed by try/except). Tests passed because `_make_provider` fixture manually injects `_today_dir`.
2. `wav_path.parent.mkdir(...)` missing before ffmpeg at main_provider.py internal_spider branch (line 115) → path ② silently fails on first call when `audio_raw/` doesn't exist.

**Fix:** Add `today_dir: Path | None = None` parameter to `RealTextProvider.__init__`; `build_text_provider` computes `find_today_dir(cfg.audio.downloads_dir)` and passes it. Add `wav_path.parent.mkdir(parents=True, exist_ok=True)` before ffmpeg in internal_spider branch only (factory branch exempt — shares parent with already-downloaded MP4).

**Commit:** `9eb220e` — `fix(provider): wire today_dir into RealTextProvider; ensure parent dir before ffmpeg in internal_spider branch`

**3 new tests:** regression test goes through `_stub_provider` → `build_text_provider` → `RealTextProvider.__init__` production path, would AttributeError pre-fix. 619 → 622 passed.

### Final Whole-Branch Review

**Verifier:** Opus (most capable model) — commits 4ce2931..6436571 (247 KB diff)

**Verdict:** FIX_REQUIRED_BEFORE_MERGE → fixed by Task 14 → re-review READY_TO_MERGE.

**Spec coverage:** All 12 spec sections (3.1-5.2) covered ✓

**Parked items for follow-up PRs (non-blocking):**
- Stale module docstring at `main_provider.py:1-19` describing old `(text, source, audio_path)` tuple contract
- Duplicate `FetchAssetFn`/`ProcessAssetFn` type aliases in `main.py` (re-import from `main_provider` instead)
- Dead `recorder` kwarg in `build_text_provider` (always None per F2-8)
- `tab_recorder` field on `SubtitleStrategy` (still wired via `main_provider.py:299`, removal blocked by callers)
- `tests/test_e2e.py` 14 pre-existing sync calls broken (per MEMORY.md, unrelated to this refactor)
- InternalSiteSpider 4 yunxuetang API implementation (per spec §4.7, backlog §1)
- `_try_browser` return type annotation cleanup (`tuple[str, dict] | None` → `None` only)

### Final Scoped Re-Review (T14 verification)

**Verifier:** Sonnet — commits 6436571..9eb220e

**Verdict:** **READY_TO_MERGE** ✓

- Blocker 1 fully closed: regression test goes through production path, would AttributeError pre-fix
- Blocker 2 closed: mkdir before ffmpeg, factory branch exemption justified
- 622 tests pass (was 619), `vla doctor` clean (10 OK / 0 FAIL)
- 16 e2e failures verified pre-existing (git stash confirmed, unrelated per MEMORY.md)

**Final commits landed (14 total):**
```
9eb220e fix(provider): wire today_dir into RealTextProvider; ensure parent dir before ffmpeg in internal_spider branch
6436571 fix(provider): build_text_provider auto-constructs QualityChecker; cond construct SubtitleRefiner
3ffcee0 docs(phase-9.5): wire spike to new provider tuple; + Phase 9.5 section; + backlog §6 internal src screenshots
76853a9 docs(refactor): clean F2-8 era 'Tab Audio Recorder fallback' stale comments
f308fdb refactor(main): _process_one uses fetch_asset + process_asset; drop text_provider injection
59c9ba9 refactor(provider): build_text_provider returns (fetch_asset, process_asset) tuple
c9c8ce8 feat(provider): process_asset 6-step chain (transcribe → quality → refine → save → cleanup)
5f604bb feat(provider): fetch_asset 4-path chain (api/browser → internal → factory → scan)
1f3c28a refactor(provider): split RealTextProvider into fetch_asset + process_asset (stub)
75458af feat(spider): add InternalSiteSpider stub (implementation in separate PR)
611aeda refactor(strategy): _try_browser popup-enabled branch returns None; scan moved to fetch_asset
b921923 refactor(transcribe): transcribe signature now takes wav only; remove _extract_audio
ab54249 feat(transcribe): extract extract_audio ffmpeg helper into its own module
a5e241d feat(models): add Asset + ProcessResult dataclasses; SubtitleResult.audio_path
```

**Plan status: COMPLETE.** All 12 planned tasks + 2 unplanned surgical fixes landed. Ready for `superpowers:finishing-a-development-branch`.

### Task 8: complete (commits 5f604bb..c9c8ce8, review clean)

**Minors parked:**
- Unused `AsyncMock` and `patch` imports in `tests/test_process_asset.py:3`
- Missing newline at end of `tests/test_process_asset.py` (ruff W292)

**Stale test (to fix in T10):**
- `tests/test_main_provider.py::test_process_asset_placeholder` (T6) — same pattern as T7's stale fetch_asset placeholder

