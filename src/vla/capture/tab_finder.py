"""跨 Chrome context 找 B站 tab(SSOT: requirements.md FR-2.28.2f)。

Chrome 多窗口场景下,`browser.contexts[0].pages` 只看第一个 context 的 pages,
会漏掉用户在其他窗口打开的 B站 tab。本模块提供遍历式查找。
"""
from __future__ import annotations

from typing import Any, Optional, Protocol


class _PageLike(Protocol):
    """duck-typed Playwright Page(只要 .url)。"""
    url: str


class _ContextLike(Protocol):
    """duck-typed Playwright BrowserContext(只要 .pages)。"""
    pages: list[Any]


class _BrowserLike(Protocol):
    """duck-typed Playwright Browser(只要 .contexts)。"""
    contexts: list[Any]


def find_bilibili_page(browser: _BrowserLike, bvid: str) -> Optional[_PageLike]:
    """跨所有 context 找 B站 tab。

    遍历顺序:先 context 后 page,稳定返回第一个命中。

    Args:
        browser: Playwright Browser(CDP 连接后即可用)
        bvid: B站 BV 号(精确 substring 匹配,目标 BV 号够长,
              "BV1abc" 命中 "BV1abcd" 视为设计选择 — caller 用完整 BV 号即可)

    Returns:
        第一个 url 含 bvid 的 page;找不到返回 None。
        不会主动新建 tab(用户故意关掉的视频不要复活)。

    Raises:
        不抛(任一层 .pages 访问异常 → 跳过该 context)
    """
    for ctx in browser.contexts:
        try:
            pages = ctx.pages
        except Exception:
            continue
        for page in pages:
            try:
                if bvid in page.url:
                    return page
            except Exception:
                continue
    return None