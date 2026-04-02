"""
routers/conversations.py — Conversation CRUD endpoints.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, PlainTextResponse

import config
import db

router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user(request: Request) -> str:
    return request.headers.get("cf-access-authenticated-user-email", "local@dev")


@router.get("/conversations")
async def list_conversations(request: Request):
    return [
        {"id": d["id"], "title": d.get("title","Untitled"),
         "model": d.get("model",""), "created_at": d.get("created_at"),
         "updated_at": d.get("updated_at"),
         "system_prompt_id": d.get("system_prompt_id")}
        for d in db.list_conversations(_user(request))
    ]


@router.post("/conversations")
async def create_conversation(request: Request):
    conv_id = str(uuid.uuid4())
    ts      = _now_iso()
    doc = {"id": conv_id, "user_email": _user(request), "title": "New Conversation",
           "model": config.DEFAULT_MODEL, "created_at": ts, "updated_at": ts, "messages": []}
    with db.lock:
        db.insert_conversation(doc)
    return {"id": conv_id, "title": doc["title"], "created_at": ts, "updated_at": ts}


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, request: Request):
    with db.lock:
        doc = db.get_conversation(conv_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if doc["user_email"] != _user(request):
        raise HTTPException(status_code=403, detail="Access denied.")
    return doc


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, request: Request):
    import rag
    user_email = _user(request)
    with db.lock:
        doc = db.get_conversation(conv_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if doc["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        db.delete_conversation(conv_id)
        session_docs = db.list_documents_for_scope(user_email, ["session"], [conv_id])
        for d in session_docs:
            db.delete_document(d["id"])
    for d in session_docs:
        rag.delete_chunks(d["id"])
    return Response(status_code=204)


@router.get("/conversations/{conv_id}/export")
async def export_conversation(conv_id: str, request: Request, format: str = "md"):
    user_email = _user(request)
    with db.lock:
        doc = db.get_conversation(conv_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if doc["user_email"] != user_email:
        raise HTTPException(status_code=403, detail="Access denied.")

    sp = None
    if doc.get("system_prompt_id"):
        with db.lock:
            sp = db.get_system_prompt(doc["system_prompt_id"])

    safe_title = (doc.get("title") or conv_id).replace("/", "-").replace("\\", "-")

    if format == "json":
        payload = {
            "id": doc["id"],
            "title": doc.get("title"),
            "model": doc.get("model"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
            "system_prompt": {"name": sp["name"], "content": sp["content"]} if sp else None,
            "messages": [
                {
                    "role": m.get("role"),
                    "content": m.get("content"),
                    "timestamp": m.get("ts"),
                    "model": doc.get("model") if m.get("role") == "assistant" else None,
                }
                for m in (doc.get("messages") or [])
            ],
        }
        headers = {
            "Content-Disposition": f'attachment; filename="{safe_title}.json"',
        }
        return Response(
            content=json.dumps(payload, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers=headers,
        )

    # Markdown format
    lines = []
    lines.append(f"# {doc.get('title') or conv_id}")
    lines.append("")
    lines.append(f"**Conversation ID:** {conv_id}")
    lines.append(f"**Model:** {doc.get('model') or '(unknown)'}")
    lines.append(f"**Created:** {doc.get('created_at', '')}")
    if sp:
        lines.append("")
        lines.append(f"**System Prompt:** {sp['name']}")
        lines.append("")
        lines.append(sp["content"])
    lines.append("")
    lines.append("---")
    lines.append("")
    for m in (doc.get("messages") or []):
        role = m.get("role", "user")
        ts   = m.get("ts", "")
        label = "User" if role == "user" else "Assistant"
        lines.append(f"**{label}** ({ts}):")
        lines.append("")
        lines.append(m.get("content", ""))
        lines.append("")
        lines.append("---")
        lines.append("")

    md_text = "\n".join(lines)
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_title}.md"',
    }
    return PlainTextResponse(content=md_text, headers=headers)


@router.patch("/conversations/{conv_id}")
async def rename_conversation(conv_id: str, request: Request):
    body  = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    user_email = _user(request)
    with db.lock:
        doc = db.get_conversation(conv_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if doc["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        db.update_conversation(conv_id, {"title": title, "updated_at": _now_iso()})
    return {"id": conv_id, "title": title}
