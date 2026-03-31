"""
db.py — SQLite database layer for Hexcaliper.

Replaces TinyDB. WAL mode, single shared connection, module-level lock.
Owns graph node/edge tables so graph.py uses this module instead of its
own connection.

Tables: conversations, clients, projects, documents, nodes, edges
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

import config

lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        _create_schema(c)
        _conn = c
    return _conn


def _create_schema(c: sqlite3.Connection) -> None:
    c.executescript("""
    BEGIN;

    CREATE TABLE IF NOT EXISTS conversations (
        id          TEXT PRIMARY KEY,
        user_email  TEXT NOT NULL,
        title       TEXT NOT NULL DEFAULT 'New Conversation',
        model       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        messages    TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS clients (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS projects (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        client_id   TEXT REFERENCES clients(id) ON DELETE CASCADE,
        created_at  TEXT NOT NULL,
        UNIQUE(name, client_id)
    );

    CREATE TABLE IF NOT EXISTS documents (
        id                TEXT PRIMARY KEY,
        user_email        TEXT NOT NULL,
        filename          TEXT NOT NULL,
        size_bytes        INTEGER NOT NULL DEFAULT 0,
        chunk_count       INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL,
        scope_type        TEXT NOT NULL DEFAULT 'global',
        scope_id          TEXT,
        doc_type          TEXT NOT NULL DEFAULT 'misc',
        summary           TEXT NOT NULL DEFAULT '',
        copyright_notices TEXT NOT NULL DEFAULT '[]'
    );

    CREATE INDEX IF NOT EXISTS idx_docs_user  ON documents(user_email);
    CREATE INDEX IF NOT EXISTS idx_docs_scope ON documents(scope_type, scope_id);
    CREATE INDEX IF NOT EXISTS idx_convs_user ON conversations(user_email);

    CREATE TABLE IF NOT EXISTS nodes (
        node_id    TEXT PRIMARY KEY,
        node_type  TEXT NOT NULL,
        label      TEXT NOT NULL,
        properties TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS edges (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        src_id     TEXT    NOT NULL,
        dst_id     TEXT    NOT NULL,
        edge_type  TEXT    NOT NULL,
        weight     REAL    NOT NULL DEFAULT 1.0,
        created_at TEXT    NOT NULL,
        properties TEXT    NOT NULL DEFAULT '{}'
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_unique ON edges(src_id, dst_id, edge_type);
    CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src_id, edge_type);
    CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst_id, edge_type);
    CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);

    COMMIT;
    """)


# ── TinyDB migration ──────────────────────────────────────────────────────────

def migrate_from_tinydb() -> None:
    """Import TinyDB data into SQLite on first run. Idempotent."""
    legacy = config.TINYDB_LEGACY
    if not os.path.exists(legacy):
        return
    try:
        with open(legacy) as f:
            data = json.load(f)
    except Exception:
        return

    c = conn()
    with lock:
        for row in data.get("conversations", {}).get("_default", {}).values():
            try:
                c.execute(
                    "INSERT OR IGNORE INTO conversations "
                    "(id,user_email,title,model,created_at,updated_at,messages) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (row.get("id",""), row.get("user_email",""),
                     row.get("title","New Conversation"),
                     row.get("model", config.DEFAULT_MODEL),
                     row.get("created_at", _now_iso()),
                     row.get("updated_at", _now_iso()),
                     json.dumps(row.get("messages", []))),
                )
            except Exception:
                pass

        for row in data.get("documents", {}).get("_default", {}).values():
            old_scope = row.get("scope", "global")
            if old_scope == "global":
                scope_type, scope_id = "global", None
            elif old_scope.startswith("conversation:"):
                scope_type = "session"
                scope_id   = old_scope[len("conversation:"):]
            else:
                scope_type, scope_id = "global", None
            try:
                c.execute(
                    "INSERT OR IGNORE INTO documents "
                    "(id,user_email,filename,size_bytes,chunk_count,created_at,"
                    " scope_type,scope_id,doc_type,summary,copyright_notices) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (row.get("id",""), row.get("user_email",""),
                     row.get("filename","unknown"), row.get("size_bytes",0),
                     row.get("chunk_count",0), row.get("created_at", _now_iso()),
                     scope_type, scope_id,
                     row.get("doc_type","misc"), row.get("summary",""),
                     json.dumps(row.get("copyright_notices") or [])),
                )
            except Exception:
                pass


# ── Conversations ─────────────────────────────────────────────────────────────

def get_conversation(conv_id: str) -> Optional[dict]:
    row = conn().execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    return _conv(dict(row)) if row else None


def list_conversations(user_email: str) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM conversations WHERE user_email=? ORDER BY updated_at DESC",
        (user_email,),
    ).fetchall()
    return [_conv(dict(r)) for r in rows]


def insert_conversation(doc: dict) -> None:
    conn().execute(
        "INSERT INTO conversations (id,user_email,title,model,created_at,updated_at,messages) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc["id"], doc["user_email"], doc.get("title","New Conversation"),
         doc.get("model",""), doc["created_at"], doc["updated_at"],
         json.dumps(doc.get("messages",[]))))


def update_conversation(conv_id: str, fields: dict) -> None:
    if "messages" in fields:
        fields = {**fields, "messages": json.dumps(fields["messages"])}
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [conv_id]
    conn().execute(f"UPDATE conversations SET {sets} WHERE id=?", vals)


def delete_conversation(conv_id: str) -> None:
    conn().execute("DELETE FROM conversations WHERE id=?", (conv_id,))


def _conv(d: dict) -> dict:
    if isinstance(d.get("messages"), str):
        try:
            d["messages"] = json.loads(d["messages"])
        except Exception:
            d["messages"] = []
    return d


# ── Documents ─────────────────────────────────────────────────────────────────

def get_document(doc_id: str) -> Optional[dict]:
    row = conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    return _doc(dict(row)) if row else None


def list_documents_for_scope(user_email: str,
                              scope_types: list[str],
                              scope_ids: list[Optional[str]]) -> list[dict]:
    """Return docs visible across the given (scope_type, scope_id) pairs."""
    clauses = []
    params: list = [user_email]
    for st, si in zip(scope_types, scope_ids):
        if si is None:
            clauses.append("scope_type=?")
            params.append(st)
        else:
            clauses.append("(scope_type=? AND scope_id=?)")
            params.extend([st, si])
    where = " OR ".join(clauses) if clauses else "1=0"
    rows = conn().execute(
        f"SELECT * FROM documents WHERE user_email=? AND ({where}) ORDER BY created_at DESC",
        params,
    ).fetchall()
    return [_doc(dict(r)) for r in rows]


def insert_document(doc: dict) -> None:
    conn().execute(
        "INSERT INTO documents "
        "(id,user_email,filename,size_bytes,chunk_count,created_at,"
        " scope_type,scope_id,doc_type,summary,copyright_notices) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (doc["id"], doc["user_email"], doc["filename"],
         doc.get("size_bytes",0), doc.get("chunk_count",0), doc["created_at"],
         doc.get("scope_type","global"), doc.get("scope_id"),
         doc.get("doc_type","misc"), doc.get("summary",""),
         json.dumps(doc.get("copyright_notices") or [])))


def delete_document(doc_id: str) -> None:
    conn().execute("DELETE FROM documents WHERE id=?", (doc_id,))


def _doc(d: dict) -> dict:
    if isinstance(d.get("copyright_notices"), str):
        try:
            d["copyright_notices"] = json.loads(d["copyright_notices"])
        except Exception:
            d["copyright_notices"] = []
    return d


# ── Clients ───────────────────────────────────────────────────────────────────

def list_clients() -> list[dict]:
    return [dict(r) for r in
            conn().execute("SELECT * FROM clients ORDER BY name").fetchall()]


def get_client(client_id: str) -> Optional[dict]:
    row = conn().execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    return dict(row) if row else None


def insert_client(client_id: str, name: str) -> dict:
    ts = _now_iso()
    conn().execute("INSERT INTO clients (id,name,created_at) VALUES (?,?,?)",
                   (client_id, name, ts))
    return {"id": client_id, "name": name, "created_at": ts}


def delete_client(client_id: str) -> None:
    conn().execute("DELETE FROM clients WHERE id=?", (client_id,))


# ── Projects ──────────────────────────────────────────────────────────────────

def list_projects(client_id: Optional[str] = None) -> list[dict]:
    if client_id:
        rows = conn().execute(
            "SELECT * FROM projects WHERE client_id=? ORDER BY name", (client_id,)
        ).fetchall()
    else:
        rows = conn().execute("SELECT * FROM projects ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_project(project_id: str) -> Optional[dict]:
    row = conn().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def insert_project(project_id: str, name: str, client_id: str) -> dict:
    ts = _now_iso()
    conn().execute("INSERT INTO projects (id,name,client_id,created_at) VALUES (?,?,?,?)",
                   (project_id, name, client_id, ts))
    return {"id": project_id, "name": name, "client_id": client_id, "created_at": ts}


def delete_project(project_id: str) -> None:
    conn().execute("DELETE FROM projects WHERE id=?", (project_id,))


# ── Graph nodes & edges (used by graph.py) ────────────────────────────────────

def upsert_node(node_id: str, node_type: str, label: str,
                properties: Optional[dict] = None) -> None:
    conn().execute(
        "INSERT INTO nodes (node_id,node_type,label,properties) VALUES (?,?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET "
        "node_type=excluded.node_type, label=excluded.label, properties=excluded.properties",
        (node_id, node_type, label, json.dumps(properties or {})))


def upsert_edge(src_id: str, dst_id: str, edge_type: str,
                weight: float = 1.0, properties: Optional[dict] = None) -> None:
    conn().execute(
        "INSERT INTO edges (src_id,dst_id,edge_type,weight,created_at,properties) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(src_id,dst_id,edge_type) DO UPDATE SET "
        "weight=excluded.weight, created_at=excluded.created_at",
        (src_id, dst_id, edge_type, weight, _now_iso(), json.dumps(properties or {})))


def get_edges_from(node_id: str, edge_type: Optional[str] = None) -> list[dict]:
    if edge_type:
        rows = conn().execute(
            "SELECT * FROM edges WHERE src_id=? AND edge_type=?", (node_id, edge_type)
        ).fetchall()
    else:
        rows = conn().execute("SELECT * FROM edges WHERE src_id=?", (node_id,)).fetchall()
    return [dict(r) for r in rows]


def get_edges_to(node_id: str, edge_type: Optional[str] = None) -> list[dict]:
    if edge_type:
        rows = conn().execute(
            "SELECT * FROM edges WHERE dst_id=? AND edge_type=?", (node_id, edge_type)
        ).fetchall()
    else:
        rows = conn().execute("SELECT * FROM edges WHERE dst_id=?", (node_id,)).fetchall()
    return [dict(r) for r in rows]


def get_node(node_id: str) -> Optional[dict]:
    row = conn().execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("properties"), str):
        try:
            d["properties"] = json.loads(d["properties"])
        except Exception:
            d["properties"] = {}
    return d


def delete_graph_for_document(doc_id: str) -> None:
    """Remove all nodes and edges associated with a document from the graph."""
    c = conn()
    doc_node = f"doc:{doc_id}"
    # Find chunk nodes belonging to this document
    chunk_edges = get_edges_to(doc_node, edge_type="chunk_in_document")
    chunk_node_ids = [e["src_id"] for e in chunk_edges]
    for cn in chunk_node_ids:
        c.execute("DELETE FROM edges WHERE src_id=? OR dst_id=?", (cn, cn))
        c.execute("DELETE FROM nodes WHERE node_id=?", (cn,))
    c.execute("DELETE FROM edges WHERE src_id=? OR dst_id=?", (doc_node, doc_node))
    c.execute("DELETE FROM nodes WHERE node_id=?", (doc_node,))
