"""转写历史(SSOT: requirements.md FR-9.5/9.6 + FR-10.2/10.6 + Phase 7.5)。

职责:
- 维护 transcribed_history.jsonl(去重 SSOT)
- 启动时把 url_key 加载到内存 set(查询 O(1))
- record_success() 追加一行 + 同步更新内存 set
- 容错:解析失败的行跳过

URL key 格式(FR-10.2):
  bilibili://group/<group_id>/<bvid>
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator


logger = logging.getLogger(__name__)


# 内部 URL 表示(FR-10.2)— 模块级常量,供 utils.bvid 等复用,保持 SSOT
URL_KEY_PREFIX = "bilibili://group/"


class HistoryManager:
    """JSONL-based 转写历史 + 去重。"""

    # 内部 URL 表示(FR-10.2)— 与模块级 URL_KEY_PREFIX 同值,保留以兼容旧引用
    URL_KEY_PREFIX = URL_KEY_PREFIX

    def __init__(self, history_file: Path) -> None:
        self.file = Path(history_file)
        self._urls: set[str] = set()
        self._records: list[dict] = []
        self._load()

    # ---------------- 启动加载 ----------------

    def _load(self) -> None:
        """从 disk 加载到 _urls + _records。"""
        if not self.file.exists():
            return
        try:
            text = self.file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("读取 history 失败 %s: %s", self.file, e)
            return
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("history 第 %d 行 JSON 解析失败,跳过: %s", lineno, e)
                continue
            url = data.get("url")
            if not url:
                logger.warning("history 第 %d 行缺 url,跳过", lineno)
                continue
            self._urls.add(url)
            self._records.append(data)

    # ---------------- URL key ----------------

    @staticmethod
    def make_url_key(group_id: str, bvid: str) -> str:
        """构造内部 URL 表示(FR-10.2)。"""
        return f"{HistoryManager.URL_KEY_PREFIX}{group_id}/{bvid}"

    # ---------------- 去重查询 ----------------

    def is_already_done(self, url_key: str) -> bool:
        """该 url_key 是否已成功转写。"""
        return url_key in self._urls

    # ---------------- 写盘 ----------------

    def record_success(
        self,
        url_key: str,
        title: str,
        duration_sec: int,
        group_id: str,
        source: str,
    ) -> None:
        """追加一行 JSONL + 更新内存 set/records。"""
        record = {
            "url": url_key,
            "title": title,
            "duration_sec": duration_sec,
            "group_id": group_id,
            "source": source,
            "transcribed_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 确保父目录存在
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with self.file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._urls.add(url_key)
        self._records.append(record)

    # ---------------- 检查 / 导出 ----------------

    @property
    def count(self) -> int:
        """已记录的 URL 数量(从内存拿,O(1))。"""
        return len(self._records)

    def iter_records(self) -> Iterator[dict]:
        """遍历历史记录(按写入顺序)。"""
        return iter(self._records)