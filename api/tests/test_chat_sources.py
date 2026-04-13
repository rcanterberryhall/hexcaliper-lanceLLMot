"""
test_chat_sources.py — Verify sources SSE event from POST /chat.

Checks that:
 - A sources event is emitted before the done event.
 - sources.documents contains name, chunk (≤100 chars), and score fields.
 - sources.graph_nodes contains entity and relation fields.
 - sources event is present even when RAG returns zero results.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock


# ── Helpers ───────────────────────────────────────────────────────────────────


def _sse_events(response_text: str) -> list[dict]:
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
def chat_mocks_with_graph(monkeypatch):
    """Patch RAG and graph to return known data for attribution checks."""
    import rag
    import graph
    import web_fetch
    import ollama as ollama_mod

    monkeypatch.setattr(rag, "search", AsyncMock(return_value=(
        ["chunk text A", "chunk text B"],
        ["doc1", "doc2"],
        ["cid1", "cid2"],
        [0.88, 0.71],
        ["§4.3 Architectural constraints", ""],
    )))
    monkeypatch.setattr(rag, "get_chunks_by_ids", MagicMock(return_value={
        "gcid1": ("graph chunk text", "## Verification strategy"),
    }))
    monkeypatch.setattr(graph, "get_context", MagicMock(return_value=[{
        "chunk_id":      "gcid1",
        "label":         "IEC 61508",
        "context_edge":  "normative_reference",
        "context_score": 0.90,
    }]))
    monkeypatch.setattr(web_fetch, "fetch_context", AsyncMock(return_value={}))

    async def _fake_stream(_client, _payload):
        yield {"message": {"content": "Hi"}, "done": False}
        yield {"message": {"content": ""}, "done": True}

    monkeypatch.setattr(ollama_mod, "stream_chat", _fake_stream)


@pytest.fixture
def chat_mocks_no_docs(monkeypatch):
    """RAG returns zero results — sources event still emitted."""
    import rag
    import graph
    import web_fetch
    import ollama as ollama_mod

    monkeypatch.setattr(rag, "search", AsyncMock(return_value=([], [], [], [], [])))
    monkeypatch.setattr(rag, "get_chunks_by_ids", MagicMock(return_value={}))
    monkeypatch.setattr(graph, "get_context", MagicMock(return_value=[]))
    monkeypatch.setattr(web_fetch, "fetch_context", AsyncMock(return_value={}))

    async def _fake_stream(_client, _payload):
        yield {"message": {"content": "Hi"}, "done": False}
        yield {"message": {"content": ""}, "done": True}

    monkeypatch.setattr(ollama_mod, "stream_chat", _fake_stream)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_sources_event_emitted(app_client, chat_mocks_with_graph):
    """A sources SSE event must be present in the stream."""
    resp = app_client.post("/chat", json={"message": "hello"})
    events = _sse_events(resp.text)
    types = [e.get("type") for e in events]
    assert "sources" in types, f"sources event not found in {types}"


def test_sources_precedes_done(app_client, chat_mocks_with_graph):
    """sources event must arrive before the done event."""
    resp = app_client.post("/chat", json={"message": "hello"})
    events = _sse_events(resp.text)
    types = [e.get("type") for e in events]
    src_idx  = types.index("sources")
    done_idx = types.index("done")
    assert src_idx < done_idx


def test_sources_documents_fields(app_client, chat_mocks_with_graph):
    """Each document entry must have name, chunk (≤100 chars), score, and
    anchor. Anchor carries the #31 structural citation label (empty for
    fixed-window-fallback chunks and pre-#31 documents)."""
    resp = app_client.post("/chat", json={"message": "hello"})
    events = _sse_events(resp.text)
    src = next(e for e in events if e.get("type") == "sources")

    assert len(src["documents"]) == 2
    for doc in src["documents"]:
        assert "name"   in doc
        assert "chunk"  in doc
        assert "score"  in doc
        assert "anchor" in doc
        assert len(doc["chunk"]) <= 100
        assert 0.0 <= doc["score"] <= 1.0

    # First doc has an anchor (structured chunk); second is empty (legacy).
    assert src["documents"][0]["anchor"] == "§4.3 Architectural constraints"
    assert src["documents"][1]["anchor"] == ""


def test_sources_graph_nodes_fields(app_client, chat_mocks_with_graph):
    """Graph node entries must have entity and relation fields."""
    resp = app_client.post("/chat", json={"message": "hello"})
    events = _sse_events(resp.text)
    src = next(e for e in events if e.get("type") == "sources")

    assert len(src["graph_nodes"]) > 0
    node = src["graph_nodes"][0]
    assert node["entity"] == "IEC 61508"
    assert node["relation"] == "normative_reference"


def test_sources_emitted_with_no_docs(app_client, chat_mocks_no_docs):
    """sources event must still be emitted when RAG returns nothing."""
    resp = app_client.post("/chat", json={"message": "hello"})
    events = _sse_events(resp.text)
    src = next((e for e in events if e.get("type") == "sources"), None)
    assert src is not None
    assert src["documents"] == []
    assert src["graph_nodes"] == []
