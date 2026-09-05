"""页面控制(SSOT: requirements.md FR-2.17 + Phase 3.5)。

职责:
- 提供页面级别的浏览器控制原语(独立于具体 adapter / recorder)
- 主要用于字幕策略 ② 触发前的视频暂停(避免插件读取时 video 自动播放)

设计:
- 函数接受 page 对象(duck typing,只需有 .evaluate 方法)
- best-effort:无 video 元素 / 暂停失败 都 log warning,不抛
- 返回字符串状态("paused" / "already_paused" / "no_video_element"),
  让调用方决定下一步行为
"""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


# 页面 video 元素的 JS 探测 + 暂停脚本
_PAUSE_VIDEO_JS = """
    () => {
        const v = document.querySelector('video');
        if (!v) return 'no_video_element';
        if (v.paused) return 'already_paused';
        v.pause();
        return 'paused';
    }
"""


def pause_page_video(page: Any) -> str:
    """尝试暂停页面上的 <video> 元素(FR-2.17:等待用户介入前先暂停)。

    Args:
        page: 浏览器 page 对象(playwright Page / 类似接口,需要 .evaluate(js) 方法)

    Returns:
        "paused"           — 成功暂停
        "already_paused"   — 视频本来就是暂停的
        "no_video_element" — 页面没有 <video> 元素

    Note:
        best-effort:evaluate 抛错(超时 / 权限 / 跨域) → log warning 不影响主流程。
        不同站点的 player 实现不同(B站 / YouTube / 普通 <video>),
        这里只覆盖原生 HTML5 video,复杂的 iframe 播放器留给用户在 Chrome 手动暂停。
    """
    try:
        result = page.evaluate(_PAUSE_VIDEO_JS)
        logger.info("页面视频状态:%s", result)
        return result or "no_video_element"
    except Exception as e:
        logger.warning("暂停页面视频失败(best-effort,不影响主流程):%s", e)
        return "no_video_element"