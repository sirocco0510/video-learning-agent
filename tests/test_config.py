"""config.py 测试(SSOT: requirements.md 第八章 + implementation-plan.md Phase 1)。

VLAConfig 嵌套所有子配置 + from_yaml + 环境变量覆盖 + model_validator。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from vla.config import VLAConfig

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "vla.yaml"


# R-10:覆盖 11 个子配置 + llm 的最小合法 YAML(测试用 fixture)。
# SAMPLE_YAML 不写 llm 块,用来验证旧 YAML 字段(qc.model / summary.model)能迁移到 cfg.llm.*。
SAMPLE_YAML = """
storage: {tmp_dir: "./tmp", auto_cleanup_on_pass: true}
whisper: {model: "small", language: "zh", segment_seconds: 30, compute_type: "int8"}
video_source:
  prefer_download: true
  download: {format: "best"}
quality_check: {enabled: true, model: "old-qc-model", min_score_to_pass: 70, min_char_per_second: 1.0, max_char_per_second: 15.0, refine_model: "old-refine-model"}
browser_plugin: {name: "Screen Recorder", enabled: true, remind_timeout_sec: 30, plugin_paths: []}
summary: {model: "old-summary-model", target_words_min: 500, target_words_max: 800, notes_file: "./notes/v.md", cross_video_dedup: true, trigger_mode: "quota", notes_section_header: "## x"}
quota: {summary_threshold_sec: 21600, on_exhausted: "stop_session"}
history: {file: "./logs/h.jsonl"}
logging: {log_dir: "./logs", notify_on_fail: false, log_alert_threshold: 50, log_alert_enabled: true}
llm_client: {provider: "minimax", api_key_env: "OPENAI_API_KEY", base_url_env: "OPENAI_BASE_URL"}
"""


# ---------------- from_yaml ----------------


class TestFromYaml:
    def test_loads_real_config(self):
        """能加载项目自带的 config/vla.yaml,关键字段值正确。"""
        cfg = VLAConfig.from_yaml(CONFIG_PATH)

        # 顶层子配置存在
        assert cfg.storage.tmp_dir == Path("./tmp")
        assert cfg.whisper.model == "small"
        assert cfg.whisper.language == "zh"

        # 嵌套:video_source.download.format
        # 2026-09-02:从 "worst" 升级为 yt-dlp format selector(B站 audio-only 兼容)
        fmt = cfg.video_source.download.format
        assert "bestvideo" in fmt
        assert "bestaudio" in fmt
        # F2-8:video_source.record 块已从 VideoSourceConfig 移除(FR-2.14 v3:录屏
        # 兜底走策略 ③ adapter.fetch_via_recording,不再需要录屏子配置)。
        assert not hasattr(cfg.video_source, "record")

        # 嵌套:summary 数值
        assert cfg.summary.target_words_min == 500
        assert cfg.summary.target_words_max == 800

        # llm_client 配置字段名(不是值)
        assert cfg.llm_client.provider == "minimax"
        assert cfg.llm_client.api_key_env == "OPENAI_API_KEY"
        assert cfg.llm_client.base_url_env == "OPENAI_BASE_URL"

    def test_from_yaml_returns_vlaconfig(self):
        """返回类型必须是 VLAConfig。"""
        cfg = VLAConfig.from_yaml(CONFIG_PATH)
        assert isinstance(cfg, VLAConfig)


# ---------------- model_validator ----------------


class TestModelValidator:
    def test_target_words_min_less_than_max_ok(self, tmp_path):
        """合法配置:min < max,加载成功。"""
        cfg_text = """
storage: {tmp_dir: "./tmp", auto_cleanup_on_pass: true}
whisper: {model: "small", language: "zh", segment_seconds: 30, compute_type: "int8"}
video_source:
  prefer_download: true
  download: {format: "worst"}
  record: {enabled: true, screen_index: 1, fps: 30, crf: 28, audio_input: "1:0", preset: "ultrafast"}
quality_check: {enabled: true, model: "x", min_score_to_pass: 70, min_char_per_second: 1.0, max_char_per_second: 15.0}
browser_plugin: {name: "VideoTrans", enabled: true, remind_timeout_sec: 30, plugin_paths: []}
summary: {model: "x", target_words_min: 500, target_words_max: 800, notes_file: "./notes/v.md", cross_video_dedup: true, trigger_mode: "quota", notes_section_header: "## x"}
quota: {summary_threshold_sec: 21600, on_exhausted: "stop_session"}
history: {file: "./logs/h.jsonl"}
logging: {log_dir: "./logs", notify_on_fail: false, log_alert_threshold: 50, log_alert_enabled: true}
llm_client: {provider: "openai", api_key_env: "OPENAI_API_KEY", base_url_env: "OPENAI_BASE_URL"}
"""
        cfg_path = tmp_path / "vla.yaml"
        cfg_path.write_text(cfg_text)
        cfg = VLAConfig.from_yaml(cfg_path)
        assert cfg.summary.target_words_min < cfg.summary.target_words_max

    def test_target_words_min_equals_max_raises(self, tmp_path):
        """非法配置:min == max 必须抛 ValidationError。"""
        cfg_text = """
