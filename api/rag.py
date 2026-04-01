"""
rag.py — Retrieval-Augmented Generation (RAG) pipeline.

Provides document chunking, embedding via Ollama, and vector storage /
retrieval via ChromaDB.  Documents are stored per-user and tagged with a
*scope_type* (``"global"``, ``"session"``, ``"project"``, ``"client"``)
and an optional *scope_id* so that search results can be filtered
appropriately for each request.
"""

import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

import chromadb
import db
import extractor
import graph
import httpx

import config

# Characters per chunk when splitting document text.
CHUNK_SIZE = 1000
# Overlap between consecutive chunks to preserve context across boundaries.
CHUNK_OVERLAP = 150

# Number of nearest-neighbour chunks to retrieve per query.
TOP_K = 4
# Maximum cosine distance for a chunk to be considered relevant (0=identical, 1=unrelated).
DISTANCE_THRESHOLD = 0.45

# Lazily initialised ChromaDB collection (singleton).
_collection = None

# Separate collection for the escalation semantic cache.
_cache_collection = None

# Cosine distance threshold for a cache hit (distance = 1 − similarity).
# 0.08 ≈ cosine similarity ≥ 0.92 — require near-identical queries.
CACHE_DISTANCE_THRESHOLD = 0.08


# ── ChromaDB ───────────────────────────────────────────────────

def get_collection():
    """
    Return the shared ChromaDB collection, creating it on first access.

    Uses a module-level singleton so the persistent ChromaDB client is only
    opened once per process.  The collection uses cosine similarity so that
    distance scores are normalised between 0 and 1.

    :return: The ChromaDB ``Collection`` object for document chunks.
    """
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=config.CHROMA_PATH)
        _collection = client.get_or_create_collection(
            "documents",
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


# ── Chunking ───────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    """
    Split *text* into overlapping fixed-size chunks.

    Chunks are CHUNK_SIZE characters wide with a CHUNK_OVERLAP-character
    overlap so context is not lost at chunk boundaries.  Empty chunks
    (e.g. from trailing whitespace) are silently skipped.

    :param text: The document text to split.
    :type text: str
    :return: An ordered list of non-empty text chunks.
    :rtype: list[str]
    """
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start : start + CHUNK_SIZE].strip()
        if chunk:
            chunks.append(chunk)
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ── Embedding ──────────────────────────────────────────────────

async def embed(text: str) -> list[float]:
    """
    Generate a vector embedding for *text* using the Ollama embeddings API.

    :param text: The text to embed.
    :type text: str
    :return: A list of floats representing the embedding vector.
    :rtype: list[float]
    :raises httpx.HTTPStatusError: If the Ollama API returns a non-2xx status.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{config.OLLAMA_BASE_URL}/api/embeddings",
            json={"model": config.EMBED_MODEL, "prompt": text},
        )
    resp.raise_for_status()
    return resp.json()["embedding"]


# ── Ingest ─────────────────────────────────────────────────────

