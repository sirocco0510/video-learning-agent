"""配置加载(SSOT: requirements.md 第八章 + implementation-plan.md Phase 1)。

VLAConfig 是顶层嵌套 pydantic BaseModel,11 个子配置类。
环境变量覆盖:LLMClientConfig.api_key_env / base_url_env 字段是 env 变量**名**,
实际取值通过 VLAConfig.resolve_api_key() / resolve_base_url() 实时读取。
"""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, model_validator


# ---------------- 子配置 ----------------


class StorageConfig(BaseModel):
    tmp_dir: Path
    auto_cleanup_on_pass: bool


class WhisperConfig(BaseModel):
    model: str
    language: str
    segment_seconds: int
    compute_type: str


class VideoSourceDownloadConfig(BaseModel):
    format: str


class VideoSourceRecordConfig(BaseModel):
    enabled: bool
    screen_index: int
    fps: int
    crf: int
    audio_input: str
    preset: str


class VideoSourceConfig(BaseModel):
    prefer_download: bool
    download: VideoSourceDownloadConfig
    record: VideoSourceRecordConfig


class QualityCheckConfig(BaseModel):
    enabled: bool
    model: str
    min_score_to_pass: int
    min_char_per_second: float
    max_char_per_second: float


class BrowserPluginConfig(BaseModel):
    name: str
    enabled: bool
    remind_timeout_sec: int
    plugin_paths: list[Path]


class SummaryConfig(BaseModel):
    model: str
    target_words_min: int
    target_words_max: int
    notes_file: Path
    cross_video_dedup: bool
    trigger_mode: str
    notes_section_header: str


class QuotaConfig(BaseModel):
    summary_threshold_sec: int
    on_exhausted: str


class HistoryConfig(BaseModel):
    file: Path


class LoggingConfig(BaseModel):
    log_dir: Path
    notify_on_fail: bool
    log_alert_threshold: int
    log_alert_enabled: bool


class LLMClientConfig(BaseModel):
    """provider 配置 + env 变量名(不是值)。

    api_key_env / base_url_env 是 env 变量**名**;
    真实取值通过 VLAConfig.resolve_api_key() / resolve_base_url() 读取。
    """

    provider: str
    api_key_env: str
    base_url_env: str


# ---------------- 顶层 ----------------


class VLAConfig(BaseModel):
    storage: StorageConfig
    whisper: WhisperConfig
    video_source: VideoSourceConfig
    quality_check: QualityCheckConfig
    browser_plugin: BrowserPluginConfig
    summary: SummaryConfig
    quota: QuotaConfig
    history: HistoryConfig
    logging: LoggingConfig
    llm_client: LLMClientConfig

    @classmethod
    def from_yaml(cls, path: str | Path) -> "VLAConfig":
        """加载 YAML 配置并构造 VLAConfig。

        用纯 pydantic BaseModel,不走 pydantic-settings(SSOT: implementation-plan.md Phase 1)。
        """
        path = Path(path)
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    @model_validator(mode="after")
    def _check_summary_word_range(self) -> "VLAConfig":
        """summary.target_words_min 必须严格小于 target_words_max。"""
        if self.summary.target_words_min >= self.summary.target_words_max:
            raise ValueError(
                f"summary.target_words_min ({self.summary.target_words_min}) "
                f"必须 < target_words_max ({self.summary.target_words_max})"
            )
        return self

    # ---- 环境变量解析(LLM) ----

    def resolve_api_key(self) -> str:
        """读取 llm_client.api_key_env 指向的 env 变量;未设置返回空串(不抛)。"""
        return os.environ.get(self.llm_client.api_key_env, "")

    def resolve_base_url(self) -> str:
        """读取 llm_client.base_url_env 指向的 env 变量;未设置返回空串(不抛)。"""
        return os.environ.get(self.llm_client.base_url_env, "")
