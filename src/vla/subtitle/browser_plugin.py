"""字幕文件解析工具(SSOT: requirements.md FR-2.4 + implementation-plan.md Phase 3.7)。

Phase 3.7:仅保留 `parse()`,`find_subtitle` / `wait_for_subtitle` 已删除(由三级降级取代)。

parse 支持四种格式:
  - .srt  pysrt
  - .vtt  webvtt-py
  - .json 递归收集所有 string
  - .ass  Dialogue: 行的最后一列,剥离 {\\...} override

BrowserDriver._fetch_subtitle_text 用 parse() 解析 Puppeteer 取回的字幕文件。
"""

import json
import re
from pathlib import Path

import pysrt
import webvtt


SUFFIXES = (".srt", ".vtt", ".json", ".ass")

_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


class BrowserPluginSubtitle:
    """字幕文件解析(Phase 3.7+ 仅 parse;find_subtitle/wait_for_subtitle 已删除)。"""

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