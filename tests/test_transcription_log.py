"""TranscriptionLog 测试。

Phase 2 stub:仅暴露接口 + no-op 实现。
Phase 6 按 requirements.md 6.1 log/transcription_log.py 完整实现。
"""

from pathlib import Path

from vla.log.transcription_log import TranscriptionLog


def test_construct_with_log_dir():
    """构造时不报错,接受 Path。"""
    log = TranscriptionLog(Path("./logs"))
    assert log is not None


def test_log_transcribe_fail_is_noop():
    """stub 阶段 log_transcribe_fail 不抛、不返回 None(返回 None 是 OK 的)。"""
    log = TranscriptionLog(Path("./logs"))
    log.log_transcribe_fail(
        video_id="BV1xxx",
        title="t",
        url="https://example.com",
        stage="download",
        error="timeout",
    )


def test_log_quality_fail_is_noop():
    """stub 阶段 log_quality_fail 不抛。"""
    log = TranscriptionLog(Path("./logs"))

    class FakeQuality:
        passed = False
        score = 0
        issues = ["x"]
        suggestion = "y"
        char_count = 0

    log.log_quality_fail(
        video_id="BV1xxx",
        title="t",
        url="https://example.com",
        result=FakeQuality(),
        text="...",
    )


def test_summary_returns_string():
    """summary() 返回 str(stub 阶段返回空串占位)。"""
    log = TranscriptionLog(Path("./logs"))
    result = log.summary()
    assert isinstance(result, str)
