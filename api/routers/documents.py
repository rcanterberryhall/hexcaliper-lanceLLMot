"""
routers/documents.py — Document upload, listing, and deletion.
"""
import asyncio
import json
import logging
import mimetypes
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import config
import copyright_extract
import db
import extractor
import graph
import ollama
import parser
import rag
from models import DOC_TYPES

log = logging.getLogger(__name__)

router = APIRouter()

# ── Server-side upload tracking ───────────────────────────────────────────────
# Maps doc_id → {filename, started_at} for every upload currently in progress.
# Single asyncio event loop — no lock needed.
_active_uploads: dict[str, dict] = {}


def active_upload_snapshot() -> list[dict]:
    """Return a list of in-progress uploads with elapsed time, newest first."""
    now = time.time()
    return sorted(
        [{"filename": v["filename"],
          "stage": v.get("stage", "uploading"),
          "elapsed_sec": round(now - v["started_at"], 1)}
         for v in _active_uploads.values()],
        key=lambda x: x["elapsed_sec"],
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_bytes(path: str, data: bytes) -> None:
    """Write bytes to disk. Runs in a thread via asyncio.to_thread."""
    with open(path, "wb") as f:
        f.write(data)


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
         "summary": d.get("summary",""), "classification": d.get("classification","client")}
        for d in docs
    ]


async def _finalize_document(
    doc_id:     str,
    text:       str,
    doc_type:   str,
    scope_type: str,
    scope_id:   Optional[str],
) -> None:
    """
    Background finalization for a freshly uploaded document.

    This runs *after* ``POST /documents`` has returned 200, so the
    request-path stays fast (parse + embed + DB insert only, typically under
    5 s). It does the slow, LLM-bound work that used to block the response:

      1. Summarize the document (``summarize_document``) and run the local
         copyright notice extractor — gathered concurrently.
      2. Write ``summary`` and ``copyright_notices`` back into the documents
         row via ``db.update_document``.
      3. Run per-chunk concept/entity extraction via
         ``rag.index_concepts_for_doc`` and index the results as graph hub
         nodes.

    Each stage is independently fault-tolerant: a failure in one stage logs a
    warning and the others continue, because a document that's embedded and
    searchable is still useful even if its summary or concept graph couldn't
    be produced. The ``_active_uploads`` dashboard entry is popped in the
    ``finally`` block so the user sees the document leave the
    "still processing" list as soon as finalization exits (success or not).
    """
    try:
        try:
            summary, notices = await asyncio.gather(
                ollama.summarize_document(text),
                asyncio.to_thread(copyright_extract.extract, text),
            )
        except Exception as exc:
            log.warning("finalize_document %s: summarize/copyright failed: %s",
                        doc_id, exc)
            summary, notices = "", []

        try:
            with db.lock:
                db.update_document(doc_id, {
                    "summary":           summary,
                    "copyright_notices": json.dumps(notices or []),
                })
        except Exception as exc:
            log.warning("finalize_document %s: db update failed: %s", doc_id, exc)

        try:
            await rag.index_concepts_for_doc(
                doc_id, text, doc_type=doc_type,
                scope_type=scope_type, scope_id=scope_id,
            )
        except Exception as exc:
            log.warning("finalize_document %s: concept extraction failed: %s",
                        doc_id, exc)
    finally:
        _active_uploads.pop(doc_id, None)


