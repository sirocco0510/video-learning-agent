"""数据模型(SSOT: requirements.md 第六章 6.1)。

四个 pydantic BaseModel:
  - VideoTask       单条视频任务输入
  - SubtitleResult  字幕提取结果(三级策略任一)
  - QualityResult   质量门控结果
  - VideoSource     视频源(下载 OR 录屏)
"""

from pathlib import Path

from pydantic import BaseModel, HttpUrl


class VideoTask(BaseModel):
    """单条视频任务。

    id 用 B站 bvid(如 BV1xxx)即可;url 必须是合法 HTTP(S) URL。
    """

    id: str
    title: str
    url: HttpUrl
    expected_duration: int  # 秒


class SubtitleResult(BaseModel):
    """字幕提取结果。

    source 取值:FR-2 三级策略
      - "official":B站官方 CC 字幕(策略 ①)
      - "plugin":浏览器插件导出(策略 ②)
      - "whisper":本地 faster-whisper 转写(策略 ③)
    """

    text: str
    source: str  # "official" | "plugin" | "whisper"
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
