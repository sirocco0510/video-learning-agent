"""数据模型(SSOT: requirements.md 第六章 6.1)。

五个 pydantic BaseModel:
  - VideoTask        单条视频任务输入
  - SubtitleResult   字幕提取结果(三级策略任一)
  - QualityResult    质量门控结果
  - VideoSource      视频源(下载 OR 录屏)
  - RefinementResult LLM 语义清理结果(2026-09-02 Level 4)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl


class VideoTask(BaseModel):
    """单条视频任务。

    id 用 B站 bvid(如 BV1xxx)即可;url 必须是合法 HTTP(S) URL。
    group_id 用于 FR-10 视频组概念(同主题多个视频一组);
    group_title 用于总结时的二级标题(可选)。
    """

    id: str
    title: str
    url: HttpUrl
    expected_duration: int  # 秒
    group_id: str = "default"
    group_title: str | None = None


class SubtitleResult(BaseModel):
    """字幕提取结果。

    source 取值:Phase 3.5 平台无关三级策略
      - "api":平台官方 API(策略 ①)
      - "browser":Puppeteer JS 探测(策略 ②)
      - "whisper":本地 faster-whisper 转写(策略 ③)
    """

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


class QualityResult(BaseModel):
    """质量门控结果。

    score: 0-100,passed 由调用方根据 min_score_to_pass 阈值决定。
    """

    passed: bool
    score: int  # 0-100
    issues: list[str]
    suggestion: str
    char_count: int


class VideoSource(BaseModel):
    """视频源(FR-1)。

    mode 取值:
      - "download":yt-dlp 下载成功
      - "record":ffmpeg 录屏兜底
    """

    path: Path
    mode: str  # "download" | "record"
    duration_sec: float


class Correction(BaseModel):
    """LLM 在语义清理时做的单条修正(2026-09-02 Level 4)。

    用于:
    - 调试:看 LLM 修了什么
    - 词典生成:把高频修正累计下来,下次 prompt 直接带示例
    """

    original: str  # 原文(可能含错字/简繁混排/碎片)
    fixed: str    # 修正后
    reason: str   # 为什么这么修(如"根据视频标题,应为 Deep Seek")


class RefinementResult(BaseModel):
    """LLM 语义清理结果(2026-09-02 Level 4,FR-2.15c)。

    流程位置:
      StreamingTranscriber.transcribe() (本地 postprocess)
      → SubtitleRefiner.refine() (云端 LLM)
      → QualityChecker.check() (云端 LLM 评分)
      → 保存 *.cleaned.txt

    与 QualityResult 的区别:
    - QualityResult 是"评分"(pass / fail)
    - RefinementResult 是"改写"(清理后的文本 + 修改记录)
    """

    original_text: str          # 原始(本地 postprocess 后)文本
    cleaned_text: str           # LLM 清理后的文本
    corrections: list[Correction] = Field(default_factory=list)
    notes: str = ""             # LLM 自述本次清理做了什么
    model: str                  # 用的模型(便于审计)
    prompt_tokens: int = 0      # 调 LLM 的 token 统计(供 quota)
    completion_tokens: int = 0


class SummaryResult(BaseModel):
    """单视频摘要结果(FR-2.15d,2026-09-10)。

    触发条件:cleaned_text 长度 > config.quality_check.refine_max_chars。
    低于阈值 → summary_text="",调用方根据此字段判断要不要落盘。

    与 RefinementResult 的区别:
    - RefinementResult 是"清理 preserve original length"
    - SummaryResult 是"压缩 200-300 字精华"
    """

    summary_text: str           # 200-300 字摘要;空字符串 = 没生成
    notes: str = ""             # 处理说明(失败原因 / 跳过原因)
    model: str                  # 用的模型(便于审计)
    prompt_tokens: int = 0
    completion_tokens: int = 0
