"""
test_extractor_priority.py — Verify extractor calls run at batch priority.

Concept extraction during bulk ingest is not latency-sensitive. It must go
out with X-Priority: batch so merLLM waits for a GPU slot instead of timing
out at INTERACTIVE_QUEUE_TIMEOUT and silently dropping graph edges.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import extractor


def test_extractor_headers_are_batch_priority():
    """config.OLLAMA_EXTRACTOR_HEADERS must mark requests as batch priority."""
    assert config.OLLAMA_EXTRACTOR_HEADERS["X-Priority"] == "batch"
    assert config.OLLAMA_EXTRACTOR_HEADERS["X-Source"] == "lancellmot"


def test_global_ollama_headers_remain_interactive():
    """Global OLLAMA_HEADERS must NOT set batch priority — RAG/query must stay interactive."""
    assert "X-Priority" not in config.OLLAMA_HEADERS


@pytest.mark.asyncio
async def test_extract_chunk_sends_batch_priority_header():
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
    assert kwargs["headers"]["X-Priority"] == "batch"
