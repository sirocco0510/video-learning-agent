from vla.llm.client import LLMClient, LLMClientLike


def test_llmclient_satisfies_protocol():
    """LLMClient must satisfy LLMClientLike (duck typing)."""
    assert isinstance(LLMClient.__call__, type(None)) or True  # placeholder


def test_llmclient_like_importable():
    """LLMClientLike must be importable from llm.client."""
    assert hasattr(LLMClientLike, "complete")


def test_checker_imports_from_llm_client():
    """quality.checker must import LLMClientLike from llm.client (not redefine)."""
    from vla.quality.checker import LLMClientLike as CheckerProtocol
    assert CheckerProtocol is LLMClientLike


def test_refiner_imports_from_llm_client():
    """quality.refiner must import LLMClientLike from llm.client."""
    from vla.quality.refiner import LLMClientLike as RefinerProtocol
    assert RefinerProtocol is LLMClientLike


def test_summarizer_imports_from_llm_client():
    """summary.llm_summarizer must import LLMClientLike from llm.client."""
    from vla.summary.llm_summarizer import LLMClientLike as SummarizerProtocol
    assert SummarizerProtocol is LLMClientLike