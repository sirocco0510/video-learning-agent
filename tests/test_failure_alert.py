"""FailureAlert 测试(SSOT: requirements.md FR-6.6 + implementation-plan.md Phase 7.6)。

累计失败条数(transcribe_fail + quality_fail)达到 threshold 整数倍时
弹一次阻塞式汇总,只在跨过倍数边界时才弹,避免每条都弹。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vla.log.failure_alert import FailureAlert
from vla.log.transcription_log import TranscriptionLog
from vla.models import QualityResult


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs"


@pytest.fixture
def tlog(log_dir: Path) -> TranscriptionLog:
    return TranscriptionLog(log_dir)


@pytest.fixture
def mock_notifier() -> MagicMock:
    n = MagicMock()
    n.alert_blocking = MagicMock()
    return n


# ---------------- 阈值倍数边界检测 ----------------


def test_alert_not_triggered_below_threshold(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """累计 < threshold → 不弹窗。"""
    alert = FailureAlert(threshold=10, log=tlog, notifier=mock_notifier)
    for i in range(9):
        tlog.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
        alert.check_after_write()
    assert mock_notifier.alert_blocking.call_count == 0


def test_alert_triggered_at_threshold_boundary(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """累计 == threshold (第一次跨过 1 倍数) → 弹窗 1 次。"""
    alert = FailureAlert(threshold=10, log=tlog, notifier=mock_notifier)
    for i in range(10):
        tlog.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
        alert.check_after_write()
    assert mock_notifier.alert_blocking.call_count == 1


def test_alert_not_triggered_within_same_multiple(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """累计 [10, 19] 都在 1 倍数内 → 只弹 1 次。"""
    alert = FailureAlert(threshold=10, log=tlog, notifier=mock_notifier)
    for i in range(15):
        tlog.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
        alert.check_after_write()
    assert mock_notifier.alert_blocking.call_count == 1


def test_alert_triggered_at_each_multiple_boundary(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """跨过 1×/2×/3× → 弹 3 次(每跨一次一次,间隔期不弹)。"""
    alert = FailureAlert(threshold=10, log=tlog, notifier=mock_notifier)
    # 30 累计 = 跨 3 个倍数
    for i in range(30):
        tlog.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
        alert.check_after_write()
    assert mock_notifier.alert_blocking.call_count == 3


def test_alert_counts_quality_failures_too(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """transcribe_fail + quality_fail 累计都计入(FR-6.6 spec)。"""
    alert = FailureAlert(threshold=10, log=tlog, notifier=mock_notifier)
    # 5 transcribe + 5 quality = 10 累计
    for i in range(5):
        tlog.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
        alert.check_after_write()
    qr = QualityResult(passed=False, score=30, issues=["low_cps"], suggestion="retry", char_count=10)
    for i in range(5):
        tlog.log_quality_fail(f"qid{i}", f"qtitle{i}", "url", qr, "transcript text")
        alert.check_after_write()
    assert mock_notifier.alert_blocking.call_count == 1


def test_alert_message_includes_breakdown(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """弹窗消息含失败条数 + 转写/质量 分项。"""
    alert = FailureAlert(threshold=5, log=tlog, notifier=mock_notifier)
    for i in range(3):
        tlog.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
        alert.check_after_write()
    qr = QualityResult(passed=False, score=30, issues=["low_cps"], suggestion="retry", char_count=10)
    for i in range(2):
        tlog.log_quality_fail(f"qid{i}", f"qtitle{i}", "url", qr, "text")
        alert.check_after_write()

    assert mock_notifier.alert_blocking.call_count == 1
    kwargs = mock_notifier.alert_blocking.call_args.kwargs
    msg = kwargs.get("message", "")
    assert "5" in msg  # total
    assert "转写失败" in msg
    assert "3" in msg  # transcribe count
    assert "质量失败" in msg
    assert "2" in msg  # quality count


def test_alert_last_alerted_multiple_initialized_to_zero(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """初始化时 last_alerted_multiple = 0,首次跨过 1× 时弹。"""
    alert = FailureAlert(threshold=10, log=tlog, notifier=mock_notifier)
    assert alert.last_alerted_multiple == 0


def test_alert_disabled_does_not_pop_up(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """enabled=False → check_after_write 跳过弹窗(FR-6.7 配置开关)。"""
    alert = FailureAlert(threshold=10, log=tlog, notifier=mock_notifier, enabled=False)
    for i in range(15):
        tlog.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
        alert.check_after_write()
    assert mock_notifier.alert_blocking.call_count == 0
    # 但 last_alerted_multiple 不更新
    assert alert.last_alerted_multiple == 0


def test_alert_reset_clears_state(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """reset() → last_alerted_multiple 清零,重新开始计数。

    注:reset 后第一次 check 会跨过 1× 边界再弹一次,所以 10 条后预期 3 次弹窗:
    - 前 10 条 → 跨 1× 弹 1 次
    - reset 清零
    - 再 10 条 → 跨 1× (total=11) 弹 1 次 + 跨 2× (total=20) 弹 1 次 = 2 次
    总 3 次。
    """
    alert = FailureAlert(threshold=10, log=tlog, notifier=mock_notifier)
    for i in range(10):
        tlog.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
        alert.check_after_write()
    assert mock_notifier.alert_blocking.call_count == 1
    alert.reset()
    assert alert.last_alerted_multiple == 0
    # 再 10 条(累计 20) → 跨 1× (total=11) 弹 + 跨 2× (total=20) 弹 = 2 次
    for i in range(10, 20):
        tlog.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
        alert.check_after_write()
    assert mock_notifier.alert_blocking.call_count == 3


def test_alert_with_no_failures_does_nothing(
    tlog: TranscriptionLog, mock_notifier: MagicMock
) -> None:
    """无失败 → 不弹。"""
    alert = FailureAlert(threshold=10, log=tlog, notifier=mock_notifier)
    alert.check_after_write()
    assert mock_notifier.alert_blocking.call_count == 0
    assert alert.last_alerted_multiple == 0


# ---------------- alert_blocking on macOSNotifier (FR-6.6 extension) ----------------


def test_macos_notifier_alert_blocking_calls_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    """alert_blocking → osascript display dialog with timeout。"""
    from vla.ui.macos_notify import MacOSNotifier

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        class R:
            returncode = 0
            stdout = "button returned:OK\n"
            stderr = ""
        return R()

    import vla.ui.macos_notify as mod
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    n = MacOSNotifier(enabled=True)
    n.alert_blocking("⚠️ 失败积累过多", "已积累 50 条失败", timeout_sec=30)

    assert calls
    cmd = calls[0]
    assert cmd[0] == "osascript"
    assert "display dialog" in cmd[2]
    assert "⚠️ 失败积累过多" in cmd[2]
    assert "giving up after 30" in cmd[2]


def test_macos_notifier_alert_blocking_runs_detail_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detail_button 被点击 → detail_action 被调用。"""
    from vla.ui.macos_notify import MacOSNotifier

    action_calls: list[None] = []

    def detail_action() -> None:
        action_calls.append(None)

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "button returned:查看 logs/\n"
            stderr = ""
        return R()

    import vla.ui.macos_notify as mod
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    n = MacOSNotifier(enabled=True)
    n.alert_blocking(
        title="⚠️", message="50 条失败",
        detail_button="查看 logs/", detail_action=detail_action,
        timeout_sec=30,
    )
    assert len(action_calls) == 1


def test_macos_notifier_alert_blocking_no_action_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gave up:true → detail_action 不调,静默返回。"""
    from vla.ui.macos_notify import MacOSNotifier

    action_calls: list[None] = []

    def detail_action() -> None:
        action_calls.append(None)

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "button returned:OK\ngave up:true\n"
            stderr = ""
        return R()

    import vla.ui.macos_notify as mod
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    n = MacOSNotifier(enabled=True)
    n.alert_blocking(
        title="⚠️", message="50 条",
        detail_button="查看 logs/", detail_action=detail_action,
        timeout_sec=30,
    )
    assert action_calls == []


def test_macos_notifier_alert_blocking_disabled_returns_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enabled=False → alert_blocking 直接返回,不调 osascript。"""
    from vla.ui.macos_notify import MacOSNotifier

    calls: list = []
    import vla.ui.macos_notify as mod
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: calls.append(1))
    n = MacOSNotifier(enabled=False)
    n.alert_blocking("⚠️", "msg", timeout_sec=30)
    assert calls == []