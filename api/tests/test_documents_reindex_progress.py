"""
test_documents_reindex_progress.py — lancellmot#52

POST /documents/reindex must return immediately with a run_id and kick off a
background task. GET /documents/reindex/status must report progress of the
in-flight run (docs_done, chunks_done, current_doc, eta_seconds).
"""
import io

import pytest


USER = "local@dev"


@pytest.fixture(autouse=True)
def _reset_reindex_runs():
    """Clear cross-test state in the in-memory run registry."""
    from routers import documents as docs_router
    docs_router._reindex_runs.clear()
    yield
    docs_router._reindex_runs.clear()


def _upload(client, filename="doc.pdf"):
    return client.post(
        "/documents",
        params={"doc_type": "standard"},
        files={"file": (filename, io.BytesIO(b"content"), "text/plain")},
    )


def _install_fakes(monkeypatch, per_doc_chunks):
    """
    Replace rag.get_doc_chunks + extractor.extract_chunks_batch + the async
    runner with deterministic stubs. The runner is no-op'd so TestClient
    teardown does not block on a floating asyncio.Task.
    """
    import extractor
    import rag
    from routers import documents as docs_router

    def fake_get_doc_chunks(doc_id):
        return [(f"{doc_id}-c{i}", f"chunk {i}") for i in range(per_doc_chunks)]

    async def fake_extract(chunk_texts, **kwargs):
        return [extractor.ExtractionResult([], [], "", "") for _ in chunk_texts]

    async def fake_run(*args, **kwargs):
        return None

    monkeypatch.setattr(rag, "get_doc_chunks", fake_get_doc_chunks)
    monkeypatch.setattr(extractor, "extract_chunks_batch", fake_extract)
    monkeypatch.setattr(docs_router, "_run_reindex", fake_run)
    docs_router._reindex_runs.clear()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_reindex_returns_run_id_and_202(app_client, monkeypatch):
    """POST /documents/reindex returns 202 + run_id + pre-computed totals."""
    _install_fakes(monkeypatch, per_doc_chunks=5)
    _upload(app_client, "a.pdf")

    r = app_client.post("/documents/reindex")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["run_id"]
    assert body["docs_total"] == 1
    assert body["chunks_total"] == 5


def test_reindex_is_idempotent_while_running(app_client, monkeypatch):
    """A second POST while a run is active returns the existing run_id."""
    _install_fakes(monkeypatch, per_doc_chunks=3)
    _upload(app_client, "a.pdf")

    first = app_client.post("/documents/reindex").json()
    second = app_client.post("/documents/reindex").json()
    assert first["run_id"] == second["run_id"]


def test_reindex_status_reports_totals(app_client, monkeypatch):
    """GET /documents/reindex/status returns run state for the active run."""
    _install_fakes(monkeypatch, per_doc_chunks=4)
    _upload(app_client, "a.pdf")
    _upload(app_client, "b.pdf")

    post = app_client.post("/documents/reindex").json()
    run_id = post["run_id"]

    r = app_client.get("/documents/reindex/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == run_id
    assert body["status"] == "running"
    assert body["docs_total"] == 2
    assert body["chunks_total"] == 8
    assert body["docs_done"] == 0
    assert body["chunks_done"] == 0


def test_reindex_status_is_idle_with_no_run(app_client):
    """GET status when nothing has ever run returns an idle record."""
    r = app_client.get("/documents/reindex/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "idle"
    assert body.get("run_id") in (None, "")
