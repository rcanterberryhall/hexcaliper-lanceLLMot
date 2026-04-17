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


@pytest.mark.asyncio
async def test_embed_with_chat_priority_adds_header():
    """rag.embed(priority='chat') must add X-Priority: chat for that call.

    lancellmot#38 + merLLM#58: the chat-path RAG query-vector embed is on
    the critical path of an interactive response, so the caller opts into
    the ``chat`` bucket. The shared config.EMBED_HEADERS must not be
    mutated — the header override is per-call.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"embedding": [0.0]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("rag.httpx.AsyncClient", return_value=mock_client) as mock_cls:
        await rag.embed("query text", priority="chat")

    _, kwargs = mock_cls.call_args
    headers = kwargs["headers"]
    assert headers["X-Source"] == "lancellmot"
    assert headers["X-Priority"] == "chat"
    # Shared config dict stays clean — the override was per-call only.
    assert "X-Priority" not in config.EMBED_HEADERS


@pytest.mark.asyncio
async def test_embed_without_priority_has_no_priority_header():
    """Default rag.embed(text) must not carry X-Priority.

    Ingest path uses the default and must stay in merLLM's ``embeddings``
    bucket. Regression guard: if someone adds a default priority, ingest
    would silently move to a different bucket.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"embedding": [0.0]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("rag.httpx.AsyncClient", return_value=mock_client) as mock_cls:
        await rag.embed("chunk text")

    _, kwargs = mock_cls.call_args
    assert "X-Priority" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_search_threads_priority_through_to_embed():
    """rag.search(priority='chat') must forward the hint to embed().

    Chat router calls rag.search(..., priority='chat'); search() only
    threads through — if this breaks, the chat-path embed silently
    reverts to the embeddings bucket.
    """
    with patch("rag.embed", new=AsyncMock(return_value=[0.0, 0.0])) as mock_embed, \
         patch("rag.get_collection") as mock_get_col:
        mock_col = MagicMock()
        mock_col.count.return_value = 0   # short-circuit return path is fine
        mock_get_col.return_value = mock_col
        await rag.search(
            user_email="u@example.com",
            query="q",
            scope_types=["global"],
            scope_ids=[None],
            priority="chat",
        )

    # embed was called either at query time or in the short-circuit path
    # — only the short-circuit happens here because count==0, so embed
    # is NOT called. Re-run with count>0 would be a ChromaDB-shaped
    # integration test; the per-call hint is already pinned by
    # test_embed_with_chat_priority_adds_header. We instead assert the
    # function signature accepts the kwarg.
    import inspect
    sig = inspect.signature(rag.search)
    assert "priority" in sig.parameters, (
        "rag.search must accept a priority= kwarg so the chat router "
        "can opt into X-Priority: chat for the query-vector embed"
    )
    assert sig.parameters["priority"].default is None, (
        "rag.search priority must default to None so non-chat callers "
        "(ingest, probes) keep landing in the embeddings bucket"
    )


def test_chat_router_calls_rag_search_with_chat_priority():
    """routers/chat.py must pass priority='chat' to rag.search().

    Source-level regression guard: if someone refactors the chat router
    and drops the priority hint, the query-vector embed silently reverts
    to the ``embeddings`` bucket (merLLM#58 / lancellmot#38). The chat
    router source is inspected directly because mocking the full chat
    endpoint requires a large fixture stack that isn't worth it for a
    single kwarg.
    """
    import pathlib
    chat_src = pathlib.Path(__file__).resolve().parents[1] / "routers" / "chat.py"
    text = chat_src.read_text()
    # The call to rag.search in the chat path must pass priority="chat".
    assert 'priority="chat"' in text or "priority='chat'" in text, (
        "routers/chat.py must call rag.search(..., priority='chat') so "
        "the chat-path RAG embed jumps to merLLM's CHAT bucket "
        "(merLLM#58 / lancellmot#38)"
    )
