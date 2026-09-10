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
    # 2026-09-02 Level 3 步骤 1:本地后处理(碎片合并 + 重复段去重)
    # 纯本地,不依赖 OpenCC / jieba / 云端 LLM,满足"字幕永远本地"红线
    postprocess_enabled: bool = True
    postprocess_min_line_chars: int = 8      # 短于这个字符的行认为碎片
    postprocess_max_line_chars: int = 80     # 合并后单行上限
    postprocess_min_overlap_chars: int = 6   # 重复段最小公共子串


class VideoSourceDownloadConfig(BaseModel):
    format: str


class VideoSourceConfig(BaseModel):
    prefer_download: bool
    download: VideoSourceDownloadConfig


class QualityCheckConfig(BaseModel):
    enabled: bool
    model: str
    min_score_to_pass: int
    min_char_per_second: float
    max_char_per_second: float
    # 2026-09-07 v3.2:总字数下界 — 文本过短直接 fail,不调 LLM
    # 避免短文本浪费 LLM token,也避免视频大量静音被误判为低语速
    min_chars: int = 50
    # 2026-09-02 Level 4:LLM 语义清理(在 quality_check 之前,云端 LLM,
    # 复用 quality_check.model 或独立 model)。
    # 设计目标:把 faster-whisper 输出的繁简混排 + 同音字错字 + 碎片,
    # 整理成可读性接近人工字幕的版本。归入"字幕质量检查"云端配额。
    refine_enabled: bool = False         # 默认关(云端 API 花钱,显式开启)
    refine_model: str | None = None       # None = 复用 quality_check.model
    refine_max_chars: int = 6000          # 超过这个字符数不调 LLM(避免爆 token)
    # 2026-09-02:输出 token 上限 — 必须大于输入 + corrections + notes 的预计总长。
    # reasoning model(MiniMax M2 / DeepSeek R1)还要算上 <think>...</think>。
    # 默认 max(2000, max_input_chars * 2 + 2000) — see SubtitleRefiner.
    refine_max_output_tokens: int = 4000


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


class AudioConfig(BaseModel):
    """F2-10 (2026-09-08):用户手动下载音频的根目录。

    约定:用户把 Tab Audio Recorder 录完的 webm 拖到 `<downloads_dir>/YYYY-MM-DD/`。
    代码侧:扫描今天的 YYYY-MM-DD/ 找未转写 webm,转写后同文件夹落 `<id>.txt`
    + sidecar `<id>.transcribed.txt` 标记。
    """
    downloads_dir: Path


class LLMClientConfig(BaseModel):
    """provider 配置 + env 变量名(不是值)。

    api_key_env / base_url_env 是 env 变量**名**;
    真实取值通过 VLAConfig.resolve_api_key() / resolve_base_url() 读取。
    """

    provider: str
    api_key_env: str
    base_url_env: str


class LLMConfig(BaseModel):
    """集中所有 LLM 模型选择(SSOT: spec §C #11,2026-09-03)。

    - `refine_model`:SubtitleRefiner 用(整理繁简 + 错字)
    - `quality_model`:QualityChecker 用(评分)
    - `summary_model`:LLMSummarizer 用(6h 批量总结)

    旧 YAML 字段通过 VLAConfig._migrate_legacy_llm_keys 自动迁移到 llm.*。
    """

    refine_model: str
    quality_model: str
    summary_model: str


class PuppeteerConfig(BaseModel):
    """Puppeteer CDP 连接配置(SSOT: requirements.md FR-2.10)。

    debugging_port: Chrome 启动时的 --remote-debugging-port。
    默认 9222(Chrome 默认)。
    """

    debugging_port: int = 9222
    cdp_host: str = "localhost"

    def cdp_url(self) -> str:
        return f"http://{self.cdp_host}:{self.debugging_port}"


class ChromeSessionConfig(BaseModel):
    """v3.2 (F2-6.1):Session 级 Chrome + page 槽复用(FR-2.28 关键路径截图)。

    enabled=False → 不启用截图,流程照常(原有 fallback 行为)
    enabled=True  → Session 启 1 次 Chrome,每条视频复用一个 page 槽(只 page.goto)
    """

    enabled: bool = False  # 默认关(避免现有调用方未注入 browser_driver 时报错)
    debug_port: int = 9222