@router.post("/documents")
async def upload_document(
    request:         Request,
    background:      BackgroundTasks,
    file:            UploadFile = File(...),
    conversation_id: Optional[str] = None,
    project_id:      Optional[str] = None,
    client_id:       Optional[str] = None,
    doc_type:        str = "misc",
    classification:  Optional[str] = None,
    defer_index:     bool = False,
):
    user_email = _user(request)
    data       = await file.read()

    if len(data) > config.MAX_DOC_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB).")

    filename = file.filename or "upload.txt"
    doc_id = str(uuid.uuid4())
    ts     = _now_iso()
    upload = {"filename": filename, "started_at": time.time(), "stage": "parsing"}
    _active_uploads[doc_id] = upload

    # Parse + embed happen in the request path. Summarize + copyright + concept
    # extraction are deferred to ``_finalize_document`` so the POST returns as
    # soon as the document is indexed for retrieval; previously the full
    # pipeline ran inline and pushed the response past the 120 s client
    # timeout on a cold qwen3:32b extractor call. ``_active_uploads`` stays
    # populated through the background stage so the dashboard still shows the
    # document as "in progress".
    try:
        text = await asyncio.to_thread(parser.parse_file, filename, data)
        if not text:
            raise HTTPException(status_code=422, detail="Could not extract text from file.")

        # Persist the original bytes so the Workbench download button can
        # return the source file later. One file per doc, named by doc_id —
        # filename is looked up from the DB when serving.
        upload_path = os.path.join(config.UPLOADS_PATH, doc_id)
        await asyncio.to_thread(_write_bytes, upload_path, data)

        if doc_type not in DOC_TYPES:
            doc_type = "misc"

        scope_type, scope_id = _resolve_scope(conversation_id, project_id, client_id)

        # Auto-classify: client/project-scoped documents are always client-confidential.
        # Global standards are public; manual uploads default to client unless overridden.
        if scope_type in ("client", "project"):
            classification = "client"
        elif classification not in ("public", "client"):
            classification = "public" if scope_type == "global" and doc_type == "standard" else "client"

        upload["stage"] = "embedding"
        chunk_count = await rag.ingest(
            doc_id, user_email, text,
            scope_type=scope_type, scope_id=scope_id,
            title=filename, uploaded_at=ts, doc_type=doc_type,
            skip_concepts=True,  # always deferred — _finalize_document owns the LLM stages
        )
    except Exception:
        _active_uploads.pop(doc_id, None)
        raise

    meta = {
        "id": doc_id, "user_email": user_email, "filename": filename,
        "size_bytes": len(data), "chunk_count": chunk_count, "created_at": ts,
        "scope_type": scope_type, "scope_id": scope_id, "doc_type": doc_type,
        "summary": "", "copyright_notices": [], "classification": classification,
    }
    with db.lock:
        db.insert_document(meta)

    if defer_index:
        # Explicit opt-out — caller asked to skip concept+summary indexing
        # entirely (used by reindex flows / batch imports that will run their
        # own finalization later).
        _active_uploads.pop(doc_id, None)
    else:
        upload["stage"] = "finalizing"
        background.add_task(
            _finalize_document,
            doc_id, text, doc_type, scope_type, scope_id,
        )

    return meta


_VALID_SCOPES = ("global", "client", "project", "session")


class DocumentPatch(BaseModel):
    doc_type:       Optional[str] = None
    classification: Optional[str] = None
    filename:       Optional[str] = None
    scope_type:     Optional[str] = None
    scope_id:       Optional[str] = None


@router.patch("/documents/{doc_id}")
async def patch_document(doc_id: str, body: DocumentPatch, request: Request):
    """Update mutable document attributes (doc_type, classification, filename, scope)."""
    user_email = _user(request)
    with db.lock:
        doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    if doc["user_email"] != user_email:
        raise HTTPException(status_code=403, detail="Access denied.")

    fields: dict = {}

    # Determine the effective scope after this patch (for classification validation).
    effective_scope = body.scope_type if body.scope_type is not None else doc.get("scope_type", "global")

    if body.scope_type is not None:
        if body.scope_type not in _VALID_SCOPES:
            raise HTTPException(status_code=422, detail=f"Invalid scope_type '{body.scope_type}'.")
        fields["scope_type"] = body.scope_type
        fields["scope_id"]   = body.scope_id  # may be None → NULL

    if body.doc_type is not None:
        if body.doc_type not in DOC_TYPES:
            raise HTTPException(status_code=422, detail=f"Invalid doc_type '{body.doc_type}'.")
        fields["doc_type"] = body.doc_type

    if body.classification is not None:
        if body.classification not in ("public", "client"):
            raise HTTPException(status_code=422, detail="classification must be 'public' or 'client'.")
        if effective_scope in ("client", "project") and body.classification == "public":
            raise HTTPException(
                status_code=422,
                detail="Client and project documents cannot be reclassified as public.",
            )
        fields["classification"] = body.classification
    elif effective_scope in ("client", "project"):
        # If scope is being moved to client/project, enforce classification.
        fields["classification"] = "client"

    if body.filename is not None:
        fname = body.filename.strip()
        if fname:
            fields["filename"] = fname

    if fields:
        with db.lock:
            db.update_document(doc_id, fields)
        # Propagate scope change to ChromaDB chunk metadata.
        if "scope_type" in fields:
            rag.update_chunk_scope(doc_id, fields["scope_type"], fields.get("scope_id"))

    with db.lock:
        doc = db.get_document(doc_id)
    return {
        "id": doc["id"], "filename": doc.get("filename", ""),
        "doc_type": doc.get("doc_type", "misc"),
        "classification": doc.get("classification", "client"),
        "scope_type": doc.get("scope_type", "global"),
        "scope_id": doc.get("scope_id"),
    }