async def ingest(
    doc_id: str,
    user_email: str,
    text: str,
    scope_type: str = "global",
    scope_id: Optional[str] = None,
    title: str = "",
    uploaded_at: str | None = None,
    doc_type: str = "",
) -> int:
    """
    Chunk, embed, and store a document in ChromaDB, then index it in the
    knowledge graph.

    Each chunk is stored with metadata so it can later be filtered by user
    and scope.  Chunk IDs follow the pattern ``"<doc_id>__<index>"``.

    :param doc_id: Unique identifier for the source document.
    :type doc_id: str
    :param user_email: Email of the user who owns this document.
    :type user_email: str
    :param text: Full extracted text of the document.
    :type text: str
    :param scope_type: Visibility scope type — ``"global"``, ``"session"``,
        ``"project"``, or ``"client"``.
    :type scope_type: str
    :param scope_id: The ID qualifying the scope (e.g. conversation ID for
        ``"session"``), or ``None`` for ``"global"``.
    :type scope_id: str | None
    :param title: Human-readable document title (filename); used in graph node label.
    :type title: str
    :param uploaded_at: ISO timestamp for the document; defaults to now.
    :type uploaded_at: str | None
    :return: Number of chunks stored, or 0 if the text produced no chunks.
    :rtype: int
    """
    col = get_collection()
    chunks = chunk_text(text)
    if not chunks:
        return 0
    embeddings = [await embed(c) for c in chunks]
    chunk_ids = [f"{doc_id}__{i}" for i in range(len(chunks))]
    col.add(
        ids=chunk_ids,
        embeddings=embeddings,
        documents=chunks,
        metadatas=[{
            "doc_id":     doc_id,
            "user_email": user_email,
            "scope_type": scope_type,
            "scope_id":   scope_id or "",
        } for _ in chunks],
    )

    # ── Knowledge graph indexing ──────────────────────────────────────────────
    ts = uploaded_at or datetime.now(timezone.utc).isoformat()
    graph.index_document(doc_id, user_email, title or doc_id, scope_type=scope_type, scope_id=scope_id, uploaded_at=ts)
    for i, chunk in enumerate(chunks):
        graph.index_chunk(chunk_ids[i], doc_id, user_email, scope_type=scope_type, scope_id=scope_id, uploaded_at=ts, label=chunk[:80])
        graph.parse_and_index_chunk_references(chunk, chunk_ids[i])
    # Parse normative references from the full document text (capped to avoid
    # excessive processing on very large files; normative refs are near the top).
    graph.parse_and_index_references(text[:60_000], doc_id)

    # ── Concept extraction (LLM) ──────────────────────────────────────────────
    # Build the scope hierarchy for vocabulary lookup so concepts from unrelated
    # clients/projects are not mixed into this document's extraction prompt.
    #
    #   global   → global concepts only
    #   client   → global + that client's concepts
    #   project  → global + owning client's concepts + that project's concepts
    #   session  → global + that session's concepts
    vocab_scope_types: list[str] = ["global"]
    vocab_scope_ids: list = [None]
    if scope_type == "client" and scope_id:
        vocab_scope_types.append("client")
        vocab_scope_ids.append(scope_id)
    elif scope_type == "project" and scope_id:
        vocab_scope_types.append("project")
        vocab_scope_ids.append(scope_id)
        # Also include the owning client's vocabulary.
        project_row = db.get_project(scope_id)
        if project_row and project_row.get("client_id"):
            vocab_scope_types.append("client")
            vocab_scope_ids.append(project_row["client_id"])
    elif scope_type == "session" and scope_id:
        vocab_scope_types.append("session")
        vocab_scope_ids.append(scope_id)

    with db.lock:
        learned_vocab = db.list_concept_vocab(vocab_scope_types, vocab_scope_ids)

    # Extract concepts/entities per chunk and index as graph hub nodes.
    # Failure is non-fatal — each chunk is independently fault-tolerant.
    for i, chunk in enumerate(chunks):
        result = await extractor.extract_chunk(chunk, doc_type=doc_type,
                                               learned_vocab=learned_vocab)
        if not result.is_empty():
            graph.index_chunk_concepts(
                chunk_ids[i],
                concepts=result.concepts,
                entities=result.entities,
                doc_role=result.doc_role,
                key_assertion=result.key_assertion,
                scope_type=scope_type,
                scope_id=scope_id,
            )

    return len(chunks)


# ── Migration ──────────────────────────────────────────────────

