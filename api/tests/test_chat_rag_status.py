"""
test_chat_rag_status.py — Verify rag_status SSE event emission from POST /chat.

Checks that:
 - A rag_status event is emitted as the first data event (before any token).
 - status=="ok" when all retrieval succeeds with doc counts reported.
 - status=="error" when RAG search raises, with the error captured in errors[].
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────


def _sse_events(response_text: str) -> list[dict]:
    """Parse all SSE data lines into a list of dicts."""
    events = []
    for line in response_text.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except Exception:
                pass
    return events


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def chat_mocks(monkeypatch):
    """Patch chat-route externals: RAG, graph, web_fetch, ollama."""
    import rag
    import graph
    import web_fetch
    import ollama as ollama_mod

    monkeypatch.setattr(rag, "search", AsyncMock(return_value=(
        ["chunk text A", "chunk text B"],   # doc_chunks
        ["doc1", "doc1"],                    # doc_ids
        ["cid1", "cid2"],                    # chunk_ids
        [0.85, 0.72],                        # scores
        ["", ""],                            # anchors (empty — legacy doc)
    )))
    monkeypatch.setattr(rag, "get_chunks_by_ids", MagicMock(return_value={}))
    monkeypatch.setattr(graph, "get_context", MagicMock(return_value=[]))
    monkeypatch.setattr(web_fetch, "fetch_context", AsyncMock(return_value={}))

    async def _fake_stream(_client, _payload):
        yield {"message": {"content": "Hi"}, "done": False}
        yield {"message": {"content": ""}, "done": True}

    monkeypatch.setattr(ollama_mod, "stream_chat", _fake_stream)


@pytest.fixture
def chat_mocks_rag_fail(monkeypatch):
    """Like chat_mocks but RAG search raises an exception."""
    import rag
    import graph
    import web_fetch
    import ollama as ollama_mod

    monkeypatch.setattr(rag, "search", AsyncMock(side_effect=RuntimeError("vector DB timeout")))  # scores not returned on failure
    monkeypatch.setattr(rag, "get_chunks_by_ids", MagicMock(return_value={}))
    monkeypatch.setattr(graph, "get_context", MagicMock(return_value=[]))
    monkeypatch.setattr(web_fetch, "fetch_context", AsyncMock(return_value={}))

    async def _fake_stream(_client, _payload):
        yield {"message": {"content": "Hi"}, "done": False}
        yield {"message": {"content": ""}, "done": True}

    monkeypatch.setattr(ollama_mod, "stream_chat", _fake_stream)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_rag_status_event_emitted_before_first_token(app_client, chat_mocks):
    """rag_status must be the first SSE event in the stream."""
    resp = app_client.post("/chat", json={"message": "hello"})
    assert resp.status_code == 200

    events = _sse_events(resp.text)
    assert events, "No SSE events received"

    first = events[0]
    assert first["type"] == "rag_status", (
        f"Expected first event type 'rag_status', got {first['type']!r}"
    )


def test_rag_status_ok_with_doc_counts(app_client, chat_mocks):
    """When RAG succeeds, rag_status reports status='ok' and correct docs_used."""
    resp = app_client.post("/chat", json={"message": "hello"})
    events = _sse_events(resp.text)
    rag_ev = next(e for e in events if e.get("type") == "rag_status")

    assert rag_ev["status"] == "ok"
    assert rag_ev["docs_used"] == 2
    assert rag_ev["errors"] == []


def test_rag_status_error_when_search_fails(app_client, chat_mocks_rag_fail):
    """When RAG search raises, rag_status reports status='error' with the message."""
    resp = app_client.post("/chat", json={"message": "hello"})
    events = _sse_events(resp.text)
    rag_ev = next(e for e in events if e.get("type") == "rag_status")

    assert rag_ev["status"] == "error"
    assert rag_ev["docs_used"] == 0
    assert len(rag_ev["errors"]) > 0
    assert "vector DB timeout" in rag_ev["errors"][0]


def test_rag_status_precedes_token_events(app_client, chat_mocks):
    """rag_status must appear before any token events in the stream."""
    resp = app_client.post("/chat", json={"message": "hello"})
    events = _sse_events(resp.text)

    types = [e.get("type") for e in events]
    assert "rag_status" in types
    assert "token" in types

    rag_idx   = types.index("rag_status")
    token_idx = types.index("token")
    assert rag_idx < token_idx, (
        f"rag_status at index {rag_idx} should come before first token at {token_idx}"
    )
