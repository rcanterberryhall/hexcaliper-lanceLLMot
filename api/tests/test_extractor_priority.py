"""
test_extractor_priority.py — Pin LanceLLMot's outbound header tagging.

merLLM runs a 5-bucket priority queue (chat > embeddings > short >
feedback > background, strict top-down drain). LanceLLMot owns the
priority decision for two of those paths and merLLM owns the third:

  * **Concept extractor** (chat-style call to /api/chat for entity
    extraction during ingest) — must go out with ``X-Priority: background``
    so merLLM waits indefinitely for a GPU slot instead of timing out at
    INTERACTIVE_QUEUE_TIMEOUT and silently dropping graph edges.

  * **User-facing chat / RAG / probes** — must go out with
    ``X-Priority: chat`` so they land in the latency-sensitive bucket.

  * **Embeddings** — routing is decided by merLLM, not LanceLLMot.
    ``proxy_embeddings`` auto-classifies every /api/embeddings call into
    the dedicated ``embeddings`` bucket regardless of header (merLLM#38).
    LanceLLMot's only responsibility on the embed path is the
    ``X-Source: lancellmot`` tag so the merLLM dashboard can attribute
    queue entries — that's what ``test_embed_sends_lancellmot_source``
    pins. The end-to-end routing contract is pinned by merLLM's own
    ``test_embeddings_auto_classify_to_embeddings_bucket`` integration
    test, not from this side.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import extractor
import rag


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


def test_embed_headers_are_source_tag_only():
    """config.EMBED_HEADERS must carry X-Source but no X-Priority.

    Embedding routing is decided by merLLM (#38). Sending an X-Priority
    here would be silently overridden — so we omit it to keep wire-level
    traffic honest about what we're actually choosing. The X-Source tag
    must remain so the merLLM dashboard can attribute queue entries.
    """
    assert config.EMBED_HEADERS["X-Source"] == "lancellmot"
    assert "X-Priority" not in config.EMBED_HEADERS


@pytest.mark.asyncio
async def test_embed_sends_lancellmot_source():
    """rag.embed() must build its httpx client with EMBED_HEADERS.

    Pins the source-tag contract from lancellmot's side. The end-to-end
    routing contract (that the request lands in the embeddings bucket)
    is pinned by merLLM's own integration test
    test_embeddings_auto_classify_to_embeddings_bucket.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"embedding": [0.0]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("rag.httpx.AsyncClient", return_value=mock_client) as mock_cls:
        await rag.embed("any text")

    _, kwargs = mock_cls.call_args
    assert kwargs["headers"] is config.EMBED_HEADERS
    assert kwargs["headers"]["X-Source"] == "lancellmot"
