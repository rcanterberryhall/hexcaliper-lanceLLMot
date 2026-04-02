"""
test_db.py — Unit tests for db.py (SQLite data layer).

Every test gets a fresh in-memory database via the `isolated_db` fixture.
"""
import pytest
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


# ── Schema ────────────────────────────────────────────────────────────────────

def test_schema_creates_tables(isolated_db):
    import db
    c = db.conn()
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    expected = {"conversations", "clients", "projects", "documents",
                "library_items", "acquisition_queue", "escalation_queue",
                "connections", "nodes", "edges", "concept_scope", "system_prompts"}
    assert expected.issubset(tables)


# ── Clients ───────────────────────────────────────────────────────────────────

def test_insert_and_get_client(isolated_db):
    import db
    with db.lock:
        db.insert_client("c1", "ACME Corp")
        result = db.get_client("c1")
    assert result["name"] == "ACME Corp"


def test_list_clients_returns_all(isolated_db):
    import db
    with db.lock:
        db.insert_client("c1", "Alpha")
        db.insert_client("c2", "Beta")
        clients = db.list_clients()
    assert len(clients) == 2
    names = {c["name"] for c in clients}
    assert names == {"Alpha", "Beta"}


def test_delete_client(isolated_db):
    import db
    with db.lock:
        db.insert_client("c1", "Delete Me")
        db.delete_client("c1")
        result = db.get_client("c1")
    assert result is None


# ── Projects ──────────────────────────────────────────────────────────────────

def test_insert_and_get_project(isolated_db):
    import db
    with db.lock:
        db.insert_client("c1", "Client A")
        db.insert_project("p1", "Project X", "c1")
        proj = db.get_project("p1")
    assert proj["name"] == "Project X"
    assert proj["client_id"] == "c1"


def test_list_projects_for_client(isolated_db):
    import db
    with db.lock:
        db.insert_client("c1", "Client")
        db.insert_project("p1", "Proj A", "c1")
        db.insert_project("p2", "Proj B", "c1")
        projects = db.list_projects(client_id="c1")
    assert len(projects) == 2


def test_cascade_delete_client_removes_projects(isolated_db):
    import db
    with db.lock:
        db.insert_client("c1", "Client")
        db.insert_project("p1", "Proj", "c1")
        db.delete_client("c1")
        proj = db.get_project("p1")
    assert proj is None


# ── Documents ─────────────────────────────────────────────────────────────────

def _make_doc(**kwargs):
    base = {
        "id": "doc1", "user_email": "user@test.com", "filename": "test.pdf",
        "size_bytes": 1024, "chunk_count": 3, "created_at": _now(),
        "scope_type": "global", "scope_id": None,
        "doc_type": "standard", "summary": "", "copyright_notices": [],
        "classification": "public",
    }
    base.update(kwargs)
    return base


def test_insert_and_get_document(isolated_db):
    import db
    doc = _make_doc()
    with db.lock:
        db.insert_document(doc)
        result = db.get_document("doc1")
    assert result["filename"] == "test.pdf"
    assert result["classification"] == "public"


def test_update_document_fields(isolated_db):
    import db
    with db.lock:
        db.insert_document(_make_doc())
        db.update_document("doc1", {"filename": "renamed.pdf", "doc_type": "misc"})
        result = db.get_document("doc1")
    assert result["filename"] == "renamed.pdf"
    assert result["doc_type"] == "misc"


def test_delete_document(isolated_db):
    import db
    with db.lock:
        db.insert_document(_make_doc())
        db.delete_document("doc1")
        result = db.get_document("doc1")
    assert result is None


def test_list_documents_for_scope_global(isolated_db):
    import db
    with db.lock:
        db.insert_document(_make_doc(id="d1", scope_type="global", scope_id=None))
        db.insert_document(_make_doc(id="d2", scope_type="client", scope_id="c1"))
        docs = db.list_documents_for_scope("user@test.com", ["global"], [None])
    ids = {d["id"] for d in docs}
    assert "d1" in ids
    assert "d2" not in ids


