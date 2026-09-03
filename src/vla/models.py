"""数据模型(SSOT: requirements.md 第六章 6.1)。

五个 pydantic BaseModel:
  - VideoTask        单条视频任务输入
  - SubtitleResult   字幕提取结果(三级策略任一)
  - QualityResult    质量门控结果
  - VideoSource      视频源(下载 OR 录屏)
  - RefinementResult LLM 语义清理结果(2026-09-02 Level 4)
"""

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

    text: str
    source: str  # "api" | "browser" | "whisper"
    metadata: dict


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
