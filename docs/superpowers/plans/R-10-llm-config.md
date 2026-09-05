# R.10 — Extract `LLMConfig` (Model Selection Consolidation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the three `model` fields (`quality_check.model`, `quality_check.refine_model`, `summary.model`) into a single `LLMConfig` sub-config under `cfg.llm.*`. Keep backward compat: old YAML keys still parse to the new structure.

**Architecture:**
- New `LLMConfig(BaseModel)` with fields: `refine_model: str`, `quality_model: str`, `summary_model: str`
- New field on `VLAConfig`: `llm: LLMConfig`
- Old YAML keys (`quality_check.model`, `quality_check.refine_model`, `summary.model`) are read via a `model_validator(mode="before")` that normalizes them to `llm.*`
- Internal code reads `cfg.llm.quality_model` etc.; legacy accessors remain as `@property` aliases for back-compat in tests

**Tech Stack:** pydantic v2

**Spec:** `docs/superpowers/specs/2026-09-03-refactor-consolidation.md` §C #11

## Global Constraints

- YAML forward-compat: existing `config/vla.yaml` with `quality_check.model` still loads without edit
- CLI / run path must continue to work — `vla doctor` green
- Tests stay green (35+)

---

### Task 1: Write failing config tests

**Files:**
- Create (or modify): `tests/test_config.py`

**Interfaces:**
- Produces: `cfg.llm.refine_model`, `cfg.llm.quality_model`, `cfg.llm.summary_model` (str)

- [ ] **Step 1: Write tests**

```python
# tests/test_config.py (new file or add to existing)
from pathlib import Path
import yaml

import pytest

from vla.config import LLMConfig, VLAConfig


SAMPLE_YAML = """
storage:
  tmp_dir: "./tmp"
  auto_cleanup_on_pass: true
whisper:
  model: "small"
  language: "zh"
  segment_seconds: 30
  compute_type: "int8"
video_source:
  prefer_download: true
  download:
    format: "best"
quality_check:
  enabled: true
  model: "test-quality-model"
  refine_model: "test-refine-model"
  min_score_to_pass: 70
  min_char_per_second: 1.0
  max_char_per_second: 15.0
browser_plugin:
  name: "X"
  enabled: true
  remind_timeout_sec: 30
  plugin_paths: ["~/Downloads"]
summary:
  model: "test-summary-model"
  target_words_min: 500
  target_words_max: 800
  notes_file: "./notes.md"
  cross_video_dedup: true
  trigger_mode: "quota"
  notes_section_header: "## {group_title}"
quota:
  summary_threshold_sec: 21600
  on_exhausted: "stop_session"
history:
  file: "./logs/history.jsonl"
logging:
  log_dir: "./logs"
  notify_on_fail: false
  log_alert_threshold: 50
  log_alert_enabled: true
llm_client:
  provider: "minimax"
  api_key_env: "OPENAI_API_KEY"
  base_url_env: "OPENAI_BASE_URL"
"""


@pytest.fixture
def cfg_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "vla.yaml"
    path.write_text(SAMPLE_YAML)
    return path


class TestLLMConfigExtraction:
    def test_old_yaml_keys_map_to_new_structure(self, cfg_yaml: Path):
        cfg = VLAConfig.from_yaml(cfg_yaml)
        assert cfg.llm.quality_model == "test-quality-model"
        assert cfg.llm.refine_model == "test-refine-model"
        assert cfg.llm.summary_model == "test-summary-model"

    def test_old_yaml_quality_check_model_alias_works(self, cfg_yaml: Path):
        cfg = VLAConfig.from_yaml(cfg_yaml)
        # Legacy accessor still works for back-compat in tests
        assert cfg.quality_check.model == "test-quality-model"

    def test_new_yaml_llm_block_works(self, tmp_path: Path):
        new_yaml = SAMPLE_YAML.replace(
            "  model: \"test-quality-model\"\n  refine_model: \"test-refine-model\"\n",
            "  model: \"test-quality-model\"  # legacy\n",
        ) + "\nllm:\n  quality_model: \"new-quality\"\n  refine_model: \"new-refine\"\n  summary_model: \"new-summary\"\n"
        path = tmp_path / "new.yaml"
        path.write_text(new_yaml)
        cfg = VLAConfig.from_yaml(path)
        # New `llm:` block wins
        assert cfg.llm.quality_model == "new-quality"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: AttributeError on `cfg.llm.quality_model` (cfg.llm does not exist yet)

---

### Task 2: Add `LLMConfig` and the field

**Files:**
- Modify: `src/vla/config.py`

**Interfaces:**
- Produces: `LLMConfig(BaseModel)` with `refine_model: str`, `quality_model: str`, `summary_model: str`

- [ ] **Step 1: Add `LLMConfig` class**

In `config.py`, after `LLMClientConfig`, add:

```python
class LLMConfig(BaseModel):
    """集中所有 LLM 模型选择(SSOT: spec §C #11,2026-09-03)。

    - `refine_model`:SubtitleRefiner 用(整理繁简 + 错字)
    - `quality_model`:QualityChecker 用(评分)
    - `summary_model`:LLMSummarizer 用(6h 批量总结)

    旧 YAML 字段(`quality_check.model` / `quality_check.refine_model` /
    `summary.model`)通过 VLAConfig 的 model_validator(mode="before") 自动
    迁移到 `llm.*`,保证向后兼容。
    """

    refine_model: str
    quality_model: str
    summary_model: str
