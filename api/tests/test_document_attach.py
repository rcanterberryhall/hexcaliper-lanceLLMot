"""
test_document_attach.py — Verify POST /api/documents/{doc_id}/attach.

Checks that:
 - A document with indexed chunks can be attached to an active conversation.
 - The new session-scoped document record is returned.
 - 404 is returned when doc_id or conversation_id is unknown.
 - 403 is returned when the document belongs to a different user.
"""
import json
import uuid
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# ── Helpers ───────────────────────────────────────────────────────────────────

USER = "alice@example.com"
OTHER = "bob@example.com"


def _headers(user=USER):
    return {"cf-access-authenticated-user-email": user}


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_attach_returns_session_doc(app_client):
    import db, rag

    doc_id  = str(uuid.uuid4())
    conv_id = str(uuid.uuid4())

    # Seed a document and a conversation
    with db.lock:
        db.insert_document({
            "id": doc_id, "user_email": USER,
            "filename": "spec.pdf", "size_bytes": 100,
            "chunk_count": 2, "created_at": "2026-01-01T00:00:00Z",
            "scope_type": "global", "scope_id": None,
            "doc_type": "standard", "classification": "public",
            "summary": "A spec", "copyright_notices": [],
        })
        db.insert_conversation({
            "id": conv_id, "user_email": USER,
            "title": "test", "model": "llm",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "messages": [],
        })

    # Fake get_doc_chunks to return some text
    with patch.object(rag, "get_doc_chunks", return_value=[
        ("chunk0", "first chunk"), ("chunk1", "second chunk"),
    ]):
        with patch.object(rag, "ingest", new_callable=AsyncMock, return_value=2):
            resp = app_client.post(
                f"/documents/{doc_id}/attach",
                json={"conversation_id": conv_id},
                headers=_headers(),
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_type"]  == "session"
    assert body["scope_id"]    == conv_id
    assert body["filename"]    == "spec.pdf"
    assert body["chunk_count"] == 2


def test_attach_404_unknown_doc(app_client):
    import db
    conv_id = str(uuid.uuid4())
    with db.lock:
        db.insert_conversation({
            "id": conv_id, "user_email": USER, "title": "t",
            "model": "m", "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z", "messages": [],
        })
    resp = app_client.post(
        f"/documents/{uuid.uuid4()}/attach",
        json={"conversation_id": conv_id},
        headers=_headers(),
    )
    assert resp.status_code == 404


def test_attach_404_unknown_conversation(app_client):
    import db, rag

    doc_id = str(uuid.uuid4())
    with db.lock:
        db.insert_document({
            "id": doc_id, "user_email": USER, "filename": "x.pdf",
            "size_bytes": 10, "chunk_count": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "scope_type": "global", "scope_id": None,
            "doc_type": "misc", "classification": "public",
            "summary": "", "copyright_notices": [],
        })
    resp = app_client.post(
        f"/documents/{doc_id}/attach",
        json={"conversation_id": str(uuid.uuid4())},
        headers=_headers(),
    )
    assert resp.status_code == 404


def test_attach_403_wrong_user(app_client):
    import db

    doc_id  = str(uuid.uuid4())
    conv_id = str(uuid.uuid4())
    with db.lock:
        db.insert_document({
            "id": doc_id, "user_email": OTHER, "filename": "y.pdf",
            "size_bytes": 10, "chunk_count": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "scope_type": "global", "scope_id": None,
            "doc_type": "misc", "classification": "public",
            "summary": "", "copyright_notices": [],
        })
        db.insert_conversation({
            "id": conv_id, "user_email": USER, "title": "t",
            "model": "m", "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z", "messages": [],
        })
    resp = app_client.post(
        f"/documents/{doc_id}/attach",
        json={"conversation_id": conv_id},
        headers=_headers(USER),
    )
    assert resp.status_code == 403


def test_attach_422_no_chunks(app_client):
    import db, rag

    doc_id  = str(uuid.uuid4())
    conv_id = str(uuid.uuid4())
    with db.lock:
        db.insert_document({
            "id": doc_id, "user_email": USER, "filename": "empty.pdf",
            "size_bytes": 0, "chunk_count": 0,
            "created_at": "2026-01-01T00:00:00Z",
            "scope_type": "global", "scope_id": None,
            "doc_type": "misc", "classification": "public",
            "summary": "", "copyright_notices": [],
        })
        db.insert_conversation({
            "id": conv_id, "user_email": USER, "title": "t",
            "model": "m", "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z", "messages": [],
        })
    with patch.object(rag, "get_doc_chunks", return_value=[]):
        resp = app_client.post(
            f"/documents/{doc_id}/attach",
            json={"conversation_id": conv_id},
            headers=_headers(),
        )
    assert resp.status_code == 422
