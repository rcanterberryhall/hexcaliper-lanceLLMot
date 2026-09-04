"""tests/test_agent.py — agent mode backend (runs client + router).

Hermes and the database are stubbed, so these tests pin the seams:
- event translation for every row of the spec table, ending in done
- rendered-copy persistence: messages + the agent_turns sidecar
- session mapping stored on the first turn and sent on the next
- run.failed → agent_error before done; turn recorded as failed
- unreachable hermes → one agent_error naming agent mode down, then done
- /agent/stop relays to the active run and reports when none is active
- agent_client.stream_events parses SSE frames and skips comments
"""
import asyncio
import json
import os
import sys
import threading
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


def _fake_db():
    """In-memory stand-in for the db module surface the router uses."""
    mod = types.ModuleType("db")
    mod.lock = threading.Lock()
    mod.conversations = {}
    mod.sessions = {}
    mod.turns = []

    def insert_conversation(doc):
        mod.conversations[doc["id"]] = dict(doc)

    def get_conversation(conv_id):
        return mod.conversations.get(conv_id)

    def update_conversation(conv_id, fields):
        mod.conversations[conv_id].update(fields)

    def get_agent_session(conv_id):
        return mod.sessions.get(conv_id)

    def upsert_agent_session(conv_id, sid, skey=""):
        mod.sessions[conv_id] = {"conversation_id": conv_id,
                                 "hermes_session_id": sid,
                                 "hermes_session_key": skey}

    def insert_agent_turn(run_id, conv_id, status, tool_trace):
        mod.turns.append({"run_id": run_id, "conversation_id": conv_id,
                          "status": status, "tool_trace": tool_trace})

    mod.insert_conversation = insert_conversation
    mod.get_conversation = get_conversation
    mod.update_conversation = update_conversation
    mod.get_agent_session = get_agent_session
    mod.upsert_agent_session = upsert_agent_session
    mod.insert_agent_turn = insert_agent_turn
    return mod


def _fake_agent_client(events, session_id="sess-1", create_error=None):
    """Stand-in for agent_client with a scripted event stream."""
    mod = types.ModuleType("agent_client")
    mod.created = []
    mod.stopped = []

    async def create_run(message, session_id=None):
        if create_error is not None:
            raise create_error
        mod.created.append({"message": message, "session_id": session_id})
        return {"run_id": "r1", "status": "started"}

    async def get_run(run_id):
        return {"run_id": run_id, "session_id": session_id,
                "session_key": "key-1"}

    async def stream_events(run_id):
        for e in events:
            yield e

    async def stop_run(run_id):
        mod.stopped.append(run_id)

    mod.create_run = create_run
    mod.get_run = get_run
    mod.stream_events = stream_events
    mod.stop_run = stop_run
    return mod


@pytest.fixture
def make_client(monkeypatch):
    """Build a TestClient over the agent router with scripted hermes."""

    def build(events, **client_kw):
        for mod in list(sys.modules.keys()):
            if mod in ("config", "db", "agent_client",
                       "routers.agent", "routers"):
                sys.modules.pop(mod, None)
        db = _fake_db()
        sys.modules["db"] = db
        ac = _fake_agent_client(events, **client_kw)
        sys.modules["agent_client"] = ac

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import agent as agent_router

        app = FastAPI()
        app.include_router(agent_router.router)
        return TestClient(app), db, ac, agent_router

    return build


def sse_events(response):
    out = []
    for line in response.text.split("\n"):
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: "):]))
    return out


HAPPY_EVENTS = [
    {"event": "tool.started", "tool": "mcp__lancellmot__docs_search"},
    {"event": "tool.completed", "tool": "mcp__lancellmot__docs_search",
     "duration": 0.4},
    {"event": "reasoning.available", "text": "thinking..."},
    {"event": "message.delta", "delta": "The answer "},
    {"event": "message.delta", "delta": "is 42."},
    {"event": "run.completed", "output": "The answer is 42."},
]


