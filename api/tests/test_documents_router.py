"""
test_documents_router.py — Router-level tests for /documents endpoints.

Uses a TestClient backed by a fresh SQLite DB and all external services mocked.
"""
import io
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

USER = "local@dev"   # default injected by the router from CF header


def upload(client, filename="test.txt", content=b"test content",
           doc_type="standard", scope="global", client_id=None, project_id=None):
    params = {"doc_type": doc_type}
    if client_id:  params["client_id"]  = client_id
    if project_id: params["project_id"] = project_id
    return client.post(
        "/documents",
        params=params,
        files={"file": (filename, io.BytesIO(content), "text/plain")},
    )


# ── GET /documents ────────────────────────────────────────────────────────────

def test_list_documents_empty(app_client):
    r = app_client.get("/documents")
    assert r.status_code == 200
    assert r.json() == []


def test_list_documents_after_upload(app_client):
    upload(app_client, "iec61508.pdf")
    r = app_client.get("/documents")
    assert r.status_code == 200
    docs = r.json()
    assert len(docs) == 1
    assert docs[0]["filename"] == "iec61508.pdf"


# ── POST /documents ───────────────────────────────────────────────────────────

def test_upload_returns_metadata(app_client):
    r = upload(app_client, "manual.pdf", doc_type="technical_manual")
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "manual.pdf"
    assert body["doc_type"] == "technical_manual"
    assert "id" in body


def test_upload_global_standard_classified_public(app_client):
    r = upload(app_client, "iso13849.pdf", doc_type="standard")
    assert r.json()["classification"] == "public"


def test_upload_global_non_standard_classified_client(app_client):
    r = upload(app_client, "contract.pdf", doc_type="contract")
    assert r.json()["classification"] == "client"


def test_upload_client_scope_forces_client_classification(app_client):
    # Create a client first.
    client_r = app_client.post("/workspace/clients", json={"name": "ACME"})
    cid = client_r.json()["id"]
    r = upload(app_client, "spec.pdf", doc_type="requirement", client_id=cid)
    body = r.json()
    assert body["classification"] == "client"
    assert body["scope_type"] == "client"


def test_upload_invalid_doc_type_falls_back_to_misc(app_client):
    r = upload(app_client, "file.pdf", doc_type="not_a_real_type")
    assert r.json()["doc_type"] == "misc"


# ── PATCH /documents/{id} ─────────────────────────────────────────────────────

def test_patch_filename(app_client):
    doc = upload(app_client, "old_name.pdf").json()
    r = app_client.patch(f"/documents/{doc['id']}",
                         json={"filename": "new_name.pdf"})
    assert r.status_code == 200
    assert r.json()["filename"] == "new_name.pdf"


def test_patch_doc_type(app_client):
    doc = upload(app_client, "doc.pdf", doc_type="misc").json()
    r = app_client.patch(f"/documents/{doc['id']}", json={"doc_type": "fmea"})
    assert r.status_code == 200
    assert r.json()["doc_type"] == "fmea"


def test_patch_invalid_doc_type_returns_422(app_client):
    doc = upload(app_client, "doc.pdf").json()
    r = app_client.patch(f"/documents/{doc['id']}", json={"doc_type": "banana"})
    assert r.status_code == 422


def test_patch_scope_global_to_client(app_client):
    client_r = app_client.post("/workspace/clients", json={"name": "Client X"})
    cid = client_r.json()["id"]
    doc = upload(app_client, "doc.pdf", doc_type="standard").json()
    assert doc["scope_type"] == "global"

    r = app_client.patch(f"/documents/{doc['id']}",
                         json={"scope_type": "client", "scope_id": cid})
    assert r.status_code == 200
    body = r.json()
    assert body["scope_type"] == "client"
    assert body["scope_id"] == cid
    # Moving to client scope forces classification to client.
    assert body["classification"] == "client"


def test_patch_scope_client_to_global(app_client):
    client_r = app_client.post("/workspace/clients", json={"name": "Client Y"})
    cid = client_r.json()["id"]
    doc = upload(app_client, "doc.pdf", client_id=cid).json()
    assert doc["scope_type"] == "client"

    r = app_client.patch(f"/documents/{doc['id']}",
                         json={"scope_type": "global", "scope_id": None,
                               "classification": "public"})
    assert r.status_code == 200
    assert r.json()["scope_type"] == "global"


def test_patch_public_classification_blocked_for_client_scope(app_client):
    client_r = app_client.post("/workspace/clients", json={"name": "Client Z"})
    cid = client_r.json()["id"]
    doc = upload(app_client, "doc.pdf", client_id=cid).json()

    r = app_client.patch(f"/documents/{doc['id']}",
                         json={"classification": "public"})
    assert r.status_code == 422


def test_patch_nonexistent_document_returns_404(app_client):
    r = app_client.patch("/documents/does-not-exist", json={"filename": "x.pdf"})
    assert r.status_code == 404


def test_patch_calls_update_chunk_scope_on_scope_change(app_client, mock_externals):
    import rag
    doc = upload(app_client, "doc.pdf").json()
    app_client.patch(f"/documents/{doc['id']}",
                     json={"scope_type": "global", "scope_id": None})
    rag.update_chunk_scope.assert_called()


# ── DELETE /documents/{id} ────────────────────────────────────────────────────

def test_delete_document(app_client):
    doc = upload(app_client, "to_delete.pdf").json()
    r = app_client.delete(f"/documents/{doc['id']}")
    assert r.status_code == 204
    # Confirm it's gone.
    docs = app_client.get("/documents").json()
    assert not any(d["id"] == doc["id"] for d in docs)


