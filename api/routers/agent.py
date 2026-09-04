"""Agent mode: conversations executed by the hermes-agent service.

``POST /agent/chat`` runs one agent turn — creating or resuming the
conversation's hermes session (REQ-F-008), relaying hermes's run events
to the browser as the SSE vocabulary ``app.js`` already parses, and
persisting the rendered copy when the turn ends (REQ-F-015: the user
and assistant messages ride the ordinary ``conversations`` store, with
an ``agent_turns`` sidecar row carrying the run id, terminal status,
and tool trace; hermes's own store stays the authoritative transcript).

Event translation (spec §4.1):

    message.delta        → token         (streamed text, REQ-F-002)
    tool.started/completed → tool        (name + status, REQ-F-003)
    reasoning.available  → think         (existing collapsible renderer)
    run.completed        → done
    run.failed / other terminal → agent_error, then done (REQ-F-011:
                           an incomplete turn is presented as such)

An unreachable hermes yields one ``agent_error`` naming agent mode
down, and plain chat is untouched (REQ-F-012). ``POST /agent/stop``
relays to the active run's stop endpoint (REQ-F-010); the browser
closing the stream does the same.

Plain chat's router is deliberately untouched by all of this
(REQ-F-009).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import agent_client
import db

log = logging.getLogger(__name__)

router = APIRouter()

# conversation_id → run_id for the turn currently streaming. One process
# serves the app, so a module dict is the whole registry.
_active_runs: dict[str, str] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _user_email(request: Request) -> str:
    return request.headers.get("cf-access-authenticated-user-email",
                               "local@dev")


class AgentChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class AgentStopRequest(BaseModel):
    conversation_id: str


def _load_or_create_conversation(req: AgentChatRequest,
                                 user_email: str) -> str:
    if req.conversation_id:
        return req.conversation_id
    conv_id = str(uuid.uuid4())
    ts = _now_iso()
    db.insert_conversation({
        "id": conv_id, "user_email": user_email,
        "title": req.message[:60], "model": "agent",
        "created_at": ts, "updated_at": ts, "messages": [],
    })
    return conv_id


def _save_turn(conv_id: str, user_message: str, reply_text: str,
               run_id: str, status: str, tool_trace: list) -> None:
    """Persist the rendered copy: messages + the agent_turns sidecar."""
    ts_now = _now_iso()
    suffix = "" if status == "completed" else f" *(turn {status})*"
    with db.lock:
        conv = db.get_conversation(conv_id)
        if conv is not None:
            updated = list(conv.get("messages", []))
            updated.append({"role": "user", "content": user_message,
                            "ts": ts_now})
            if reply_text or suffix:
                updated.append({"role": "assistant",
                                "content": reply_text + suffix,
                                "ts": ts_now})
            db.update_conversation(conv_id, {"messages": updated,
                                             "updated_at": ts_now,
                                             "model": "agent"})
        db.insert_agent_turn(run_id, conv_id, status, tool_trace)


async def _store_session_mapping(conv_id: str, run_id: str) -> None:
    """Record the hermes session for a first turn (REQ-F-008)."""
    if db.get_agent_session(conv_id) is not None:
        return
    try:
        run = await agent_client.get_run(run_id)
    except httpx.HTTPError:
        return
    session_id = run.get("session_id") or ""
    if session_id:
        db.upsert_agent_session(conv_id, session_id,
                                run.get("session_key") or "")


@router.post("/agent/chat")
async def agent_chat(req: AgentChatRequest, request: Request):
    user_email = _user_email(request)
    conv_id = _load_or_create_conversation(req, user_email)
    session = db.get_agent_session(conv_id)
    session_id = session["hermes_session_id"] if session else None

    async def generate():
        reply_parts: list[str] = []
        tool_trace: list[dict] = []
        status = "failed"
        saved = False

        def save(final_status: str) -> None:
            nonlocal saved
            if saved:
                return
            saved = True
            _save_turn(conv_id, req.message, "".join(reply_parts),
                       run_id, final_status, tool_trace)

        try:
            run = await agent_client.create_run(req.message,
                                                session_id=session_id)
        except httpx.HTTPError as exc:
            log.warning("agent mode unavailable: %s", exc)
            yield _sse({"type": "agent_error",
                        "detail": "Agent mode is unavailable (the hermes "
                                  "service is not reachable). Plain chat "
                                  "still works."})
            yield _sse({"type": "done", "conversation_id": conv_id,
                        "model": "agent", "sources": None, "doc_ids": [],
                        "has_client_docs": False})
            return

        run_id = run["run_id"]
        _active_runs[conv_id] = run_id
        try:
            async for event in agent_client.stream_events(run_id):
                kind = event.get("event", "")
                if kind == "message.delta":
                    reply_parts.append(event.get("delta", ""))
                    yield _sse({"type": "token",
                                "content": event.get("delta", "")})
                elif kind == "tool.started":
                    tool_trace.append({"name": event.get("tool", ""),
                                       "status": "running"})
                    yield _sse({"type": "tool",
                                "name": event.get("tool", ""),
                                "status": "running"})
                elif kind == "tool.completed":
                    entry = {"name": event.get("tool", ""),
                             "status": "error" if event.get("error")
                                       else "completed",
                             "duration": event.get("duration"),
                             "error": event.get("error")}
                    tool_trace.append(entry)
                    yield _sse({"type": "tool", **entry})
                elif kind == "reasoning.available":
                    yield _sse({"type": "think",
                                "content": event.get("text", "")})
                elif kind == "run.completed":
                    status = "completed"
                    final = event.get("output")
                    if final and not reply_parts:
                        reply_parts.append(final)
                        yield _sse({"type": "token", "content": final})
                elif kind.startswith("run."):
                    # failed / stopped / cancelled / budget exhaustion —
                    # any terminal state that is not completion.
                    status = kind.split(".", 1)[1]
                    if status not in ("stopping",):
                        yield _sse({"type": "agent_error",
                                    "detail": event.get("error")
                                    or f"agent turn ended: {status}"})
                # unknown events are dropped, forward-compatibly
        except GeneratorExit:
            # Browser closed the stream: stop the run and keep what
            # streamed so far, marked as stopped (REQ-F-010).
            try:
                await agent_client.stop_run(run_id)
            except httpx.HTTPError:
                pass
            save("stopped")
            raise
        except httpx.HTTPError as exc:
            yield _sse({"type": "agent_error",
                        "detail": f"agent stream failed: {exc}"})
        finally:
            _active_runs.pop(conv_id, None)

        await _store_session_mapping(conv_id, run_id)
        save(status)
        yield _sse({"type": "done", "conversation_id": conv_id,
                    "model": "agent", "sources": None, "doc_ids": [],
                    "has_client_docs": False})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/agent/stop")
async def agent_stop(req: AgentStopRequest):
    run_id = _active_runs.get(req.conversation_id)
    if not run_id:
        return {"stopped": False, "reason": "no active agent turn"}
    try:
        await agent_client.stop_run(run_id)
    except httpx.HTTPError as exc:
        return {"stopped": False, "reason": str(exc)}
    return {"stopped": True, "run_id": run_id}