def test_translation_and_persistence(make_client):
    client, db, ac, _ = make_client(HAPPY_EVENTS)
    r = client.post("/agent/chat", json={"message": "what is the answer?"})
    events = sse_events(r)
    kinds = [e["type"] for e in events]
    assert kinds == ["tool", "tool", "think", "token", "token", "done"]
    assert events[0] == {"type": "tool",
                         "name": "mcp__lancellmot__docs_search",
                         "status": "running"}
    assert events[1]["status"] == "completed"
    assert events[1]["duration"] == 0.4
    assert "".join(e["content"] for e in events if e["type"] == "token") \
        == "The answer is 42."

    conv_id = events[-1]["conversation_id"]
    msgs = db.conversations[conv_id]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "The answer is 42."
    assert db.turns == [{"run_id": "r1", "conversation_id": conv_id,
                         "status": "completed",
                         "tool_trace": [
                             {"name": "mcp__lancellmot__docs_search",
                              "status": "running"},
                             {"name": "mcp__lancellmot__docs_search",
                              "status": "completed", "duration": 0.4,
                              "error": None}]}]


def test_session_stored_then_sent(make_client):
    client, db, ac, _ = make_client(HAPPY_EVENTS)
    r = client.post("/agent/chat", json={"message": "first"})
    conv_id = sse_events(r)[-1]["conversation_id"]
    assert db.sessions[conv_id]["hermes_session_id"] == "sess-1"
    assert ac.created[0]["session_id"] is None

    client.post("/agent/chat", json={"message": "second",
                                     "conversation_id": conv_id})
    assert ac.created[1]["session_id"] == "sess-1"


def test_run_failed_is_agent_error_then_done(make_client):
    events = [{"event": "message.delta", "delta": "partial "},
              {"event": "run.failed", "error": "budget exhausted"}]
    client, db, ac, _ = make_client(events)
    r = client.post("/agent/chat", json={"message": "hi"})
    got = sse_events(r)
    assert [e["type"] for e in got] == ["token", "agent_error", "done"]
    assert "budget exhausted" in got[1]["detail"]
    assert db.turns[0]["status"] == "failed"
    conv_id = got[-1]["conversation_id"]
    assert db.conversations[conv_id]["messages"][1]["content"] \
        == "partial  *(turn failed)*"


def test_hermes_down_degrades(make_client):
    import httpx
    client, db, ac, _ = make_client(
        [], create_error=httpx.ConnectError("refused"))
    r = client.post("/agent/chat", json={"message": "hi"})
    got = sse_events(r)
    assert [e["type"] for e in got] == ["agent_error", "done"]
    assert "unavailable" in got[0]["detail"]
    assert db.turns == []  # no run existed, nothing to record


def test_stop_relays_to_active_run(make_client):
    client, db, ac, agent_router = make_client(HAPPY_EVENTS)
    agent_router._active_runs["conv-9"] = "r9"
    r = client.post("/agent/stop", json={"conversation_id": "conv-9"})
    assert r.json() == {"stopped": True, "run_id": "r9"}
    assert ac.stopped == ["r9"]

    r = client.post("/agent/stop", json={"conversation_id": "conv-none"})
    assert r.json()["stopped"] is False


def test_stream_events_parses_sse(monkeypatch):
    for mod in ("config", "agent_client"):
        sys.modules.pop(mod, None)
    import agent_client as ac

    frames = (b": keepalive\n\n"
              b"data: {\"event\": \"message.delta\", \"delta\": \"hi\"}\n\n"
              b"not-a-frame\n"
              b"data: {\"event\": \"run.completed\"}\n\n")

    class FakeResp:
        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            # split mid-frame to exercise the buffer path
            yield frames[:25]
            yield frames[25:]

    class FakeStreamCM:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            return FakeStreamCM()

    monkeypatch.setattr(ac.httpx, "AsyncClient", FakeClient)

    async def collect():
        return [e async for e in ac.stream_events("r1")]

    events = asyncio.run(collect())
    assert events == [{"event": "message.delta", "delta": "hi"},
                      {"event": "run.completed"}]