def migrate_legacy_scopes() -> None:
    """
    Backfill pre-scope ChromaDB chunks with ``scope_type="global"`` and
    ``scope_id=""``.  One-time, idempotent.

    Older document chunks stored before the scope fields were introduced have
    neither ``scope_type`` nor ``scope_id`` metadata keys (some may have the
    old ``scope`` key).  This function backfills them so scope-based filtering
    works correctly.  Safe to call on every startup — chunks that already have
    ``scope_type`` are left untouched.
    """
    try:
        col = get_collection()
        results = col.get(include=["metadatas"])
        ids_to_update, new_metas = [], []
        for id_, meta in zip(results["ids"], results["metadatas"]):
            if not meta.get("scope_type"):
                # Attempt to translate old single-string scope field.
                old_scope = meta.get("scope", "global")
                if old_scope == "global":
                    new_scope_type = "global"
                    new_scope_id   = ""
                elif old_scope.startswith("conversation:"):
                    new_scope_type = "session"
                    new_scope_id   = old_scope[len("conversation:"):]
                else:
                    new_scope_type = "global"
                    new_scope_id   = ""
                ids_to_update.append(id_)
                new_metas.append({
                    **{k: v for k, v in meta.items() if k != "scope"},
                    "scope_type": new_scope_type,
                    "scope_id":   new_scope_id,
                })
        if ids_to_update:
            col.update(ids=ids_to_update, metadatas=new_metas)
    except Exception:
        pass


# ── Search ─────────────────────────────────────────────────────

async def search(
    user_email: str,
    query: str,
    top_k: int = TOP_K,
    scope_types: list[str] = None,
    scope_ids: list = None,
) -> tuple[list[str], list[str], list[str]]:
    """
    Retrieve the most relevant document chunks for a query.

    Embeds *query* and performs an approximate nearest-neighbour search
    against ChromaDB, filtering by user and scope.  The caller passes lists
    of (scope_type, scope_id) pairs that are combined with ``$or`` so that
    global, project, client, and session chunks can all be searched together.

    :param user_email: Email of the requesting user; used to restrict results
        to documents owned by that user.
    :type user_email: str
    :param query: The natural-language query to search for.
    :type query: str
    :param top_k: Maximum number of candidate chunks to request from ChromaDB
        before distance filtering.
    :type top_k: int
    :param scope_types: List of scope type strings to include (e.g.
        ``["global", "session"]``).  Defaults to ``["global"]``.
    :type scope_types: list[str] | None
    :param scope_ids: Parallel list of scope IDs corresponding to
        *scope_types*.  Use ``None`` for ``"global"`` entries.
    :type scope_ids: list | None
    :return: A tuple of ``(text_chunks, doc_ids, chunk_ids)`` for chunks that
        pass the distance threshold.
    :rtype: tuple[list[str], list[str], list[str]]
    """
    if scope_types is None:
        scope_types = ["global"]
        scope_ids   = [None]
    if scope_ids is None:
        scope_ids = [None] * len(scope_types)

    col = get_collection()
    if col.count() == 0:
        return [], [], []
    query_emb = await embed(query)

    # Build the ChromaDB metadata filter.
    # Each (scope_type, scope_id) pair becomes one clause in a $or.
    scope_clauses = []
    for st, si in zip(scope_types, scope_ids):
        if si is None or si == "":
            scope_clauses.append({"scope_type": {"$eq": st}})
        else:
            scope_clauses.append({
                "$and": [
                    {"scope_type": {"$eq": st}},
                    {"scope_id":   {"$eq": si}},
                ]
            })

    if len(scope_clauses) == 1:
        scope_filter = scope_clauses[0]
    else:
        scope_filter = {"$or": scope_clauses}

    where: dict = {
        "$and": [
            {"user_email": {"$eq": user_email}},
            scope_filter,
        ]
    }

    try:
        results = col.query(
            query_embeddings=[query_emb],
            n_results=top_k,
            where=where,
            include=["documents", "distances", "metadatas", "ids"],
        )
        if not results["documents"]:
            return [], [], []
        chunks, doc_ids, chunk_ids = [], [], []
        for doc, dist, meta, cid in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0],
            results["ids"][0],
        ):
            # Only include chunks that are meaningfully close to the query.
            if dist < DISTANCE_THRESHOLD:
                chunks.append(doc)
                doc_ids.append(meta.get("doc_id", ""))
                chunk_ids.append(cid)
        return chunks, doc_ids, chunk_ids
    except Exception:
        return [], [], []


# ── Deletion ───────────────────────────────────────────────────

