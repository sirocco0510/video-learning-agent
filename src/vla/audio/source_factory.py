"""yt-dlp 音频提取(SSOT: spec 2026-09-03-fr2-fr3 §3.2, FR-2.14 path ①)。

策略 ①:对可下载 URL(B站 / YouTube / 其他 yt-dlp 支持站点),用
`yt-dlp --simulate` 先探测,再用 `yt-dlp -x --audio-format wav` 抽音频。

输出:<save_dir>/<stem>.wav(单声道 16kHz PCM — 由 ffmpeg_postargs 控制),
faster-whisper 直接读 .wav,无需二次转码。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)

DEFAULT_SAVE_DIR = Path("./logs/audio_raw")
DEFAULT_AUDIO_FORMAT = "wav"
DEFAULT_FFMPEG_POSTARGS = "-ac 1 -ar 16000"  # 单声道 + 16kHz(Whisper 期望采样率)
DEFAULT_SIMULATE_TIMEOUT_SEC = 30
DEFAULT_EXTRACT_TIMEOUT_SEC = 600  # 10 分钟:60min 长视频 + 弱网络


@dataclass(frozen=True)
class AudioExtractionResult:
    """音频抽取结果(SSOT: spec §3.2)。"""

    audio_path: Path
    source: str  # 当前实现固定 "yt-dlp"(path ② TabAudioRecorder 会用别的 source)
    duration_sec: int


class AudioSourceFactory:
    """yt-dlp 抽音频工厂(SSOT: spec §3.2)。

    用法:
        factory = AudioSourceFactory(save_dir=Path("./logs/audio_raw"))
        if factory.is_downloadable(url):
            result = factory.extract(url, stem=bvid)
            transcriber.transcribe(result.audio_path)
    """

    def __init__(
        self,
        save_dir: Path = DEFAULT_SAVE_DIR,
        audio_format: str = DEFAULT_AUDIO_FORMAT,
        ffmpeg_postargs: str = DEFAULT_FFMPEG_POSTARGS,
        simulate_timeout_sec: int = DEFAULT_SIMULATE_TIMEOUT_SEC,
    ) -> None:
        self.save_dir = Path(save_dir)
        self.audio_format = audio_format
        self.ffmpeg_postargs = ffmpeg_postargs
        self.simulate_timeout_sec = simulate_timeout_sec

    # ---- 探测:URL 是否可下载? ----

    def is_downloadable(self, url: str) -> bool:
        """FR-1.4: yt-dlp --simulate 先验证 URL 可下载。

        返回 True / False,不抛。失败 = 不可下载(主调度走 path ② TabAudioRecorder)。
        ~2-5s typical;timeout 默认 30s。
        """
        try:
            proc = subprocess.run(
                ["yt-dlp", "--simulate", "--no-warnings", url],
                check=False,                  # 不抛,自己看 returncode
                capture_output=True,
                timeout=self.simulate_timeout_sec,
            )
            return proc.returncode == 0
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp --simulate 超时(>%ds):%s", self.simulate_timeout_sec, url)
            return False
        except FileNotFoundError:
            logger.error("yt-dlp 未安装或不在 PATH;path ① 不可用")
            return False

    # ---- 抽取:实际下载音频 ----

    def extract(self, url: str, stem: str) -> AudioExtractionResult:
        """yt-dlp -x --audio-format wav <url> → <save_dir>/<stem>.wav。

        Returns:
            AudioExtractionResult with path + duration。
        Raises:
            subprocess.CalledProcessError: yt-dlp 失败(网络 / 404 / 区域限制)。
        """
        self.save_dir.mkdir(parents=True, exist_ok=True)
        out_template = str(self.save_dir / f"{stem}.%(ext)s")
        cmd = [
            "yt-dlp",
            "-x",                          # 仅抽音频
            "--audio-format", self.audio_format,
            "--audio-quality", "0",        # best
            "--postprocessor-args", self.ffmpeg_postargs,
            "-o", out_template,
            "--no-warnings",
            url,
        ]
        logger.info("yt-dlp 抽音频:%s → %s", url, out_template)
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=DEFAULT_EXTRACT_TIMEOUT_SEC,
        )

        audio_path = self.save_dir / f"{stem}.{self.audio_format}"
        if not audio_path.exists():
            raise FileNotFoundError(
                f"yt-dlp 声称成功但 {audio_path} 不存在(可能格式不是 {self.audio_format})"
            )

        duration_sec = self._probe_duration(audio_path)
        return AudioExtractionResult(
            audio_path=audio_path,
            source="yt-dlp",
            duration_sec=duration_sec,
        )

    def _probe_duration(self, audio_path: Path) -> int:
        """用 ffprobe 拿时长(秒)。失败 fallback 到 0(主流程不阻塞)。"""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            logger.warning("ffprobe 未安装,duration_sec 退化为 0")
            return 0
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "json",
                    str(audio_path),
                ],
                check=True,
                capture_output=True,
                timeout=10,
            )
            data = json.loads(proc.stdout)
            return int(float(data["format"]["duration"]))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                KeyError, ValueError, json.JSONDecodeError) as e:
            logger.warning("ffprobe 解析时长失败:%s", e)
            return 0
