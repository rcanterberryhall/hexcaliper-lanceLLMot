"""
test_extractor_priority.py — Verify extractor calls land in merLLM's
``background`` bucket and user-facing calls land in the ``chat`` bucket.

merLLM runs a 5-bucket priority queue (chat > reserved > short >
feedback > background, strict top-down drain). LanceLLMot's concept
extractor is not latency-sensitive and must go out with
``X-Priority: background`` so merLLM waits indefinitely for a GPU slot
instead of timing out at INTERACTIVE_QUEUE_TIMEOUT and silently dropping
graph edges. Every other LanceLLMot → merLLM call is user-facing and
must land in the ``chat`` bucket.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import extractor


def test_extractor_headers_are_background_priority():
    """config.OLLAMA_EXTRACTOR_HEADERS must target the background bucket."""
    assert config.OLLAMA_EXTRACTOR_HEADERS["X-Priority"] == "background"
    assert config.OLLAMA_EXTRACTOR_HEADERS["X-Source"] == "lancellmot"


def test_global_ollama_headers_are_chat_priority():
    """Global OLLAMA_HEADERS must target the chat bucket — RAG/query is user-facing."""
    assert config.OLLAMA_HEADERS["X-Priority"] == "chat"
    assert config.OLLAMA_HEADERS["X-Source"] == "lancellmot"


@pytest.mark.asyncio
async def test_extract_chunk_sends_background_priority_header():
    """extract_chunk must build its httpx client with the extractor headers."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "{}"}}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client) as mock_cls:
        await extractor.extract_chunk("some chunk text")

    # httpx.AsyncClient(...) must have been called with the extractor headers.
    _, kwargs = mock_cls.call_args
    assert kwargs["headers"] is config.OLLAMA_EXTRACTOR_HEADERS
    assert kwargs["headers"]["X-Priority"] == "background"
