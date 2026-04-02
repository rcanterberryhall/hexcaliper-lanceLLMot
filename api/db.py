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
import uuid
from datetime import datetime, timezone
from typing import Optional

import config
import crypto

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
        copyright_notices TEXT NOT NULL DEFAULT '[]',
        classification    TEXT NOT NULL DEFAULT 'client'
    );

    CREATE TABLE IF NOT EXISTS library_items (
        id           TEXT PRIMARY KEY,
        manufacturer TEXT NOT NULL,
        product_id   TEXT NOT NULL,
        doc_type     TEXT NOT NULL,
        version      TEXT,
        filename     TEXT NOT NULL,
        filepath     TEXT NOT NULL,
        source_url   TEXT,
        checksum     TEXT,
        indexed      INTEGER NOT NULL DEFAULT 0,
        source       TEXT    NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_library_mfr     ON library_items(manufacturer);
    CREATE INDEX IF NOT EXISTS idx_library_product ON library_items(manufacturer, product_id);

    CREATE TABLE IF NOT EXISTS acquisition_queue (
        id           TEXT PRIMARY KEY,
        manufacturer TEXT NOT NULL,
        product_id   TEXT NOT NULL,
        doc_type     TEXT,
        source_url   TEXT,
        reason       TEXT,
        project_id   TEXT,
        status       TEXT NOT NULL DEFAULT 'pending_approval',
        requested_at TEXT NOT NULL,
        approved_at  TEXT,
        completed_at TEXT,
        error        TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_acq_status ON acquisition_queue(status);

    CREATE TABLE IF NOT EXISTS escalation_queue (
        id              TEXT PRIMARY KEY,
        query_text      TEXT NOT NULL,
        source_doc_ids  TEXT NOT NULL DEFAULT '[]',
        has_client_docs INTEGER NOT NULL DEFAULT 0,
        conversation_id TEXT,
        status          TEXT NOT NULL DEFAULT 'pending_approval',
        requested_at    TEXT NOT NULL,
        approved_at     TEXT,
        completed_at    TEXT,
        response        TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_esc_status ON escalation_queue(status);

    CREATE TABLE IF NOT EXISTS connections (
        id      TEXT PRIMARY KEY,
        type    TEXT NOT NULL UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 0,
        config  TEXT NOT NULL DEFAULT '{}'
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

    CREATE TABLE IF NOT EXISTS concept_scope (
        concept_label TEXT NOT NULL,
        scope_type    TEXT NOT NULL,
        scope_id      TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (concept_label, scope_type, scope_id)
    );
    CREATE INDEX IF NOT EXISTS idx_concept_scope ON concept_scope(scope_type, scope_id);

    COMMIT;
    """)


# ── Schema migrations ─────────────────────────────────────────────────────────

def migrate_classification_column() -> None:
    """
    Add the ``classification`` column to ``documents`` for databases created
    before this column existed.  Idempotent — safe to call on every startup.

    All existing documents default to ``'client'`` (the safe conservative
    choice).  Standards (``doc_type='standard'``) that are global-scoped are
    then reclassified to ``'public'`` automatically.
    """
    c = conn()
    cols = {row[1] for row in c.execute("PRAGMA table_info(documents)").fetchall()}
    if "classification" in cols:
        return
    c.execute("ALTER TABLE documents ADD COLUMN classification TEXT NOT NULL DEFAULT 'client'")
    # Reclassify global-scoped standards as public — they are never private.
    c.execute(
        "UPDATE documents SET classification='public' "
        "WHERE scope_type='global' AND doc_type='standard'"
    )


# ── Concept scope backfill ────────────────────────────────────────────────────

def migrate_concept_scope() -> None:
    """
    Backfill concept_scope for concept nodes that were indexed before the
    concept_scope table existed.  All pre-existing concepts are recorded as
    global scope since we can't retroactively determine their original scope.
    Idempotent — already-recorded pairs are skipped via INSERT OR IGNORE.
    """
    c = conn()
    rows = c.execute(
        "SELECT label FROM nodes WHERE node_type='concept'"
    ).fetchall()
    if not rows:
        return
    c.executemany(
        "INSERT OR IGNORE INTO concept_scope (concept_label, scope_type, scope_id) VALUES (?,?,?)",
        [(r[0].lower().strip(), "global", "") for r in rows],
    )


def migrate_credentials_encryption() -> None:
    """
    Encrypt any plain-text credential fields in existing connection configs.

    Idempotent — values that already appear to be Fernet tokens are skipped.
    No-op when CREDENTIALS_KEY is not configured.
    """
    import config as _cfg
    if not _cfg.CREDENTIALS_KEY:
        return
    with lock:
        rows = conn().execute("SELECT id, config FROM connections").fetchall()
        for row in rows:
            try:
                cfg_dict = json.loads(row["config"])
            except (json.JSONDecodeError, TypeError):
                continue
            encrypted = crypto.encrypt_config(cfg_dict)
            if encrypted != cfg_dict:
                conn().execute(
                    "UPDATE connections SET config=? WHERE id=?",
                    (json.dumps(encrypted), row["id"]),
                )


def migrate_library_source_column() -> None:
    """
    Add the ``source`` column to ``library_items`` for databases created before
    this column existed.  Idempotent — safe to call on every startup.
    Items added by web scrapers default to ``''``; M-Files items use ``'mfiles'``.
    """
    c = conn()
    cols = {row[1] for row in c.execute("PRAGMA table_info(library_items)").fetchall()}
    if "source" not in cols:
        c.execute("ALTER TABLE library_items ADD COLUMN source TEXT NOT NULL DEFAULT ''")


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


def list_all_documents(user_email: str) -> list[dict]:
    """Return all documents owned by *user_email* regardless of scope."""
    rows = conn().execute(
        "SELECT * FROM documents WHERE user_email=? ORDER BY created_at DESC",
        (user_email,),
    ).fetchall()
    return [_doc(dict(r)) for r in rows]


def insert_document(doc: dict) -> None:
    conn().execute(
        "INSERT INTO documents "
        "(id,user_email,filename,size_bytes,chunk_count,created_at,"
        " scope_type,scope_id,doc_type,summary,copyright_notices,classification) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc["id"], doc["user_email"], doc["filename"],
         doc.get("size_bytes",0), doc.get("chunk_count",0), doc["created_at"],
         doc.get("scope_type","global"), doc.get("scope_id"),
         doc.get("doc_type","misc"), doc.get("summary",""),
         json.dumps(doc.get("copyright_notices") or []),
         doc.get("classification","client")))


def update_document(doc_id: str, fields: dict) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [doc_id]
    conn().execute(f"UPDATE documents SET {sets} WHERE id=?", vals)


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


# ── Library items ─────────────────────────────────────────────────────────────

def list_library_items(
    manufacturer: Optional[str] = None,
    product_id:   Optional[str] = None,
    doc_type:     Optional[str] = None,
    public_only:  bool = False,
) -> list[dict]:
    clauses, params = [], []
    if manufacturer:
        clauses.append("manufacturer=?"); params.append(manufacturer)
    if product_id:
        clauses.append("product_id=?");   params.append(product_id)
    if doc_type:
        clauses.append("doc_type=?");     params.append(doc_type)
    if public_only:
        clauses.append("source != 'mfiles'")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn().execute(
        f"SELECT * FROM library_items {where} ORDER BY manufacturer, product_id, filename",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_library_item(item_id: str) -> Optional[dict]:
    row = conn().execute(
        "SELECT * FROM library_items WHERE id=?", (item_id,)
    ).fetchone()
    return dict(row) if row else None


def insert_library_item(item: dict) -> None:
    ts = _now_iso()
    conn().execute(
        "INSERT INTO library_items "
        "(id,manufacturer,product_id,doc_type,version,filename,filepath,"
        " source_url,checksum,indexed,source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item["id"], item["manufacturer"], item["product_id"], item["doc_type"],
         item.get("version"), item["filename"], item["filepath"],
         item.get("source_url"), item.get("checksum"),
         item.get("indexed", 0), item.get("source", ""), ts, ts),
    )


def update_library_item(item_id: str, fields: dict) -> None:
    fields = {**fields, "updated_at": _now_iso()}
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [item_id]
    conn().execute(f"UPDATE library_items SET {sets} WHERE id=?", vals)


def delete_library_item(item_id: str) -> None:
    conn().execute("DELETE FROM library_items WHERE id=?", (item_id,))


def list_library_manufacturers(public_only: bool = False) -> list[dict]:
    """Return distinct manufacturers with product counts."""
    where = "WHERE source != 'mfiles'" if public_only else ""
    rows = conn().execute(
        f"SELECT manufacturer, COUNT(DISTINCT product_id) AS product_count, "
        f"COUNT(*) AS doc_count FROM library_items {where} "
        f"GROUP BY manufacturer ORDER BY manufacturer"
    ).fetchall()
    return [dict(r) for r in rows]


# ── Acquisition queue ─────────────────────────────────────────────────────────

def list_acquisition_queue(status: Optional[str] = None) -> list[dict]:
    if status:
        rows = conn().execute(
            "SELECT * FROM acquisition_queue WHERE status=? ORDER BY requested_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn().execute(
            "SELECT * FROM acquisition_queue ORDER BY requested_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_acquisition_item(item_id: str) -> Optional[dict]:
    row = conn().execute(
        "SELECT * FROM acquisition_queue WHERE id=?", (item_id,)
    ).fetchone()
    return dict(row) if row else None


def insert_acquisition_item(item: dict) -> None:
    ts = _now_iso()
    conn().execute(
        "INSERT INTO acquisition_queue "
        "(id,manufacturer,product_id,doc_type,source_url,reason,project_id,"
        " status,requested_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (item["id"], item["manufacturer"], item["product_id"],
         item.get("doc_type"), item.get("source_url"), item.get("reason"),
         item.get("project_id"), item.get("status", "pending_approval"), ts),
    )


def update_acquisition_item(item_id: str, fields: dict) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [item_id]
    conn().execute(f"UPDATE acquisition_queue SET {sets} WHERE id=?", vals)


# ── Escalation queue ─────────────────────────────────────────────────────────

def list_escalation_queue(status: Optional[str] = None) -> list[dict]:
    if status:
        rows = conn().execute(
            "SELECT * FROM escalation_queue WHERE status=? ORDER BY requested_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn().execute(
            "SELECT * FROM escalation_queue ORDER BY requested_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_escalation_item(item_id: str) -> Optional[dict]:
    row = conn().execute(
        "SELECT * FROM escalation_queue WHERE id=?", (item_id,)
    ).fetchone()
    return dict(row) if row else None


def insert_escalation_item(item: dict) -> None:
    ts = _now_iso()
    conn().execute(
        "INSERT INTO escalation_queue "
        "(id,query_text,source_doc_ids,has_client_docs,conversation_id,status,requested_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (item["id"], item["query_text"],
         json.dumps(item.get("source_doc_ids") or []),
         1 if item.get("has_client_docs") else 0,
         item.get("conversation_id"), item.get("status", "pending_approval"), ts),
    )


def update_escalation_item(item_id: str, fields: dict) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [item_id]
    conn().execute(f"UPDATE escalation_queue SET {sets} WHERE id=?", vals)


# ── Connections ───────────────────────────────────────────────────────────────

def _parse_conn_row(d: dict) -> dict:
    """Parse the JSON config blob and decrypt credential fields."""
    try:
        d["config"] = json.loads(d["config"])
    except (json.JSONDecodeError, TypeError):
        d["config"] = {}
    d["config"] = crypto.decrypt_config(d["config"])
    return d


def list_connections() -> list[dict]:
    rows = conn().execute("SELECT * FROM connections ORDER BY type").fetchall()
    return [_parse_conn_row(dict(r)) for r in rows]


def get_connection(conn_type: str) -> Optional[dict]:
    row = conn().execute(
        "SELECT * FROM connections WHERE type=?", (conn_type,)
    ).fetchone()
    if not row:
        return None
    return _parse_conn_row(dict(row))


def upsert_connection(conn_type: str, cfg: dict, enabled: bool = False) -> None:
    existing = get_connection(conn_type)
    row_id   = existing["id"] if existing else str(uuid.uuid4())
    encrypted_cfg = crypto.encrypt_config(cfg)
    conn().execute(
        "INSERT INTO connections (id,type,enabled,config) VALUES (?,?,?,?) "
        "ON CONFLICT(type) DO UPDATE SET config=excluded.config, enabled=excluded.enabled",
        (row_id, conn_type, 1 if enabled else 0, json.dumps(encrypted_cfg)),
    )


def set_connection_enabled(conn_type: str, enabled: bool) -> None:
    conn().execute(
        "UPDATE connections SET enabled=? WHERE type=?", (1 if enabled else 0, conn_type)
    )


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


def record_concept_scope(concept_label: str, scope_type: str, scope_id: str = "") -> None:
    """Record that a concept was observed in the given scope (idempotent)."""
    conn().execute(
        "INSERT OR IGNORE INTO concept_scope (concept_label, scope_type, scope_id) VALUES (?,?,?)",
        (concept_label.lower().strip(), scope_type, scope_id or ""),
    )


def list_concept_vocab(
    scope_types: list[str] | None = None,
    scope_ids: list | None = None,
) -> list[str]:
    """
    Return learned concept labels visible in the given scope hierarchy.

    When *scope_types* is None, returns all known concepts (used for global
    ingest or backward-compat callers).  When provided, returns only concepts
    recorded against at least one of the (scope_type, scope_id) pairs so that
    project-level extractions are seeded with global + client + project vocab
    rather than the entire cross-tenant vocabulary.

    :param scope_types: List of scope type strings to include.
    :param scope_ids:   Parallel list of scope IDs (None or "" means any ID for
                        that scope type, i.e. global-scope concepts).
    :return: Sorted list of distinct concept label strings.
    """
    if not scope_types:
        rows = conn().execute(
            "SELECT DISTINCT concept_label FROM concept_scope ORDER BY concept_label"
        ).fetchall()
        return [r[0] for r in rows]

    if scope_ids is None:
        scope_ids = [None] * len(scope_types)

    clauses: list[str] = []
    params: list = []
    for st, si in zip(scope_types, scope_ids):
        if si is None or si == "":
            clauses.append("(scope_type=?)")
            params.append(st)
        else:
            clauses.append("(scope_type=? AND scope_id=?)")
            params.extend([st, si])

    where = " OR ".join(clauses) if clauses else "1=0"
    rows = conn().execute(
        f"SELECT DISTINCT concept_label FROM concept_scope WHERE {where} ORDER BY concept_label",
        params,
    ).fetchall()
    return [r[0] for r in rows]


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
