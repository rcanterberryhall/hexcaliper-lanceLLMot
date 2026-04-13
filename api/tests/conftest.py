"""
conftest.py — Shared pytest fixtures for Hexcaliper unit tests.

All tests that touch the database or the FastAPI app use `isolated_db` to get
a fresh in-file SQLite database in a temporary directory, avoiding cross-test
contamination.  The `app_client` fixture builds a Starlette TestClient with all
external services (Ollama, ChromaDB, graph indexing) mocked out.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


# ── Isolated SQLite database ─────────────────────────────────────────────────

@pytest.fixture(autouse=False)
def isolated_db(tmp_path, monkeypatch):
    """
    Give each test its own SQLite database.

    Patches config.DB_PATH to a temp file, resets the module-level connection
    singleton in db.py, and cleans up afterwards.
    """
    import config
    import db as db_module

    db_file = str(tmp_path / "test_hexcaliper.db")
    monkeypatch.setattr(config, "DB_PATH", db_file)

    # Force the module to create a new connection to the temp file.
    old_conn = db_module._conn
    db_module._conn = None

    yield db_file

    # Teardown: close and discard the test connection.
    if db_module._conn is not None:
        try:
            db_module._conn.close()
        except Exception:
            pass
    db_module._conn = old_conn


# ── Mocked external services ─────────────────────────────────────────────────

@pytest.fixture
def mock_externals(monkeypatch):
    """
    Replace all calls to Ollama, ChromaDB, graph indexing, and the parser
    with lightweight stubs.  Returns a namespace of the mocks for assertions.
    """
    import rag
    import graph
    import ollama
    import parser
    import copyright_extract

    mocks = MagicMock()

    monkeypatch.setattr(rag, "ingest", AsyncMock(return_value=3))
    monkeypatch.setattr(rag, "delete_chunks", MagicMock())
    monkeypatch.setattr(rag, "update_chunk_scope", MagicMock())
    monkeypatch.setattr(rag, "get_doc_chunks", MagicMock(return_value=[]))

    monkeypatch.setattr(graph, "index_document", MagicMock())
    monkeypatch.setattr(graph, "index_chunk", MagicMock())
    monkeypatch.setattr(graph, "parse_and_index_chunk_references", MagicMock(return_value=[]))
    monkeypatch.setattr(graph, "parse_and_index_references", MagicMock(return_value=[]))
    monkeypatch.setattr(graph, "delete_document", MagicMock())

    monkeypatch.setattr(ollama, "summarize_document", AsyncMock(return_value="test summary"))
    monkeypatch.setattr(parser, "parse_file", MagicMock(return_value="extracted document text"))
    monkeypatch.setattr(copyright_extract, "extract", MagicMock(return_value=[]))

    mocks.rag    = rag
    mocks.graph  = graph
    mocks.ollama = ollama
    return mocks


# ── FastAPI test client ───────────────────────────────────────────────────────

@pytest.fixture
def app_client(isolated_db, tmp_path, monkeypatch, mock_externals):
    """
    A Starlette TestClient wrapping the full FastAPI app with:
      - a fresh isolated SQLite database
      - a temp LIBRARY_PATH directory
      - all external services mocked
    """
    from starlette.testclient import TestClient
    import config

    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path / "library"))

    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "UPLOADS_PATH", str(uploads_dir))

    # Import app after all patches are in place.
    from app import app

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client
