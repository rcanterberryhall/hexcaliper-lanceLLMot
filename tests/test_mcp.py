"""tests/test_mcp.py — MCP tool server (registry, dispatch, transport).

First test module in this repo. Covers:
- token auth: 401 on missing, wrong, and unconfigured token (fail closed)
- protocol envelopes: initialize, notifications/initialized (202),
  tools/list, unknown method (-32601), unknown tool (-32602)
- tools/call on each of the four tools with stubbed backing modules,
  including the awaited async ``docs.search`` path
- tool exception → isError result, not a protocol error
- schema property names match handler signatures (the wire contract —
  dispatch expands arguments by keyword, so a rename breaks callers)
"""
import inspect
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

TOKEN = "test-token-123"


@pytest.fixture
def client(monkeypatch):
    """TestClient over a minimal app mounting only the MCP router."""
    monkeypatch.setenv("MCP_TOKEN", TOKEN)
    for mod in list(sys.modules.keys()):
        if mod in ("config", "mcp_tools", "routers.mcp", "routers"):
            sys.modules.pop(mod, None)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import mcp as mcp_router

    app = FastAPI()
    app.include_router(mcp_router.router)
    return TestClient(app, raise_server_exceptions=True)


def rpc(client, method, params=None, token=TOKEN, id_=1):
    headers = {}
    if token is not None:
        headers["X-LanceLLMot-MCP-Token"] = token
    return client.post("/mcp",
                       json={"jsonrpc": "2.0", "id": id_,
                             "method": method, "params": params or {}},
                       headers=headers)


# ── auth ──────────────────────────────────────────────────────────────────────

def test_missing_token_401(client):
    assert rpc(client, "tools/list", token=None).status_code == 401


def test_wrong_token_401(client):
    assert rpc(client, "tools/list", token="nope").status_code == 401


def test_unconfigured_token_rejects_everything(monkeypatch):
    monkeypatch.delenv("MCP_TOKEN", raising=False)
    for mod in list(sys.modules.keys()):
        if mod in ("config", "mcp_tools", "routers.mcp", "routers"):
            sys.modules.pop(mod, None)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import mcp as mcp_router

    app = FastAPI()
    app.include_router(mcp_router.router)
    c = TestClient(app)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                             "method": "tools/list"},
               headers={"X-LanceLLMot-MCP-Token": ""})
    assert r.status_code == 401


# ── protocol ──────────────────────────────────────────────────────────────────

def test_initialize_envelope(client):
    r = rpc(client, "initialize").json()
    result = r["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "lancellmot-docs"
    assert "tools" in result["capabilities"]


def test_initialized_notification_202(client):
    r = client.post("/mcp",
                    json={"jsonrpc": "2.0",
                          "method": "notifications/initialized"},
                    headers={"X-LanceLLMot-MCP-Token": TOKEN})
    assert r.status_code == 202


def test_tools_list_names_all_four(client):
    r = rpc(client, "tools/list").json()
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"docs.search", "docs.get", "graph.context",
                     "library.list"}


def test_unknown_method_32601(client):
    r = rpc(client, "resources/list").json()
    assert r["error"]["code"] == -32601


def test_unknown_tool_32602(client):
    r = rpc(client, "tools/call", {"name": "nope", "arguments": {}}).json()
    assert r["error"]["code"] == -32602


# ── tools/call with stubbed backings ─────────────────────────────────────────

def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def test_docs_search_awaits_async_and_uses_chat_priority(client, monkeypatch):
    calls = {}

    async def fake_search(user_email, query, top_k=8, priority=None, **kw):
        calls.update(user=user_email, query=query, top_k=top_k,
                     priority=priority)
        return (["chunk text"], ["doc-1"], ["chunk-1"], [0.87], ["§4.1"])

    _stub_module("rag", search=fake_search)
    r = rpc(client, "tools/call",
            {"name": "docs.search",
             "arguments": {"query": "contactor exercising", "top_k": 3}}).json()
    payload = json.loads(r["result"]["content"][0]["text"])
    assert r["result"]["isError"] is False
    assert payload["results"][0]["doc_id"] == "doc-1"
    assert payload["results"][0]["anchor"] == "§4.1"
    assert calls["priority"] == "chat"
    assert calls["top_k"] == 3
    assert calls["user"] == "local@dev"


def test_docs_get(client):
    _stub_module("db",
                 get_document=lambda doc_id: {"id": doc_id, "title": "FDS"},
                 list_library_items=lambda **kw: [])
    r = rpc(client, "tools/call",
            {"name": "docs.get", "arguments": {"doc_id": "d1"}}).json()
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["title"] == "FDS"


def test_graph_context(client):
    seen = {}

    def fake_get_context(chunk_id, user_email, max_n=5, **kw):
        seen.update(chunk_id=chunk_id, user=user_email, max_n=max_n)
        return [{"chunk_id": "c2", "context_score": 0.9}]

    _stub_module("graph", get_context=fake_get_context)
    r = rpc(client, "tools/call",
            {"name": "graph.context",
             "arguments": {"chunk_id": "c1", "max_n": 2}}).json()
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["related"][0]["chunk_id"] == "c2"
    assert seen == {"chunk_id": "c1", "user": "local@dev", "max_n": 2}


def test_library_list_filters(client):
    seen = {}

    def fake_list(manufacturer=None, product_id=None, doc_type=None):
        seen.update(manufacturer=manufacturer, doc_type=doc_type)
        return [{"id": "L1", "manufacturer": "SEW"}]

    _stub_module("db", list_library_items=fake_list,
                 get_document=lambda doc_id: None)
    r = rpc(client, "tools/call",
            {"name": "library.list",
             "arguments": {"manufacturer": "SEW",
                           "doc_type": "manual"}}).json()
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["items"][0]["id"] == "L1"
    assert seen == {"manufacturer": "SEW", "doc_type": "manual"}


def test_tool_exception_becomes_iserror(client):
    def boom(doc_id):
        raise RuntimeError("db unavailable")

    _stub_module("db", get_document=boom,
                 list_library_items=lambda **kw: [])
    r = rpc(client, "tools/call",
            {"name": "docs.get", "arguments": {"doc_id": "d1"}}).json()
    assert r["result"]["isError"] is True
    assert "db unavailable" in r["result"]["content"][0]["text"]


# ── wire-contract pin ─────────────────────────────────────────────────────────

def test_schema_matches_signature(client):
    import mcp_tools

    for name, fn in mcp_tools.TOOL_REGISTRY.items():
        schema_props = set(mcp_tools.TOOL_SCHEMAS[name]["properties"])
        sig_params = set(inspect.signature(fn).parameters)
        assert schema_props == sig_params, (
            f"{name}: schema {schema_props} != signature {sig_params}")
        required = set(mcp_tools.TOOL_SCHEMAS[name].get("required", []))
        assert required <= schema_props