def test_delete_nonexistent_returns_404(app_client):
    r = app_client.delete("/documents/ghost-id")
    assert r.status_code == 404


def test_delete_removes_upload_bytes(app_client):
    import os, config
    doc = upload(app_client, "to_delete.pdf", content=b"payload").json()
    upload_path = os.path.join(config.UPLOADS_PATH, doc["id"])
    assert os.path.exists(upload_path)
    app_client.delete(f"/documents/{doc['id']}")
    assert not os.path.exists(upload_path)


# ── GET /documents/{id}/download ──────────────────────────────────────────────

def test_download_document_returns_original_bytes(app_client):
    content = b"\x89PNG\r\n\x1a\n-original-bytes-"
    doc = upload(app_client, "diagram.png", content=content).json()
    r = app_client.get(f"/documents/{doc['id']}/download")
    assert r.status_code == 200
    assert r.content == content
    # Filename is surfaced via Content-Disposition so the browser saves the original name.
    assert "diagram.png" in r.headers.get("content-disposition", "")


def test_download_document_sets_media_type_from_filename(app_client):
    doc = upload(app_client, "spec.pdf", content=b"%PDF-1.4 fake").json()
    r = app_client.get(f"/documents/{doc['id']}/download")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/pdf")


def test_download_document_404_when_doc_missing(app_client):
    r = app_client.get("/documents/does-not-exist/download")
    assert r.status_code == 404


def test_download_document_404_when_bytes_missing(app_client):
    """Legacy docs uploaded before the spool existed have no stored bytes."""
    import os, config
    doc = upload(app_client, "legacy.pdf", content=b"legacy").json()
    os.unlink(os.path.join(config.UPLOADS_PATH, doc["id"]))
    r = app_client.get(f"/documents/{doc['id']}/download")
    assert r.status_code == 404


# ── POST /documents/reindex + GET /documents/reindex/status ───────────────────

@pytest.fixture
def clean_reindex_state():
    """Wipe module-level reindex tracking dicts before and after each test —
    they persist across the in-process app, which would otherwise leak runs
    between tests."""
    from routers import documents as docs_router
    docs_router._reindex_runs.clear()
    docs_router._user_active_run.clear()
    yield
    docs_router._reindex_runs.clear()
    docs_router._user_active_run.clear()


def test_reindex_returns_run_id_and_initial_state(app_client, clean_reindex_state):
    upload(app_client, "iec61508.pdf")
    r = app_client.post("/documents/reindex")
    assert r.status_code == 200
    body = r.json()
    assert "run_id" in body and body["run_id"]
    # The endpoint returns immediately; status may already be "completed"
    # because TestClient drains BackgroundTasks before returning, but in
    # either case the shape must include the progress fields.
    assert body["status"] in {"running", "completed"}
    assert "docs_total" in body
    assert "chunks_total" in body
    assert "scope" in body and body["scope"] == {"project_id": None, "client_id": None}


def test_reindex_idempotent_per_user_while_active(app_client, clean_reindex_state):
    """Two POSTs without an intervening completion should share a run_id.
    Forced by pre-seeding an in-flight run so the second POST sees it."""
    from routers import documents as docs_router
    upload(app_client, "doc.pdf")
    # Inject a sentinel "running" run for USER and assert the second POST
    # returns it instead of starting a new one.
    docs_router._reindex_runs["sentinel-run"] = {
        "run_id": "sentinel-run", "user_email": USER, "status": "running",
        "started_at": "2026-04-20T00:00:00+00:00", "completed_at": None,
        "scope": {"project_id": None, "client_id": None},
        "docs_total": 1, "docs_done": 0, "chunks_total": 0, "chunks_done": 0,
        "current_doc": None, "rate_samples": [], "error": None,
    }
    docs_router._user_active_run[USER] = "sentinel-run"

    r = app_client.post("/documents/reindex")
    assert r.status_code == 200
    assert r.json()["run_id"] == "sentinel-run"


def test_reindex_status_by_run_id(app_client, clean_reindex_state):
    upload(app_client, "doc.pdf")
    run_id = app_client.post("/documents/reindex").json()["run_id"]
    r = app_client.get(f"/documents/reindex/status?run_id={run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["status"] in {"running", "completed"}


def test_reindex_status_without_run_id_returns_user_run(app_client, clean_reindex_state):
    upload(app_client, "doc.pdf")
    run_id = app_client.post("/documents/reindex").json()["run_id"]
    r = app_client.get("/documents/reindex/status")
    assert r.status_code == 200
    assert r.json()["run_id"] == run_id


def test_reindex_status_unknown_run_id_returns_404(app_client, clean_reindex_state):
    r = app_client.get("/documents/reindex/status?run_id=does-not-exist")
    assert r.status_code == 404


def test_reindex_status_no_runs_for_user_returns_404(app_client, clean_reindex_state):
    r = app_client.get("/documents/reindex/status")
    assert r.status_code == 404


def test_reindex_status_other_user_run_returns_404(app_client, clean_reindex_state):
    """A user must not be able to read another user's run state."""
    from routers import documents as docs_router
    docs_router._reindex_runs["other-user-run"] = {
        "run_id": "other-user-run", "user_email": "someone-else@dev",
        "status": "running", "started_at": "2026-04-20T00:00:00+00:00",
        "completed_at": None, "scope": {"project_id": None, "client_id": None},
        "docs_total": 0, "docs_done": 0, "chunks_total": 0, "chunks_done": 0,
        "current_doc": None, "rate_samples": [], "error": None,
    }
    r = app_client.get("/documents/reindex/status?run_id=other-user-run")
    assert r.status_code == 404
