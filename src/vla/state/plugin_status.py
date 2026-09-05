"""插件状态机(SSOT: requirements.md FR-2.9/2.10 + Phase 7.5)。

职责:
- 整 session 跟踪 VideoTrans / B站官方 CC 插件可用性
- 三态:unknown(初始)/ available / unavailable
- unavailable 后,主调度不再弹窗,直接走策略 ③(Whisper 转写)
- 是 session 单例(主调度只 new 一次)

使用模式:
    plugin_status = PluginStatus()
    if not plugin_status.is_known():
        # 第一次检查,主调度会异步确认扩展
        ...
    if plugin_status.is_unavailable():
        # 整 session 都不再用插件,走 Whisper 兜底
```

"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


# 状态常量
STATUS_UNKNOWN = "unknown"
STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"


class PluginStatus:
    """整 session 跟踪插件可用性。"""

    def __init__(self) -> None:
        self._status: str = STATUS_UNKNOWN
        self._reason: str | None = None

    # ---------------- 查询 ----------------

    def get(self) -> str:
        """当前状态字符串(UNKNOWN / AVAILABLE / UNAVAILABLE)。"""
        return self._status

    def is_unavailable(self) -> bool:
        """是否已标记为不可用(主调度据此跳过插件策略)。"""
        return self._status == STATUS_UNAVAILABLE

    def is_known(self) -> bool:
        """是否已确认(unknown 表示还没确认过)。"""
        return self._status != STATUS_UNKNOWN

    @property
    def reason(self) -> str | None:
        """标记为 unavailable 时的原因(可用 / available 时为 None)。"""
        return self._reason

    # ---------------- 状态变更 ----------------

    def mark_available(self) -> None:
        """标记为可用(插件检测成功)。"""
        self._status = STATUS_AVAILABLE
        self._reason = None
        logger.info("✅ 插件状态: available")

    def mark_unavailable(self, reason: str) -> None:
        """标记为不可用,记原因(FR-2.10 — 后续不再弹窗)。"""
        self._status = STATUS_UNAVAILABLE
        self._reason = reason
        logger.info("❌ 插件状态: unavailable — %s", reason)

    def reset(self) -> None:
        """重置为 unknown(测试 / 显式重置用)。"""
        self._status = STATUS_UNKNOWN
        self._reason = None