class AttachRequest(BaseModel):
    conversation_id: str


@router.post("/documents/{doc_id}/attach")
async def attach_document_to_conversation(
    doc_id:  str,
    body:    AttachRequest,
    request: Request,
):
    """
    Copy a document into a conversation's session scope so it is
    always available to RAG within that conversation, regardless of
    the conversation's project scope.

    Re-ingests the existing chunks at ``scope_type="session"`` /
    ``scope_id=conversation_id``.  Returns the new session-scoped
    document record.
    """
    user_email = _user(request)

    with db.lock:
        src = db.get_document(doc_id)
    if not src:
        raise HTTPException(status_code=404, detail="Document not found.")
    if src["user_email"] != user_email:
        raise HTTPException(status_code=403, detail="Access denied.")

    # Verify the conversation exists and belongs to this user.
    with db.lock:
        conv = db.get_conversation(body.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conv["user_email"] != user_email:
        raise HTTPException(status_code=403, detail="Access denied.")

    # Reconstruct the full text from existing ChromaDB chunks.
    chunks = rag.get_doc_chunks(doc_id)
    if not chunks:
        raise HTTPException(
            status_code=422,
            detail="Source document has no indexed chunks — re-index it first.",
        )
    full_text = "\n\n".join(text for _, text in sorted(chunks))

    new_doc_id = str(uuid.uuid4())
    ts         = _now_iso()

    await rag.ingest(
        new_doc_id, user_email, full_text,
        scope_type="session", scope_id=body.conversation_id,
        title=src["filename"], uploaded_at=ts,
        doc_type=src.get("doc_type", "misc"),
        skip_concepts=True,   # fast path — reuse the source doc's concepts
    )

    meta = {
        "id":           new_doc_id,
        "user_email":   user_email,
        "filename":     src["filename"],
        "size_bytes":   src.get("size_bytes", 0),
        "chunk_count":  len(chunks),
        "created_at":   ts,
        "scope_type":   "session",
        "scope_id":     body.conversation_id,
        "doc_type":     src.get("doc_type", "misc"),
        "classification": src.get("classification", "client"),
        "summary":      src.get("summary", ""),
        "copyright_notices": src.get("copyright_notices", []),
    }
    with db.lock:
        db.insert_document(meta)

    return meta


@router.get("/documents/{doc_id}/download")
async def download_document(doc_id: str, request: Request):
    """
    Stream the original uploaded bytes back to the user.

    Returns 404 for unknown doc_ids and for legacy docs uploaded before the
    spool existed (their bytes were never persisted). The frontend surfaces
    this as a "download not available" status message.
    """
    user_email = _user(request)
    with db.lock:
        doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    if doc["user_email"] != user_email:
        raise HTTPException(status_code=403, detail="Access denied.")

    upload_path = os.path.join(config.UPLOADS_PATH, doc_id)
    if not os.path.exists(upload_path):
        raise HTTPException(
            status_code=404,
            detail="Original bytes unavailable — document predates the download spool.",
        )

    filename = doc.get("filename") or f"{doc_id}.bin"
    media_type, _ = mimetypes.guess_type(filename)
    return FileResponse(
        upload_path,
        media_type=media_type or "application/octet-stream",
        filename=filename,
    )


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
    # Best-effort cleanup of the stored original. Missing file is fine —
    # legacy docs never had one, and a partial upload may have failed before
    # the spool write.
    upload_path = os.path.join(config.UPLOADS_PATH, doc_id)
    try:
        os.unlink(upload_path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("delete_document: could not unlink %s: %s", upload_path, exc)
    return Response(status_code=204)


@router.post("/documents/reindex")
async def reindex_documents(
    request:    Request,
    project_id: Optional[str] = None,
    client_id:  Optional[str] = None,
):
    """
    Re-run concept extraction on all existing document chunks using the current
    vocabulary, back-propagating newly learned keywords to older documents.

    Without query params: reindexes all documents owned by this user.
    With ``project_id``: only that project's documents.
    With ``client_id``: only that client's documents.
    """
    user_email = _user(request)

    with db.lock:
        if project_id:
            docs = db.list_documents_for_scope(user_email, ["project"], [project_id])
        elif client_id:
            docs = db.list_documents_for_scope(user_email, ["client"], [client_id])
        else:
            docs = db.list_all_documents(user_email)

    docs_processed   = 0
    chunks_processed = 0

    for doc in docs:
        doc_id     = doc["id"]
        scope_type = doc.get("scope_type", "global")
        scope_id   = doc.get("scope_id") or None
        doc_type   = doc.get("doc_type", "")

        # Build scoped vocabulary — same hierarchy as ingest().
        vocab_scope_types: list[str]        = ["global"]
        vocab_scope_ids:   list[Optional[str]] = [None]
        if scope_type == "client" and scope_id:
            vocab_scope_types.append("client")
            vocab_scope_ids.append(scope_id)
        elif scope_type == "project" and scope_id:
            vocab_scope_types.append("project")
            vocab_scope_ids.append(scope_id)
            proj = db.get_project(scope_id)
            if proj and proj.get("client_id"):
                vocab_scope_types.append("client")
                vocab_scope_ids.append(proj["client_id"])
        elif scope_type == "session" and scope_id:
            vocab_scope_types.append("session")
            vocab_scope_ids.append(scope_id)

        with db.lock:
            learned_vocab = db.list_concept_vocab(
                vocab_scope_types, vocab_scope_ids, limit=extractor.MAX_LEARNED_VOCAB,
            )

        # Collect all chunks for the doc, then route the extraction through
        # merLLM's durable batch queue (hexcaliper#29). A restart mid-reindex
        # used to silently truncate the concept graph for every document
        # still being processed — the batch path persists each chunk in
        # merLLM's SQLite so it resumes after restart instead.
        chunk_pairs       = list(rag.get_doc_chunks(doc_id))
        chunk_ids_for_doc = [cid for cid, _ in chunk_pairs]
        chunk_texts       = [text for _, text in chunk_pairs]
        results           = await extractor.extract_chunks_batch(
            chunk_texts, doc_type=doc_type, learned_vocab=learned_vocab,
        )
        for chunk_id, result in zip(chunk_ids_for_doc, results):
            if not result.is_empty():
                graph.index_chunk_concepts(
                    chunk_id,
                    concepts=result.concepts,
                    entities=result.entities,
                    doc_role=result.doc_role,
                    key_assertion=result.key_assertion,
                    scope_type=scope_type,
                    scope_id=scope_id,
                )
            chunks_processed += 1

        docs_processed += 1

    return {"ok": True, "docs_reindexed": docs_processed, "chunks_processed": chunks_processed}


@router.post("/documents/migrate-concept-scope")
async def migrate_concept_scope():
    """
    Backfill concept_scope for concept nodes indexed before scoped vocabulary
    was introduced.  Assigns them global scope.  Safe to call multiple times.
    """
    with db.lock:
        db.migrate_concept_scope()
    return {"ok": True}
