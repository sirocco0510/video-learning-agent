"""LLM 批量总结(SSOT: requirements.md FR-5 + implementation-plan.md Phase 7)。

设计(2026-09 收敛):
- 从 `logs/transcribed/*.txt` 读所有字幕(按 mtime 升序 = 处理顺序)
- 批量 LLM 总结 → 500-800 字 Markdown
- 返回纯 Markdown(不写文件),由主调度统一追加到 notes_file
- clear_after=True 时总结完后删除源文件(避免下次重复)

文件格式(由 TranscriptionLog.save_transcribed 写入):
    # 标题
    来源:source | 质量:N/100 | 时长:Ns

    <字幕正文>
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from vla.config import VLAConfig
from vla.llm.client import LLMClientLike


logger = logging.getLogger(__name__)


# ---------------- LLM 协议 ----------------
# LLMClientLike 已统一从 vla.llm.client 导入(SSOT)


# ---------------- 数据结构 ----------------


@dataclass
class TranscribedItem:
    """从 logs/transcribed/*.txt 读出的单条字幕。"""

    title: str
    source: str
    quality_score: int
    duration_sec: int
    text: str
    path: Path
    mtime: float


# ---------------- PROMPT ----------------


SUMMARIZE_BATCH_PROMPT = """你是视频内容总结助手。以下是累计约 6 小时视频的字幕(共 {video_count} 个视频),请生成一份 {min_words}-{max_words} 字的统一总结。

【视频清单】
{video_index}

【字幕内容】
{video_sections}

【要求】
1. 从所有视频标题提取**核心知识点**,跨视频**合并去重**
2. 每个视频作为总结中的一个**子要点**(## 二级标题 + 子列表)
3. 优先保留 **可操作的方法 / 概念 / 结论**
4. 跳过偶尔提到的次要内容
5. 使用 Markdown 格式(## / ### / - / 列表)
6. 字数控制在 {min_words}-{max_words} 之间
7. 输出里**只包含这一批 6 小时的总结**,不写"以上是本次总结"等元话语

【输出】
直接输出 Markdown 内容。"""


_HEADER_TITLE_RE = re.compile(r"^#\s+(.+)$")
_META_TOKEN_RE = re.compile(r"(来源|质量|时长):\s*([^|\s]+(?:[^\n|]*))?")


# ---------------- 主类 ----------------


class LLMSummarizer:
    """从磁盘 `logs/transcribed/*.txt` 读所有字幕 → 批量 LLM 总结 → Markdown。"""

    def __init__(self, llm: LLMClientLike, notes_file: Path) -> None:
        self.llm = llm
        self.notes_file = Path(notes_file)
        self.cfg: VLAConfig | None = None  # 由主调度注入,或测试 fixture 注入

    # ---------------- 读盘 ----------------

    def _load_items(self, transcribed_dir: Path) -> list[TranscribedItem]:
        """从 transcribed/*.txt 读所有字幕,按 mtime 升序。"""
        transcribed_dir = Path(transcribed_dir)
        if not transcribed_dir.exists():
            return []

        items: list[TranscribedItem] = []
        for path in sorted(transcribed_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime):
            try:
                item = self._parse_file(path)
            except Exception as e:
                logger.warning("跳过异常字幕文件 %s: %s", path, e)
                continue
            items.append(item)
        return items

    def _parse_file(self, path: Path) -> TranscribedItem:
        """解析单条字幕文件。"""
        content = path.read_text(encoding="utf-8")
        lines = content.split("\n", 1)
        # 标题
        title = path.stem
        if lines and lines[0].startswith("# "):
            title = lines[0][2:].strip()
        # 元数据
        meta_line = lines[1].split("\n", 1)[0] if len(lines) > 1 else ""
        source = "whisper"
        quality_score = 0
        duration_sec = 0
        for token in meta_line.split("|"):
            token = token.strip()
            if token.startswith("来源:"):
                source = token.removeprefix("来源:").strip()
            elif "质量:" in token:
                m = re.search(r"质量:(\d+)", token)
                if m:
                    quality_score = int(m.group(1))
            elif "时长:" in token:
                m = re.search(r"时长:(\d+)", token)
                if m:
                    duration_sec = int(m.group(1))
        # 正文(header 后第一个空行之后)
        if "\n\n" in content:
            text = content.split("\n\n", 1)[1].strip()
        else:
            text = content.strip()
        return TranscribedItem(
            title=title,
            source=source,
            quality_score=quality_score,
            duration_sec=duration_sec,
            text=text,
            path=path,
            mtime=path.stat().st_mtime,
        )

    # ---------------- 总结 ----------------

    def summarize_batch(
        self,
        transcribed_dir: Path,
        group_title: str | None = None,
        clear_after: bool = True,
    ) -> str:
        """从 transcribed_dir 读盘 → 批量 LLM 总结 → 返回 Markdown。

        Args:
            transcribed_dir: 字幕原文目录
            group_title: 可选,若提供则加 ## group_title — 累计 X 分钟(N 个视频) 头部
            clear_after: True → 总结完后删除源文件(默认)

        Returns:
            Markdown 字符串(空目录返回 "")
        """
        items = self._load_items(transcribed_dir)
        if not items:
            return ""

        # 1. 构造 video_index
        video_index = "\n".join(
            f"- [{i+1}] {item.title}({item.duration_sec}s, 来源:{item.source})"
            for i, item in enumerate(items)
        )

        # 2. 构造 video_sections(每个视频一段,前 3000 字截断)
        video_sections = "\n\n".join(
            f"### 视频 {i+1}:{item.title}\n"
            f"时长:{item.duration_sec}s | 来源:{item.source} | 质量:{item.quality_score}/100\n\n"
            f"{item.text[:3000]}"
            for i, item in enumerate(items)
        )

        # 3. 调 LLM
        cfg = self.cfg
        min_words = cfg.summary.target_words_min if cfg else 500
        max_words = cfg.summary.target_words_max if cfg else 800

        prompt = SUMMARIZE_BATCH_PROMPT.format(
            video_count=len(items),
            video_index=video_index,
            video_sections=video_sections,
            min_words=min_words,
            max_words=max_words,
        )
        summary_text = self.llm.complete(prompt, max_tokens=2000)

        # 4. 加头部
        if group_title:
            total_sec = sum(item.duration_sec for item in items)
            header = f"## {group_title} — 累计 {total_sec // 60} 分钟({len(items)} 个视频)\n\n"
            result = header + summary_text
        else:
            result = summary_text

        # 5. 清理源文件(用 mtime 精确匹配)
        if clear_after:
            self._clear_items(transcribed_dir, items)

        return result

    def _clear_items(self, transcribed_dir: Path, items: list[TranscribedItem]) -> None:
        """删除已总结的源文件。"""
        cleared = 0
        for item in items:
            try:
                if item.path.exists():
                    item.path.unlink()
                    cleared += 1
            except OSError as e:
                logger.warning("清理字幕文件失败 %s: %s", item.path, e)
        logger.info("🧹 总结完成,清理 %d 个字幕文件", cleared)

    # ---------------- 写盘 ----------------

    def write_to_notes(self, content: str) -> None:
        """追加内容到 notes_file(父目录不存在自动创建)。"""
        if not content:
            return
        self.notes_file.parent.mkdir(parents=True, exist_ok=True)
        with self.notes_file.open("a", encoding="utf-8") as f:
            if self.notes_file.stat().st_size > 0:
                # 已有内容,加分隔
                f.write("\n\n")
            f.write(content)
        logger.info("📝 写入笔记:%s", self.notes_file)