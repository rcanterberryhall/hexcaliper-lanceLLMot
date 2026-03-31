"""
routers/documents.py — Document upload, listing, and deletion.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

import config
import copyright_extract
import db
import ollama
import parser
import rag
from models import DOC_TYPES

router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user(request: Request) -> str:
    return request.headers.get("cf-access-authenticated-user-email", "local@dev")


def _resolve_scope(conversation_id: Optional[str],
                   project_id:      Optional[str],
                   client_id:       Optional[str]) -> tuple[str, Optional[str]]:
    if conversation_id:
        return "session", conversation_id
    if project_id:
        return "project", project_id
    if client_id:
        return "client", client_id
    return "global", None


@router.get("/documents")
async def list_documents(
    request:         Request,
    conversation_id: Optional[str] = None,
    project_id:      Optional[str] = None,
    client_id:       Optional[str] = None,
):
    user_email  = _user(request)
    scope_types = ["global"]
    scope_ids:   list[Optional[str]] = [None]

    if client_id:
        scope_types.append("client");  scope_ids.append(client_id)
    if project_id:
        scope_types.append("project"); scope_ids.append(project_id)
        proj = db.get_project(project_id)
        if proj and proj.get("client_id"):
            scope_types.append("client"); scope_ids.append(proj["client_id"])
    if conversation_id:
        scope_types.append("session"); scope_ids.append(conversation_id)

    with db.lock:
        docs = db.list_documents_for_scope(user_email, scope_types, scope_ids)

    return [
        {"id": d["id"], "filename": d.get("filename","unknown"),
         "size_bytes": d.get("size_bytes",0), "chunk_count": d.get("chunk_count",0),
         "created_at": d.get("created_at"), "scope_type": d.get("scope_type","global"),
         "scope_id": d.get("scope_id"), "doc_type": d.get("doc_type","misc"),
         "summary": d.get("summary","")}
        for d in docs
    ]


@router.post("/documents")
async def upload_document(
    request:         Request,
    file:            UploadFile = File(...),
    conversation_id: Optional[str] = None,
    project_id:      Optional[str] = None,
    client_id:       Optional[str] = None,
    doc_type:        str = "misc",
):
    user_email = _user(request)
    data       = await file.read()

    if len(data) > config.MAX_DOC_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB).")

    filename = file.filename or "upload.txt"
    text     = parser.parse_file(filename, data)
    if not text:
        raise HTTPException(status_code=422, detail="Could not extract text from file.")

    if doc_type not in DOC_TYPES:
        doc_type = "misc"

    scope_type, scope_id = _resolve_scope(conversation_id, project_id, client_id)
    doc_id = str(uuid.uuid4())
    ts     = _now_iso()

    chunk_count = await rag.ingest(
        doc_id, user_email, text,
        scope_type=scope_type, scope_id=scope_id,
        title=filename, uploaded_at=ts, doc_type=doc_type,
    )
    summary, notices = await asyncio.gather(
        ollama.summarize_document(text),
        asyncio.to_thread(copyright_extract.extract, text),
    )

    meta = {
        "id": doc_id, "user_email": user_email, "filename": filename,
        "size_bytes": len(data), "chunk_count": chunk_count, "created_at": ts,
        "scope_type": scope_type, "scope_id": scope_id, "doc_type": doc_type,
        "summary": summary, "copyright_notices": notices,
    }
    with db.lock:
        db.insert_document(meta)
    return meta


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, request: Request):
    user_email = _user(request)
    with db.lock:
        doc = db.get_document(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")
        if doc["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        db.delete_document(doc_id)
    rag.delete_chunks(doc_id)
    return Response(status_code=204)