```

- [ ] **Step 2: Add `llm: LLMConfig` to `VLAConfig`**

In `VLAConfig` class body, after `platforms`:

```python
llm: LLMConfig
```

- [ ] **Step 3: Add pre-validator that migrates old YAML keys**

In `VLAConfig`, add a `@model_validator(mode="before")` static method:

```python
@model_validator(mode="before")
@classmethod
def _migrate_legacy_llm_keys(cls, data: Any) -> Any:
    """迁移旧 YAML 字段到 llm.*。

    旧位置:
    - quality_check.model         → llm.quality_model
    - quality_check.refine_model  → llm.refine_model
    - summary.model               → llm.summary_model
    """
    if not isinstance(data, dict):
        return data
    llm = dict(data.get("llm") or {})
    qc = data.get("quality_check") or {}
    sm = data.get("summary") or {}
    if "quality_model" not in llm and "model" in qc:
        llm["quality_model"] = qc["model"]
    if "refine_model" not in llm and "refine_model" in qc:
        llm["refine_model"] = qc["refine_model"]
    if "summary_model" not in llm and "model" in sm:
        llm["summary_model"] = sm["model"]
    if llm:
        data["llm"] = llm
    return data
```

- [ ] **Step 4: Run new tests**

Run: `uv run pytest tests/test_config.py -v`
Expected: All pass

- [ ] **Step 5: Run full regression**

Run: `uv run pytest -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/vla/config.py tests/test_config.py
git commit -m "feat(config): LLMConfig sub-config with legacy YAML migration"
```

---

### Task 3: Migrate internal callers to `cfg.llm.*`

**Files:**
- Modify: `src/vla/cli.py` (where `cfg.quality_check.model` and `cfg.summary.model` are read)

**Interfaces:**
- `cfg.llm.quality_model` / `cfg.llm.refine_model` / `cfg.llm.summary_model`

- [ ] **Step 1: Find references**

Run: `grep -rn "cfg.quality_check.model\|cfg.summary.model\|cfg.quality_check.refine_model" src/vla/`

- [ ] **Step 2: Replace each**

Replace `cfg.quality_check.model` → `cfg.llm.quality_model`
Replace `cfg.quality_check.refine_model` → `cfg.llm.refine_model` (or keep fallback if needed)
Replace `cfg.summary.model` → `cfg.llm.summary_model`

- [ ] **Step 3: Run regression**

Run: `uv run pytest -v && uv run vla doctor`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/vla/cli.py
git commit -m "refactor(cli): use cfg.llm.* for model selection"
```

---

### Task 4: Optionally update `vla.yaml` to new schema

- [ ] **Step 1: Try removing old keys**

Edit `config/vla.yaml`: delete `quality_check.model`, `quality_check.refine_model`, `summary.model`, and add a top-level `llm:` block:

```yaml
llm:
  refine_model: MiniMax-M2.7-highspeed
  quality_model: MiniMax-M2.7-highspeed
  summary_model: MiniMax-M2.7-highspeed
```

- [ ] **Step 2: Verify doctor + tests still pass**

Run: `uv run vla doctor && uv run pytest -v`
Expected: All green

- [ ] **Step 3: Commit**

```bash
git add config/vla.yaml
git commit -m "chore(yaml): migrate to llm.* model config"
```

(If you'd rather keep the legacy keys in the file for clarity, skip this task — the validator handles both.)