def get_doc_chunks(doc_id: str) -> list[tuple[str, str]]:
    """
    Fetch all (chunk_id, text) pairs for a document from ChromaDB.

    Used by the re-index endpoint to iterate over existing chunks without
    needing to re-parse the source file.

    :param doc_id: The document whose chunks to retrieve.
    :return: List of ``(chunk_id, text)`` tuples in storage order.
    """
    col = get_collection()
    try:
        results = col.get(where={"doc_id": doc_id}, include=["documents"])
        return [(cid, doc) for cid, doc in zip(results["ids"], results["documents"]) if doc]
    except Exception:
        return []


def update_chunk_scope(doc_id: str, scope_type: str, scope_id: Optional[str]) -> None:
    """Update scope metadata on all ChromaDB chunks for a document."""
    col = get_collection()
    try:
        results = col.get(where={"doc_id": doc_id}, include=["metadatas"])
    except Exception:
        return
    if not results["ids"]:
        return
    new_metas = [
        {**m, "scope_type": scope_type, "scope_id": scope_id or ""}
        for m in results["metadatas"]
    ]
    col.update(ids=results["ids"], metadatas=new_metas)


def delete_chunks(doc_id: str) -> None:
    """
    Delete all ChromaDB chunks associated with a document.

    Graph cleanup is handled separately by ``db.delete_graph_for_document()``
    called from the router layer.

    :param doc_id: The document ID whose chunks should be removed.
    :type doc_id: str
    """
    col = get_collection()
    col.delete(where={"doc_id": doc_id})


def get_chunks_by_ids(chunk_ids: list[str]) -> dict[str, str]:
    """
    Fetch chunk texts from ChromaDB by their IDs.

    Used to retrieve the text of graph-context chunks (found via graph
    traversal) so they can be injected into the prompt alongside the
    semantically-matched chunks.

    :param chunk_ids: List of chunk IDs to retrieve.
    :type chunk_ids: list[str]
    :return: Mapping of ``{chunk_id: text}`` for found chunks.
    :rtype: dict[str, str]
    """
    if not chunk_ids:
        return {}
    col = get_collection()
    try:
        results = col.get(ids=chunk_ids, include=["documents"])
        return {
            cid: doc
            for cid, doc in zip(results["ids"], results["documents"])
            if doc
        }
    except Exception:
        return {}


# ── Escalation cache ───────────────────────────────────────────

def _get_cache_collection():
    """Return the escalation_cache ChromaDB collection (singleton)."""
    global _cache_collection
    if _cache_collection is None:
        client = chromadb.PersistentClient(path=config.CHROMA_PATH)
        _cache_collection = client.get_or_create_collection(
            "escalation_cache",
            metadata={"hnsw:space": "cosine"},
        )
    return _cache_collection


async def search_escalation_cache(query_text: str) -> Optional[str]:
    """
    Search the escalation cache for a semantically similar previous response.

    Embeds *query_text* and queries the ``escalation_cache`` collection.
    Returns the cached response string if the nearest result is within
    :data:`CACHE_DISTANCE_THRESHOLD`, otherwise ``None``.

    :param query_text: The query to look up.
    :return: Cached response text, or ``None`` if no sufficiently similar entry exists.
    """
    col = _get_cache_collection()
    if col.count() == 0:
        return None
    embedding = await embed(query_text)
    results = col.query(
        query_embeddings=[embedding],
        n_results=1,
        include=["metadatas", "distances"],
    )
    if not results["ids"] or not results["ids"][0]:
        return None
    distance = results["distances"][0][0]
    if distance <= CACHE_DISTANCE_THRESHOLD:
        return results["metadatas"][0][0].get("response", "")
    return None


async def store_escalation_cache(query_text: str, response_text: str) -> None:
    """
    Store a query/response pair in the escalation cache for future reuse.

    :param query_text:    The original query that was sent to the cloud model.
    :param response_text: The cloud model's response.
    """
    col = _get_cache_collection()
    embedding = await embed(query_text)
    col.add(
        ids=[str(_uuid.uuid4())],
        embeddings=[embedding],
        documents=[query_text],
        metadatas=[{"response": response_text, "query_preview": query_text[:200]}],
    )
