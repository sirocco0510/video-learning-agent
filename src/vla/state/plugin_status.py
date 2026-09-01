"""插件状态机(SSOT: requirements.md FR-2.10)。

⚠️ 本文件是 **Phase 3 stub**,只暴露接口 + 简单状态存储,
让 Phase 3 SubtitleStrategy 可以正常 inject。

完整实现(模块级 session 单例 + 状态持久化)在 Phase 7.5 落地。
"""

from dataclasses import dataclass, field


@dataclass
class PluginStatus:
    """Phase 3 stub:接口 + 内部状态。Phase 7.5 升级为 session 级单例。"""

    _state: str = field(default="unknown")
    _reason: str = field(default="")

    def get(self) -> str:
        """返回当前状态:`unknown` / `available` / `unavailable`。"""
        return self._state

    def mark_available(self) -> None:
        """标记为 available(用户确认启动 + 等到文件 / 扫描命中)。"""
        self._state = "available"
        self._reason = ""

    def mark_unavailable(self, reason: str) -> None:
        """标记为 unavailable(FR-2.10 整 session 后续跳过弹窗)。"""
        self._state = "unavailable"
        self._reason = reason

    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason
