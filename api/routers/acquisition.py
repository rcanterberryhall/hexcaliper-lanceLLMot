"""
routers/acquisition.py — Acquisition queue for the technical document library.

The acquisition queue allows the LLM (or a user) to request documentation for
a specific manufacturer product.  Items require user approval before any
network activity is attempted — no web requests are made automatically.

Queue lifecycle:
  pending_approval → approved → in_progress → complete
                              ↘              ↘ failed → (retry) → in_progress …
  pending_approval → rejected

Prefix: /acquisition
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
import db
import scrapers
from routers.escalation import _run_escalation

log = logging.getLogger(__name__)
router = APIRouter(prefix="/acquisition")


# ── SSE pub/sub ───────────────────────────────────────────────────────────────

# Each connected SSE client gets its own asyncio.Queue.
_subscribers: list[asyncio.Queue] = []


def _publish(event: dict) -> None:
    """Broadcast an event to all connected SSE clients (non-blocking)."""
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # Slow client — drop the event rather than block.


# ── Background scraping ───────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_scrape(item_id: str) -> None:
    """
    Background coroutine that runs the scraper for an approved queue item.

    Publishes SSE progress events throughout and updates the DB status when
    complete.  All exceptions are caught so the task never silently crashes.
    """
    with db.lock:
        item = db.get_acquisition_item(item_id)
    if not item:
        return

    manufacturer = item["manufacturer"]
    product_id   = item["product_id"]

    _publish({
        "type":         "start",
        "id":           item_id,
        "manufacturer": manufacturer,
        "product_id":   product_id,
    })

    with db.lock:
        db.update_acquisition_item(item_id, {"status": "in_progress"})

    try:
        scraper = scrapers.get_scraper(manufacturer)
        if scraper is None:
            raise ValueError(
                f"No scraper registered for manufacturer '{manufacturer}'. "
                f"Supported: {', '.join(scrapers.REGISTRY.keys())}"
            )

        _publish({"type": "progress", "id": item_id, "message": "Searching for documentation…"})

        results = await scraper.scrape_product(
            manufacturer=manufacturer,
            product_id=product_id,
            doc_type=item.get("doc_type") or None,
            source_url=item.get("source_url") or None,
        )

        if not results:
            # Nothing found — queue a cloud escalation as last resort.
            esc_query = f"Find technical documentation for {manufacturer} {product_id}"
            if item.get("reason"):
                esc_query += f". Reason: {item['reason']}"

            esc_id = str(uuid.uuid4())
            auto   = config.AUTO_ESCALATE   # public tech docs are always auto-escalatable
            esc_item = {
                "id":              esc_id,
                "query_text":      esc_query,
                "source_doc_ids":  [],
                "has_client_docs": False,
                "conversation_id": None,
                "status":          "approved" if auto else "pending_approval",
            }
            with db.lock:
                db.insert_escalation_item(esc_item)
                if auto:
                    db.update_escalation_item(esc_id, {"approved_at": _now_iso()})

            if auto:
                asyncio.create_task(_run_escalation(esc_id))

            err_msg = (
                f"No documentation found for {manufacturer} {product_id} — "
                + ("queued and auto-approved" if auto else "queued")
                + " for cloud escalation."
            )
            with db.lock:
                db.update_acquisition_item(item_id, {
                    "status": "failed",
                    "error":  err_msg,
                })
            _publish({
                "type":          "escalated",
                "id":            item_id,
                "escalation_id": esc_id,
                "auto":          auto,
                "message":       err_msg,
            })
            return

        files_added = 0
        for r in results:
            if not r.success:
                _publish({"type": "file_error", "id": item_id,
                          "filename": r.filename, "error": r.error})
                continue

            lib_item = {
                "id":           str(uuid.uuid4()),
                "manufacturer": manufacturer,
                "product_id":   product_id,
                "doc_type":     r.doc_type,
                "version":      r.version,
                "filename":     r.filename,
                "filepath":     r.filepath,
                "source_url":   r.url,
                "checksum":     r.checksum,
                "indexed":      0,
            }
            with db.lock:
                # Avoid inserting duplicate filepaths.
                existing = db.list_library_items(manufacturer=manufacturer,
                                                 product_id=product_id)
                if not any(e.get("filepath") == r.filepath for e in existing):
                    db.insert_library_item(lib_item)
                    files_added += 1

            _publish({
                "type":     "file",
                "id":       item_id,
                "filename": r.filename,
                "doc_type": r.doc_type,
            })

        with db.lock:
            db.update_acquisition_item(item_id, {
                "status":       "complete",
                "completed_at": _now_iso(),
            })

        _publish({
            "type":        "complete",
            "id":          item_id,
            "files_added": files_added,
        })

    except Exception as exc:
        log.exception("Acquisition scrape failed for item %s", item_id)
        with db.lock:
            db.update_acquisition_item(item_id, {
                "status": "failed",
                "error":  str(exc)[:500],
            })
        _publish({"type": "error", "id": item_id, "error": str(exc)})


# ── Pydantic models ───────────────────────────────────────────────────────────

class AcquisitionItemIn(BaseModel):
    manufacturer: str
    product_id:   str
    doc_type:     Optional[str] = None
    source_url:   Optional[str] = None
    reason:       Optional[str] = None
    project_id:   Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/queue")
async def list_queue(status: Optional[str] = None):
    """List all queue items, optionally filtered by status."""
    with db.lock:
        return db.list_acquisition_queue(status or None)


@router.post("/queue", status_code=201)
async def add_to_queue(body: AcquisitionItemIn):
    """Add an item to the acquisition queue (status: pending_approval)."""
    item = {
        "id":           str(uuid.uuid4()),
        "manufacturer": body.manufacturer.strip(),
        "product_id":   body.product_id.strip().upper(),
        "doc_type":     body.doc_type,
        "source_url":   body.source_url,
        "reason":       body.reason,
        "project_id":   body.project_id,
        "status":       "pending_approval",
    }
    with db.lock:
        db.insert_acquisition_item(item)
    return item


@router.patch("/queue/{item_id}/approve")
async def approve_item(item_id: str, background_tasks: BackgroundTasks):
    """
    Approve a pending item — triggers the scraper as a background task.
    The scraper runs asynchronously; progress is streamed via GET /acquisition/stream.
    """
    with db.lock:
        item = db.get_acquisition_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found.")
    if item["status"] not in ("pending_approval", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Item is in status '{item['status']}' and cannot be approved.",
        )

    with db.lock:
        db.update_acquisition_item(item_id, {
            "status":      "approved",
            "approved_at": _now_iso(),
            "error":       None,
        })

    background_tasks.add_task(_run_scrape, item_id)
    return {"ok": True, "id": item_id, "status": "approved"}


@router.patch("/queue/{item_id}/reject")
async def reject_item(item_id: str):
    """Reject a pending item."""
    with db.lock:
        item = db.get_acquisition_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found.")
    if item["status"] != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Item is in status '{item['status']}' and cannot be rejected.",
        )
    with db.lock:
        db.update_acquisition_item(item_id, {"status": "rejected"})
    return {"ok": True, "id": item_id, "status": "rejected"}


@router.post("/queue/{item_id}/retry")
async def retry_item(item_id: str, background_tasks: BackgroundTasks):
    """Retry a failed item — re-runs the scraper immediately."""
    with db.lock:
        item = db.get_acquisition_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found.")
    if item["status"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Item is in status '{item['status']}'; only 'failed' items can be retried.",
        )
    with db.lock:
        db.update_acquisition_item(item_id, {
            "status":      "approved",
            "approved_at": _now_iso(),
            "error":       None,
        })
    background_tasks.add_task(_run_scrape, item_id)
    return {"ok": True, "id": item_id, "status": "approved"}


@router.delete("/queue/{item_id}", status_code=204)
async def delete_item(item_id: str):
    """Remove a queue item (any status)."""
    with db.lock:
        item = db.get_acquisition_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found.")
    with db.lock:
        db.update_acquisition_item(item_id, {"status": "deleted"})


@router.get("/stream")
async def stream_events():
    """
    Server-Sent Events stream for real-time acquisition progress.

    Connect once; receives JSON-encoded events for all active scrapes:
      {"type": "start",    "id": ..., "manufacturer": ..., "product_id": ...}
      {"type": "progress", "id": ..., "message": ...}
      {"type": "file",     "id": ..., "filename": ..., "doc_type": ...}
      {"type": "file_error","id": ..., "filename": ..., "error": ...}
      {"type": "complete", "id": ..., "files_added": ...}
      {"type": "error",    "id": ..., "error": ...}

    A keepalive comment (``: keepalive``) is sent every 25 s to prevent the
    connection from being dropped by proxies or browsers.
    """
    async def generate():
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
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
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # Disable nginx buffering for SSE.
            "Connection":       "keep-alive",
        },
    )