storage: {tmp_dir: "./tmp", auto_cleanup_on_pass: true}
whisper: {model: "small", language: "zh", segment_seconds: 30, compute_type: "int8"}
video_source:
  prefer_download: true
  download: {format: "worst"}
  record: {enabled: true, screen_index: 1, fps: 30, crf: 28, audio_input: "1:0", preset: "ultrafast"}
quality_check: {enabled: true, model: "x", min_score_to_pass: 70, min_char_per_second: 1.0, max_char_per_second: 15.0}
browser_plugin: {name: "VideoTrans", enabled: true, remind_timeout_sec: 30, plugin_paths: []}
summary: {model: "x", target_words_min: 800, target_words_max: 800, notes_file: "./notes/v.md", cross_video_dedup: true, trigger_mode: "quota", notes_section_header: "## x"}
quota: {summary_threshold_sec: 21600, on_exhausted: "stop_session"}
history: {file: "./logs/h.jsonl"}
logging: {log_dir: "./logs", notify_on_fail: false, log_alert_threshold: 50, log_alert_enabled: true}
llm_client: {provider: "openai", api_key_env: "OPENAI_API_KEY", base_url_env: "OPENAI_BASE_URL"}
"""
        cfg_path = tmp_path / "vla.yaml"
        cfg_path.write_text(cfg_text)
        with pytest.raises(ValidationError):
            VLAConfig.from_yaml(cfg_path)

    def test_target_words_min_greater_than_max_raises(self, tmp_path):
        """非法配置:min > max 必须抛 ValidationError。"""
        cfg_text = """
storage: {tmp_dir: "./tmp", auto_cleanup_on_pass: true}
whisper: {model: "small", language: "zh", segment_seconds: 30, compute_type: "int8"}
video_source:
  prefer_download: true
  download: {format: "worst"}
  record: {enabled: true, screen_index: 1, fps: 30, crf: 28, audio_input: "1:0", preset: "ultrafast"}
quality_check: {enabled: true, model: "x", min_score_to_pass: 70, min_char_per_second: 1.0, max_char_per_second: 15.0}
browser_plugin: {name: "VideoTrans", enabled: true, remind_timeout_sec: 30, plugin_paths: []}
summary: {model: "x", target_words_min: 900, target_words_max: 800, notes_file: "./notes/v.md", cross_video_dedup: true, trigger_mode: "quota", notes_section_header: "## x"}
quota: {summary_threshold_sec: 21600, on_exhausted: "stop_session"}
history: {file: "./logs/h.jsonl"}
logging: {log_dir: "./logs", notify_on_fail: false, log_alert_threshold: 50, log_alert_enabled: true}
llm_client: {provider: "openai", api_key_env: "OPENAI_API_KEY", base_url_env: "OPENAI_BASE_URL"}
"""
        cfg_path = tmp_path / "vla.yaml"
        cfg_path.write_text(cfg_text)
        with pytest.raises(ValidationError):
            VLAConfig.from_yaml(cfg_path)


# ---------------- 环境变量覆盖 ----------------


class TestEnvOverride:
    def test_resolve_api_key_from_env(self, tmp_path, monkeypatch):
        """llm_client.api_key_env 指向的 env 变量能被读取到。"""
        cfg_text = """
storage: {tmp_dir: "./tmp", auto_cleanup_on_pass: true}
whisper: {model: "small", language: "zh", segment_seconds: 30, compute_type: "int8"}
video_source:
  prefer_download: true
  download: {format: "worst"}
  record: {enabled: true, screen_index: 1, fps: 30, crf: 28, audio_input: "1:0", preset: "ultrafast"}
quality_check: {enabled: true, model: "x", min_score_to_pass: 70, min_char_per_second: 1.0, max_char_per_second: 15.0}
browser_plugin: {name: "VideoTrans", enabled: true, remind_timeout_sec: 30, plugin_paths: []}
summary: {model: "x", target_words_min: 500, target_words_max: 800, notes_file: "./notes/v.md", cross_video_dedup: true, trigger_mode: "quota", notes_section_header: "## x"}
quota: {summary_threshold_sec: 21600, on_exhausted: "stop_session"}
history: {file: "./logs/h.jsonl"}
logging: {log_dir: "./logs", notify_on_fail: false, log_alert_threshold: 50, log_alert_enabled: true}
llm_client: {provider: "openai", api_key_env: "TEST_VLA_API_KEY", base_url_env: "TEST_VLA_BASE_URL"}
"""
        cfg_path = tmp_path / "vla.yaml"
        cfg_path.write_text(cfg_text)
        monkeypatch.setenv("TEST_VLA_API_KEY", "sk-test-123")
        monkeypatch.setenv("TEST_VLA_BASE_URL", "https://example.com/v1")

        cfg = VLAConfig.from_yaml(cfg_path)

        assert cfg.resolve_api_key() == "sk-test-123"
        assert cfg.resolve_base_url() == "https://example.com/v1"

    def test_resolve_api_key_missing_env_returns_empty(self, tmp_path, monkeypatch):
        """env 变量未设置时,resolve 返回空串(不抛)。"""
        cfg_text = """
storage: {tmp_dir: "./tmp", auto_cleanup_on_pass: true}
whisper: {model: "small", language: "zh", segment_seconds: 30, compute_type: "int8"}
video_source:
  prefer_download: true
  download: {format: "worst"}
  record: {enabled: true, screen_index: 1, fps: 30, crf: 28, audio_input: "1:0", preset: "ultrafast"}
quality_check: {enabled: true, model: "x", min_score_to_pass: 70, min_char_per_second: 1.0, max_char_per_second: 15.0}
browser_plugin: {name: "VideoTrans", enabled: true, remind_timeout_sec: 30, plugin_paths: []}
summary: {model: "x", target_words_min: 500, target_words_max: 800, notes_file: "./notes/v.md", cross_video_dedup: true, trigger_mode: "quota", notes_section_header: "## x"}
quota: {summary_threshold_sec: 21600, on_exhausted: "stop_session"}
history: {file: "./logs/h.jsonl"}
logging: {log_dir: "./logs", notify_on_fail: false, log_alert_threshold: 50, log_alert_enabled: true}
llm_client: {provider: "openai", api_key_env: "TEST_VLA_MISSING_KEY", base_url_env: "TEST_VLA_MISSING_URL"}
"""
        cfg_path = tmp_path / "vla.yaml"
        cfg_path.write_text(cfg_text)
        monkeypatch.delenv("TEST_VLA_MISSING_KEY", raising=False)
        monkeypatch.delenv("TEST_VLA_MISSING_URL", raising=False)

        cfg = VLAConfig.from_yaml(cfg_path)

        assert cfg.resolve_api_key() == ""
        assert cfg.resolve_base_url() == ""


# ---------------- R-10: LLMConfig 抽取 ----------------


class TestLLMConfigExtraction:
    """R-10:把 quality_check.model / refine_model / summary.model 合并到 cfg.llm.*。

    旧 YAML(quality_check.model 等)自动迁移;legacy accessor cfg.quality_check.model 仍可用;
    新 llm: 块优先。
    """

    def test_old_yaml_keys_map_to_new_structure(self, tmp_path):
        """旧 YAML(无 llm 块)→ cfg.llm.quality_model/refine_model/summary_model 自动填充。"""
        cfg_path = tmp_path / "vla.yaml"
        cfg_path.write_text(SAMPLE_YAML)
        cfg = VLAConfig.from_yaml(cfg_path)

        assert cfg.llm.quality_model == "old-qc-model"
        assert cfg.llm.refine_model == "old-refine-model"
        assert cfg.llm.summary_model == "old-summary-model"

    def test_old_yaml_quality_check_model_alias_works(self, tmp_path):
        """legacy accessor `cfg.quality_check.model` 仍可用(back-compat)。"""
        cfg_path = tmp_path / "vla.yaml"
        cfg_path.write_text(SAMPLE_YAML)
        cfg = VLAConfig.from_yaml(cfg_path)

        assert cfg.quality_check.model == "old-qc-model"
        assert cfg.quality_check.refine_model == "old-refine-model"
        assert cfg.summary.model == "old-summary-model"

    def test_new_yaml_llm_block_wins(self, tmp_path):
        """新旧字段都在 → 新 llm: 块胜出(legacy 字段保留 YAML 里的旧值,不与新块冲突时用旧值)。"""
        cfg_text = SAMPLE_YAML + """
llm:
  quality_model: "new-quality-model"
  refine_model: "new-refine-model"
  summary_model: "new-summary-model"
"""
        cfg_path = tmp_path / "vla.yaml"
        cfg_path.write_text(cfg_text)
        cfg = VLAConfig.from_yaml(cfg_path)

        # 新 llm: 块胜出
        assert cfg.llm.quality_model == "new-quality-model"
        assert cfg.llm.refine_model == "new-refine-model"
        assert cfg.llm.summary_model == "new-summary-model"
