"""BVID + URL key utilities(SSOT: spec §B #8,2026-09-03)。

集中所有 B站 bvid 提取 / URL key 构造,消除 cli.py / bilibili_official.py /
state/history.py 三处分散实现。
"""

from __future__ import annotations

import re

from vla.state.history import URL_KEY_PREFIX


_BVID_PATTERN = re.compile(r"(BV[a-zA-Z0-9]+)", re.IGNORECASE)


def extract_bvid(url: str) -> str | None:
    """从 B站 URL 提取 bvid;不命中返回 None(不抛错)。

    适用于 `https://www.bilibili.com/video/BVxxx` / `BVxxx?p=1`。
    对 b23.tv 短链因不含 bvid 模式,返回 None(调用方需自行 resolve)。
    """
    match = _BVID_PATTERN.search(url)
    return match.group(1) if match else None


def make_url_key(group_id: str, bvid: str, p: int | None = None) -> str:
    """构造 HistoryManager 用的 URL key。

    格式:`{URL_KEY_PREFIX}{group_id}/{bvid}` + 可选 `?p=<p>`
    URL_KEY_PREFIX 在 state/history.py 定义,保持 SSOT。
    """
    key = f"{URL_KEY_PREFIX}{group_id}/{bvid}"
    if p is not None:
        key += f"?p={p}"
    return key
