"""浏览器插件字幕(策略 ②,SSOT: requirements.md FR-2.2 + implementation-plan.md Phase 3)。

扫描 plugin_paths 目录,优先精确匹配 {title}_{bvid}.{srt|vtt|json|ass},
否则模糊匹配 *{bvid}* + 已知后缀。

parse 支持四种格式:
  - .srt  pysrt
  - .vtt  webvtt-py
  - .json 递归收集所有 string
  - .ass  Dialogue: 行的最后一列,剥离 {\\...} override
"""

import json
import re
import time
from pathlib import Path

import pysrt
import webvtt

from ..config import VLAConfig

SUFFIXES = (".srt", ".vtt", ".json", ".ass")

_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


class BrowserPluginSubtitle:
    """策略 ②:浏览器插件导出的字幕文件。"""

    def __init__(self, config: VLAConfig) -> None:
        self.config = config
        self.plugin_paths = [
            Path(p).expanduser() for p in config.browser_plugin.plugin_paths
        ]

    def find_subtitle(self, bvid: str, title: str) -> Path | None:
        """按 plugin_paths 顺序扫描;精确 → 模糊。

        精确:`{title}_{bvid}.{srt|vtt|json|ass}`
        模糊:`{bvid}` 出现在文件名 + 已知后缀
        """
        for base in self.plugin_paths:
            if not base.exists() or not base.is_dir():
                continue
            # 精确
            for suffix in SUFFIXES:
                candidate = base / f"{title}_{bvid}{suffix}"
                if candidate.exists():
                    return candidate
            # 模糊
            for path in base.iterdir():
                if not path.is_file():
                    continue
                if bvid not in path.name:
                    continue
                if path.suffix.lower() in SUFFIXES:
                    return path
        return None

    def wait_for_subtitle(
        self, bvid: str, title: str, timeout: int = 600
    ) -> Path | None:
        """轮询 find_subtitle,直到超时或命中。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            path = self.find_subtitle(bvid, title)
            if path is not None:
                return path
            time.sleep(2)
        return None

    def parse(self, path: Path) -> str:
        """根据后缀分发解析;返回纯文本(按时间顺序拼接,行间 \\n)。"""
        suffix = path.suffix.lower()
        if suffix == ".srt":
            subs = pysrt.open(str(path))
            return "\n".join(s.text for s in subs)
        if suffix == ".vtt":
            captions = webvtt.read(str(path))
            return "\n".join(c.text for c in captions)
        if suffix == ".json":
            return self._parse_json(path)
        if suffix == ".ass":
            return self._parse_ass(path)
        raise ValueError(f"不支持的字幕格式: {suffix}")

    def _parse_json(self, path: Path) -> str:
        """递归收集 JSON 中的所有 string。"""
        data = json.loads(path.read_text(encoding="utf-8"))

        def collect(obj):
            if isinstance(obj, str):
                yield obj
            elif isinstance(obj, dict):
                for v in obj.values():
                    yield from collect(v)
            elif isinstance(obj, list):
                for item in obj:
                    yield from collect(item)

        return "\n".join(collect(data))

    def _parse_ass(self, path: Path) -> str:
        """解析 ASS Dialogue 行,取第 10 列(Text),剥离 override。"""
        lines = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.startswith("Dialogue:"):
                continue
            parts = raw.split(",", 9)
            if len(parts) < 10:
                continue
            text = _ASS_OVERRIDE_RE.sub("", parts[9]).strip()
            if text:
                lines.append(text)
        return "\n".join(lines)
