"""
routers/escalation.py — Cloud escalation queue for unanswerable queries.

When the local Ollama model can't adequately answer a query (either because
it lacks knowledge or the user explicitly requests it), the query can be
escalated to a cloud LLM (Anthropic or OpenAI).

Security rules:
  - If the query context includes client documents (has_client_docs=true),
    the user MUST explicitly approve before any data leaves the system.
  - If all context is public (has_client_docs=false) AND AUTO_ESCALATE=true,
    the item is approved automatically at submission time.

Queue lifecycle:
  pending_approval → approved → in_progress → complete
                              ↘              ↘ failed → (retry) → in_progress …
  pending_approval → rejected

Prefix: /escalation
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
import db
import rag

log = logging.getLogger(__name__)
router = APIRouter(prefix="/escalation")

# ── SSE pub/sub ───────────────────────────────────────────────────────────────

_subscribers: list[asyncio.Queue] = []


def _publish(event: dict) -> None:
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ── Cloud provider call ───────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SYSTEM_PROMPT = (
    "You are a highly knowledgeable technical assistant specialising in industrial "
    "automation, process control, and engineering systems. "
    "Answer the user's question as clearly and accurately as possible. "
    "If you reference specific standards, product manuals, or specifications, "
    "cite them by name so the user can look them up."
)


async def _call_anthropic(query_text: str) -> str:
    """Call the Anthropic Messages API and return the full response text."""
    if not config.ESCALATION_API_KEY:
        raise ValueError("ESCALATION_API_KEY is not set.")

    payload = {
        "model":      config.ESCALATION_MODEL,
        "max_tokens": 2048,
        "system":     _SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": query_text}],
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         config.ESCALATION_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


async def _call_openai(query_text: str) -> str:
    """Call the OpenAI Chat Completions API and return the full response text."""
    if not config.ESCALATION_API_KEY:
        raise ValueError("ESCALATION_API_KEY is not set.")

    payload = {
        "model":    config.ESCALATION_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": query_text},
        ],
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.ESCALATION_API_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _call_cloud(query_text: str) -> str:
    provider = config.ESCALATION_PROVIDER.lower()
    if provider == "openai":
        return await _call_openai(query_text)
    return await _call_anthropic(query_text)


# ── Background escalation task ────────────────────────────────────────────────

async def _run_escalation(item_id: str) -> None:
    """Run the cloud escalation for an approved queue item."""
    with db.lock:
        item = db.get_escalation_item(item_id)
    if not item:
        return

    _publish({"type": "start", "id": item_id, "query_text": item["query_text"]})

    with db.lock:
        db.update_escalation_item(item_id, {"status": "in_progress"})

    _publish({"type": "thinking", "id": item_id,
              "message": "Checking semantic cache…"})

    try:
        cached = await rag.search_escalation_cache(item["query_text"])
        if cached:
            with db.lock:
                db.update_escalation_item(item_id, {
                    "status":       "complete",
                    "response":     cached,
                    "completed_at": _now_iso(),
                })
            _publish({"type": "complete", "id": item_id, "response": cached, "cached": True})
            return

        _publish({"type": "thinking", "id": item_id,
                  "message": f"Calling {config.ESCALATION_PROVIDER} ({config.ESCALATION_MODEL})…"})

        response_text = await _call_cloud(item["query_text"])

        await rag.store_escalation_cache(item["query_text"], response_text)

        with db.lock:
            db.update_escalation_item(item_id, {
                "status":       "complete",
                "response":     response_text,
                "completed_at": _now_iso(),
            })

        _publish({"type": "complete", "id": item_id, "response": response_text, "cached": False})

    except Exception as exc:
        log.exception("Escalation failed for item %s", item_id)
        err = str(exc)[:500]
        with db.lock:
            db.update_escalation_item(item_id, {"status": "failed", "error": err})
        _publish({"type": "error", "id": item_id, "error": err})


# ── Pydantic models ───────────────────────────────────────────────────────────

class EscalationItemIn(BaseModel):
    query_text:      str
    source_doc_ids:  list[str] = []
    has_client_docs: bool = False
    conversation_id: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/queue")
async def list_queue(status: Optional[str] = None):
    """List escalation queue items, optionally filtered by status."""
    with db.lock:
        items = db.list_escalation_queue(status or None)
    # Parse source_doc_ids from JSON string back to list.
    for item in items:
        if isinstance(item.get("source_doc_ids"), str):
            try:
                item["source_doc_ids"] = json.loads(item["source_doc_ids"])
            except (json.JSONDecodeError, TypeError):
                item["source_doc_ids"] = []
    return items


@router.post("/queue", status_code=201)
async def add_to_queue(body: EscalationItemIn, background_tasks: BackgroundTasks):
    """
    Add a query to the escalation queue.

    If AUTO_ESCALATE is true and has_client_docs is false, the item is
    approved and the cloud call is triggered immediately.
    """
    item_id = str(uuid.uuid4())
    auto = config.AUTO_ESCALATE and not body.has_client_docs
    item = {
        "id":              item_id,
        "query_text":      body.query_text.strip(),
        "source_doc_ids":  body.source_doc_ids,
        "has_client_docs": body.has_client_docs,
        "conversation_id": body.conversation_id,
        "status":          "approved" if auto else "pending_approval",
    }
    with db.lock:
        db.insert_escalation_item(item)
        if auto:
            db.update_escalation_item(item_id, {"approved_at": _now_iso()})

    if auto:
        background_tasks.add_task(_run_escalation, item_id)

    return {**item, "auto_approved": auto}


@router.patch("/queue/{item_id}/approve")
async def approve_item(item_id: str, background_tasks: BackgroundTasks):
    """Approve a pending escalation item and trigger the cloud call."""
    with db.lock:
        item = db.get_escalation_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Escalation item not found.")
    if item["status"] not in ("pending_approval", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Item is in status '{item['status']}' and cannot be approved.",
        )
    with db.lock:
        db.update_escalation_item(item_id, {
            "status":      "approved",
            "approved_at": _now_iso(),
        })
    background_tasks.add_task(_run_escalation, item_id)
    return {"ok": True, "id": item_id, "status": "approved"}


@router.patch("/queue/{item_id}/reject")
async def reject_item(item_id: str):
    """Reject a pending escalation item."""
    with db.lock:
        item = db.get_escalation_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Escalation item not found.")
    if item["status"] != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Item is in status '{item['status']}' and cannot be rejected.",
        )
    with db.lock:
        db.update_escalation_item(item_id, {"status": "rejected"})
    return {"ok": True, "id": item_id, "status": "rejected"}


@router.post("/queue/{item_id}/retry")
async def retry_item(item_id: str, background_tasks: BackgroundTasks):
    """Retry a failed escalation item."""
    with db.lock:
        item = db.get_escalation_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Escalation item not found.")
    if item["status"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Item is in status '{item['status']}'; only 'failed' items can be retried.",
        )
    with db.lock:
        db.update_escalation_item(item_id, {
            "status":      "approved",
            "approved_at": _now_iso(),
        })
    background_tasks.add_task(_run_escalation, item_id)
    return {"ok": True, "id": item_id, "status": "approved"}


@router.delete("/queue/{item_id}", status_code=204)
async def delete_item(item_id: str):
    """Remove an escalation queue item."""
    with db.lock:
        item = db.get_escalation_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Escalation item not found.")
    with db.lock:
        db.update_escalation_item(item_id, {"status": "deleted"})


@router.get("/stream")
async def stream_events():
    """
    Server-Sent Events stream for real-time escalation progress.

    Events:
      {"type": "start",    "id": ..., "query_text": ...}
      {"type": "thinking", "id": ..., "message": ...}
      {"type": "complete", "id": ..., "response": ...}
      {"type": "error",    "id": ..., "error": ...}
    """
    async def generate():
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        _subscribers.append(q)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )
