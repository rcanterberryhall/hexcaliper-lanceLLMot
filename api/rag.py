"""
rag.py — Retrieval-Augmented Generation (RAG) pipeline.

Provides document chunking, embedding via Ollama, and vector storage /
retrieval via ChromaDB.  Documents are stored per-user and tagged with a
*scope_type* (``"global"``, ``"session"``, ``"project"``, ``"client"``)
and an optional *scope_id* so that search results can be filtered
appropriately for each request.
"""

import asyncio
import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

import chromadb
import chunker
import db
import extractor
import graph
import httpx

import config

log = logging.getLogger(__name__)

# Characters per chunk when splitting document text.
CHUNK_SIZE = 1000
# Overlap between consecutive chunks to preserve context across boundaries.
CHUNK_OVERLAP = 150

# Max concurrent embedding requests during ingest (matches 2-GPU round-robin).
EMBED_CONCURRENCY = 4

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

async def embed(text: str, priority: Optional[str] = None) -> list[float]:
    """
    Generate a vector embedding for *text* using the Ollama embeddings API.

    Routing: merLLM defaults every ``/api/embeddings`` call to its
    dedicated ``embeddings`` priority bucket and only honors one
    explicit override — ``X-Priority: chat`` routes the request to
    ``chat`` so a chat-path RAG embed jumps ahead of ingest-chunk
    embeds in the shared ``embeddings`` bucket (merLLM#58). LanceLLMot's
    default stance is still "let merLLM decide" — the chat path opts in
    by passing ``priority="chat"``; ingest keeps the default so bulk
    chunk embeds stay in the ``embeddings`` bucket as before (merLLM#38).

    :param text: The text to embed.
    :type text: str
    :param priority: Optional priority hint. When ``"chat"``, an
        ``X-Priority: chat`` header is added for this request only —
        the shared :data:`config.EMBED_HEADERS` dict is never mutated.
        Any other value (including ``None``) sends the request with
        source-tag only, landing it in merLLM's ``embeddings`` bucket.
    :type priority: str | None
    :return: A list of floats representing the embedding vector.
    :rtype: list[float]
    :raises httpx.HTTPStatusError: If the Ollama API returns a non-2xx status.
    """
    if priority == "chat":
        headers = {**config.EMBED_HEADERS, "X-Priority": "chat"}
    else:
        headers = config.EMBED_HEADERS
    async with httpx.AsyncClient(
        timeout=120.0,
        headers=headers,
    ) as client:
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
    skip_concepts: bool = False,
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
    structured = chunker.chunk_structured(text)
    if not structured:
        return 0
    chunks = [sc.text for sc in structured]
    sem = asyncio.Semaphore(EMBED_CONCURRENCY)

    async def _bounded_embed(c: str) -> list[float]:
        async with sem:
            # Ingest chunks stay in merLLM's default ``embeddings`` bucket —
            # we deliberately don't pass ``priority`` here. Only the chat
            # path opts into CHAT (merLLM#58 + lancellmot#38). The semaphore
            # exists only to bound client-side concurrency to
            # EMBED_CONCURRENCY.
            return await embed(c)

    # return_exceptions so one oversized chunk doesn't kill the whole upload —
    # nomic-embed-text 500s when input exceeds its 2048-token architectural cap
    # and asyncio.gather without this flag aborts the entire ingest.
    raw = await asyncio.gather(
        *[_bounded_embed(c) for c in chunks], return_exceptions=True,
    )
    kept = [i for i, r in enumerate(raw) if not isinstance(r, Exception)]
    for i, r in enumerate(raw):
        if isinstance(r, Exception):
            log.warning("ingest %s: chunk %d (%d chars) embed failed: %s",
                        doc_id, i, len(chunks[i]), r)
    if not kept:
        return 0
    chunks     = [chunks[i] for i in kept]
    structured = [structured[i] for i in kept]
    embeddings = [raw[i] for i in kept]
    chunk_ids  = [f"{doc_id}__{i}" for i in range(len(chunks))]
    col.add(
        ids=chunk_ids,
        embeddings=embeddings,
        documents=chunks,
        metadatas=[{
            "doc_id":     doc_id,
            "user_email": user_email,
            "scope_type": scope_type,
            "scope_id":   scope_id or "",
            # Structural anchor when present ("" for fixed-window fallback
            # chunks) — scaffolding for hexcaliper-lanceLLMot#31; future UI
            # work surfaces this in chat citations and graph views.
            "anchor":     sc.anchor or "",
        } for sc in structured],
    )

    # ── Knowledge graph indexing ──────────────────────────────────────────────
    ts = uploaded_at or datetime.now(timezone.utc).isoformat()
    graph.index_document(doc_id, user_email, title or doc_id, scope_type=scope_type, scope_id=scope_id, uploaded_at=ts)
    for i, sc in enumerate(structured):
        # Prefer the structural anchor as the chunk's graph label — it makes
        # citations like "§4.3 Architectural constraints" readable instead
        # of dumping the first 80 chars of raw text.
        label = sc.anchor or sc.text[:80]
        graph.index_chunk(chunk_ids[i], doc_id, user_email, scope_type=scope_type, scope_id=scope_id, uploaded_at=ts, label=label)
        graph.parse_and_index_chunk_references(sc.text, chunk_ids[i])
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
        learned_vocab = db.list_concept_vocab(
            vocab_scope_types, vocab_scope_ids, limit=extractor.MAX_LEARNED_VOCAB,
        )

    if not skip_concepts:
        # Extract concepts/entities per chunk and index as graph hub nodes.
        # Routes through merLLM's durable batch queue (hexcaliper#29) so a
        # mid-ingest merLLM restart no longer silently drops graph edges.
        # Per-chunk failure is non-fatal — each result is returned as an
        # empty ``ExtractionResult`` in its slot.
        results = await extractor.extract_chunks_batch(
            chunks, doc_type=doc_type, learned_vocab=learned_vocab,
        )
        for i, result in enumerate(results):
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


async def index_concepts_for_doc(
    doc_id: str,
    text: str,
    doc_type: str = "",
    scope_type: str = "global",
    scope_id: Optional[str] = None,
) -> int:
    """
    Run LLM concept/entity extraction for every chunk of a document and index
    the results as graph hub nodes.

    This is the same concept-extraction loop that ``ingest()`` runs inline, but
    extracted as a standalone coroutine so callers can schedule it as a
    background task after returning a quick ``POST /documents`` response. The
    text is re-chunked deterministically (same ``chunker.chunk_structured``
    output and same ``{doc_id}__{i}`` chunk IDs) so it matches what the
    embedding pass stored in ChromaDB — no need to thread chunks through
    the caller.

    Each chunk's extraction is independently fault-tolerant: a per-chunk
    failure logs a warning and continues. Returns the number of chunks that
    produced at least one concept or entity.
    """
    structured = chunker.chunk_structured(text)
    if not structured:
        return 0
    chunks    = [sc.text for sc in structured]
    chunk_ids = [f"{doc_id}__{i}" for i in range(len(chunks))]

    # Same scope hierarchy as ingest() — see rag.ingest for the rationale.
    vocab_scope_types: list[str] = ["global"]
    vocab_scope_ids: list = [None]
    if scope_type == "client" and scope_id:
        vocab_scope_types.append("client")
        vocab_scope_ids.append(scope_id)
    elif scope_type == "project" and scope_id:
        vocab_scope_types.append("project")
        vocab_scope_ids.append(scope_id)
        project_row = db.get_project(scope_id)
        if project_row and project_row.get("client_id"):
            vocab_scope_types.append("client")
            vocab_scope_ids.append(project_row["client_id"])
    elif scope_type == "session" and scope_id:
        vocab_scope_types.append("session")
        vocab_scope_ids.append(scope_id)

    with db.lock:
        learned_vocab = db.list_concept_vocab(
            vocab_scope_types, vocab_scope_ids, limit=extractor.MAX_LEARNED_VOCAB,
        )

    # Route bulk extraction through merLLM's durable batch queue so a restart
    # mid-ingest (power outage, code redeploy) no longer silently loses graph
    # edges for half the document (hexcaliper#29). Per-chunk failure is
    # non-fatal: the extractor returns an empty ``ExtractionResult`` in that
    # slot and the loop just skips it.
    results = await extractor.extract_chunks_batch(
        chunks, doc_type=doc_type, learned_vocab=learned_vocab,
    )
    indexed = 0
    for i, result in enumerate(results):
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
            indexed += 1
    return indexed


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
    except Exception as exc:
        log.warning("migrate_legacy_scopes: %s", exc)


# ── Search ─────────────────────────────────────────────────────

async def search(
    user_email: str,
    query: str,
    top_k: int = TOP_K,
    scope_types: list[str] = None,
    scope_ids: list = None,
    priority: Optional[str] = None,
) -> tuple[list[str], list[str], list[str], list[float], list[str]]:
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
    :param priority: Optional priority hint forwarded to :func:`embed`.
        Chat callers pass ``"chat"`` so the query-vector embed routes
        to merLLM's ``chat`` bucket and doesn't queue behind ingest-chunk
        embeds (merLLM#58). Defaults to ``None`` → embed lands in the
        ``embeddings`` bucket.
    :type priority: str | None
    :return: A tuple of ``(text_chunks, doc_ids, chunk_ids, scores)`` for
        chunks that pass the distance threshold.  Scores are in [0, 1] with
        higher values indicating greater similarity. Anchors carry the
        structural citation label (e.g. ``"§4.3 Architectural
        constraints"``) when the chunk was produced by a structured split;
        empty string for fixed-window-fallback chunks or documents
        ingested before the structure-aware chunker (#31).
    :rtype: tuple[list[str], list[str], list[str], list[float], list[str]]
    """
    if scope_types is None:
        scope_types = ["global"]
        scope_ids   = [None]
    if scope_ids is None:
        scope_ids = [None] * len(scope_types)

    col = get_collection()
    if col.count() == 0:
        return [], [], [], [], []
    query_emb = await embed(query, priority=priority)

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
            include=["documents", "distances", "metadatas"],
        )
        if not results["documents"]:
            return [], [], [], [], []
        chunks, doc_ids, chunk_ids, scores, anchors = [], [], [], [], []
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
                scores.append(round(1.0 - dist, 4))
                # Pre-#31 chunks have no "anchor" metadata key — default to
                # "" so the chat layer can treat it as "no structural label
                # available" uniformly without branching on presence.
                anchors.append(meta.get("anchor", "") or "")
        return chunks, doc_ids, chunk_ids, scores, anchors
    except Exception as exc:
        log.warning("query failed: %s", exc)
        return [], [], [], [], []


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
    except Exception as exc:
        log.warning("get_chunks failed: %s", exc)
        return []


def update_chunk_scope(doc_id: str, scope_type: str, scope_id: Optional[str]) -> None:
    """Update scope metadata on all ChromaDB chunks for a document."""
    col = get_collection()
    try:
        results = col.get(where={"doc_id": doc_id}, include=["metadatas"])
    except Exception as exc:
        log.warning("update_chunk_metadata failed: %s", exc)
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


def get_chunks_by_ids(chunk_ids: list[str]) -> dict[str, tuple[str, str]]:
    """
    Fetch chunk texts (and structural anchors) from ChromaDB by their IDs.

    Used to retrieve graph-context chunks (found via graph traversal) so
    they can be injected into the prompt alongside the semantically-matched
    chunks. Anchors are returned alongside so the chat layer can cite
    "§4.3 Architectural constraints" instead of quoting the first 80
    characters of the chunk.

    :param chunk_ids: List of chunk IDs to retrieve.
    :type chunk_ids: list[str]
    :return: Mapping of ``{chunk_id: (text, anchor)}`` for found chunks.
             ``anchor`` is ``""`` for pre-#31 chunks and for fixed-window
             fallback chunks.
    :rtype: dict[str, tuple[str, str]]
    """
    if not chunk_ids:
        return {}
    col = get_collection()
    try:
        results = col.get(ids=chunk_ids, include=["documents", "metadatas"])
        out: dict[str, tuple[str, str]] = {}
        for cid, doc, meta in zip(
            results["ids"], results["documents"], results["metadatas"],
        ):
            if doc:
                anchor = (meta or {}).get("anchor", "") or ""
                out[cid] = (doc, anchor)
        return out
    except Exception as exc:
        log.warning("collection_stats failed: %s", exc)
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

    Adds a ``created_at`` timestamp to the metadata. After inserting, evicts
    the oldest entries if the collection exceeds ``MAX_ESCALATION_CACHE_SIZE``.

    :param query_text:    The original query that was sent to the cloud model.
    :param response_text: The cloud model's response.
    """
    import time as _time
    col = _get_cache_collection()
    embedding = await embed(query_text)
    col.add(
        ids=[str(_uuid.uuid4())],
        embeddings=[embedding],
        documents=[query_text],
        metadatas=[{
            "response":      response_text,
            "query_preview": query_text[:200],
            "created_at":    _time.time(),
        }],
    )

    max_size = config.MAX_ESCALATION_CACHE_SIZE
    count = col.count()
    if count > max_size:
        evict_n = count - max_size
        all_entries = col.get(include=["metadatas"])
        ids_with_ts = [
            (id_, meta.get("created_at", 0.0))
            for id_, meta in zip(all_entries["ids"], all_entries["metadatas"])
        ]
        oldest = sorted(ids_with_ts, key=lambda x: x[1])[:evict_n]
        col.delete(ids=[id_ for id_, _ in oldest])