# ---------------- 平台 adapter 配置(2026-09-02 新增) ----------------


class PlatformEntryConfig(BaseModel):
    """单个平台 adapter 启用配置(FR-2.0 + Phase 3.0)。

    match_hosts 仅作文档/校验用途(实际匹配逻辑在 adapter 自身的 match() 类方法里)。
    enabled=False → build_text_provider 跳过这个 adapter 注册。
    """

    enabled: bool = False
    match_hosts: list[str] = []


class PlatformsConfig(BaseModel):
    """所有平台 adapter 的启用状态。

    2026-09-02 修复:之前 VLAConfig 没有这个字段,`cfg.platforms.*` 直接 AttributeError,
    实际等于永远 0 个 adapter 被注册(Phase 9 集成 bug)。

    默认:B站开,内部网站关(等账号下发)。
    """

    bilibili: PlatformEntryConfig = PlatformEntryConfig(enabled=True)
    internal_site: PlatformEntryConfig = PlatformEntryConfig(enabled=False)


# ---------------- 顶层 ----------------


class VLAConfig(BaseModel):
    storage: StorageConfig
    whisper: WhisperConfig
    video_source: VideoSourceConfig
    quality_check: QualityCheckConfig
    summary: SummaryConfig
    quota: QuotaConfig
    history: HistoryConfig
    logging: LoggingConfig
    llm_client: LLMClientConfig
    llm: LLMConfig
    puppeteer: PuppeteerConfig = PuppeteerConfig()
    # 2026-09-02 修复:之前 VLAConfig 没有 platforms 字段,YAML 里写 platforms:.* 是被 pydantic 静默忽略的
    platforms: PlatformsConfig = PlatformsConfig()
    # F2-6.1 (v3.2):Chrome Session(截图关键路径依赖)
    chrome_session: ChromeSessionConfig = ChromeSessionConfig()
    # F2-10 (2026-09-08):用户手动下载音频根目录(扫今天 YYYY-MM-DD/ 路径)
    audio: AudioConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_llm_keys(cls, data: Any) -> Any:
        """迁移旧 YAML 字段到 llm.*,并保持 legacy accessor 可用。

        旧字段 → 新字段:
        - quality_check.model         → llm.quality_model
        - quality_check.refine_model  → llm.refine_model
        - summary.model               → llm.summary_model

        新字段 → 旧字段(back-compat):
        - llm.quality_model    → quality_check.model  (QualityCheckConfig.model 是必填字段)
        - llm.refine_model     → quality_check.refine_model
        - llm.summary_model    → summary.model

        规则:新 llm: 块优先(SSOT)。当新旧并存时,新字段胜出,旧字段被新字段覆盖。
        """
        if not isinstance(data, dict):
            return data
        llm = dict(data.get("llm") or {})
        qc = dict(data.get("quality_check") or {})
        sm = dict(data.get("summary") or {})

        # 旧 → 新(legacy YAML 兼容)
        if "quality_model" not in llm and "model" in qc:
            llm["quality_model"] = qc["model"]
        # refine_model:旧 YAML 里 refine_model 可选(None = 复用 quality_check.model),
        # 新 llm.refine_model 是必填 str,所以缺省时默认 = quality_model。
        if "refine_model" not in llm:
            legacy_refine = qc.get("refine_model")
            llm["refine_model"] = (
                legacy_refine if legacy_refine else llm.get("quality_model") or qc.get("model", "")
            )
        if "summary_model" not in llm and "model" in sm:
            llm["summary_model"] = sm["model"]

        # 新 → 旧(legacy accessor 兼容 — quality_check.model / summary.model 都是必填字段)
        if "quality_model" in llm:
            qc["model"] = llm["quality_model"]
        if "refine_model" in llm:
            qc["refine_model"] = llm["refine_model"]
        if "summary_model" in llm:
            sm["model"] = llm["summary_model"]

        if llm:
            data["llm"] = llm
        if qc:
            data["quality_check"] = qc
        if sm:
            data["summary"] = sm
        return data

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