def test_list_documents_for_scope_multiple(isolated_db):
    import db
    with db.lock:
        db.insert_document(_make_doc(id="d1", scope_type="global", scope_id=None))
        db.insert_document(_make_doc(id="d2", scope_type="client", scope_id="c1"))
        db.insert_document(_make_doc(id="d3", scope_type="project", scope_id="p1"))
        docs = db.list_documents_for_scope(
            "user@test.com",
            ["global", "client", "project"],
            [None, "c1", "p1"],
        )
    ids = {d["id"] for d in docs}
    assert ids == {"d1", "d2", "d3"}


def test_update_document_scope(isolated_db):
    import db
    with db.lock:
        db.insert_document(_make_doc(scope_type="global", scope_id=None))
        db.update_document("doc1", {"scope_type": "client", "scope_id": "c99"})
        result = db.get_document("doc1")
    assert result["scope_type"] == "client"
    assert result["scope_id"] == "c99"


# ── Library items ─────────────────────────────────────────────────────────────

def _make_lib_item(**kwargs):
    base = {
        "id": "lib1", "manufacturer": "Beckhoff", "product_id": "EL1008",
        "doc_type": "technical_manual", "version": "1.0",
        "filename": "el1008.pdf", "filepath": "/app/data/library/beckhoff/EL1008/el1008.pdf",
        "source_url": None, "checksum": None, "indexed": 0, "source": "scraper",
    }
    base.update(kwargs)
    return base


def test_insert_and_get_library_item(isolated_db):
    import db
    with db.lock:
        db.insert_library_item(_make_lib_item())
        result = db.get_library_item("lib1")
    assert result["manufacturer"] == "Beckhoff"
    assert result["product_id"] == "EL1008"


def test_list_library_items_all(isolated_db):
    import db
    with db.lock:
        db.insert_library_item(_make_lib_item(id="lib1", manufacturer="Beckhoff"))
        db.insert_library_item(_make_lib_item(id="lib2", manufacturer="Siemens",
                                               product_id="S7-300", filename="s7.pdf",
                                               filepath="/app/data/library/siemens/s7.pdf"))
        items = db.list_library_items()
    assert len(items) == 2


def test_list_library_items_filter_by_manufacturer(isolated_db):
    import db
    with db.lock:
        db.insert_library_item(_make_lib_item(id="lib1", manufacturer="Beckhoff"))
        db.insert_library_item(_make_lib_item(id="lib2", manufacturer="Siemens",
                                               product_id="S7", filename="s7.pdf",
                                               filepath="/app/data/library/siemens/s7.pdf"))
        items = db.list_library_items(manufacturer="Beckhoff")
    assert len(items) == 1
    assert items[0]["manufacturer"] == "Beckhoff"


def test_update_library_item(isolated_db):
    import db
    with db.lock:
        db.insert_library_item(_make_lib_item())
        db.update_library_item("lib1", {"indexed": 1, "version": "2.0"})
        result = db.get_library_item("lib1")
    assert result["indexed"] == 1
    assert result["version"] == "2.0"


def test_delete_library_item(isolated_db):
    import db
    with db.lock:
        db.insert_library_item(_make_lib_item())
        db.delete_library_item("lib1")
        result = db.get_library_item("lib1")
    assert result is None


def test_list_library_manufacturers(isolated_db):
    import db
    with db.lock:
        db.insert_library_item(_make_lib_item(id="l1", manufacturer="Beckhoff"))
        db.insert_library_item(_make_lib_item(id="l2", manufacturer="Beckhoff",
                                               product_id="EL2008", filename="el2008.pdf",
                                               filepath="/tmp/el2008.pdf"))
        db.insert_library_item(_make_lib_item(id="l3", manufacturer="Siemens",
                                               product_id="S7", filename="s7.pdf",
                                               filepath="/tmp/s7.pdf"))
        mfrs = db.list_library_manufacturers()
    names = {m["manufacturer"] for m in mfrs}
    assert names == {"Beckhoff", "Siemens"}
    beckhoff = next(m for m in mfrs if m["manufacturer"] == "Beckhoff")
    assert beckhoff["doc_count"] == 2
