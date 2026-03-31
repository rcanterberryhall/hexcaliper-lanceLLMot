# Hexcaliper GraphRAG — Session Handoff

**Date:** 2026-03-30
**Status:** extractor.py created; graph.py concept wiring IN PROGRESS (interrupted mid-edit)

---

## What Was Completed This Session

### Module split (fully done, syntax-verified)
All 17 Python files pass syntax check. App.py is now a thin bootstrap.

New modules:
- `config.py` — env vars
- `db.py` — SQLite WAL, all tables (conversations, clients, projects, documents, nodes, edges), TinyDB migration
- `models.py` — Pydantic models, DOC_TYPES
- `parser.py` — file parsing (pdf, docx, xlsx, csv, PLC source)
- `ollama.py` — model listing, warming, summarisation, streaming, GPU stats
- `routers/health.py`, `routers/conversations.py`, `routers/documents.py`, `routers/library.py`, `routers/chat.py`
- `rag.py` updated — scope_type/scope_id, ChromaDB $or filter, get_chunks_by_ids
- `graph.py` updated — uses db module, no self-contained SQLite, citation nodes, family hubs, 3-hop traversal

### extractor.py (newly created, complete)
`/home/lobulus/GitHub/hexcaliper/api/extractor.py`

- `CONCEPT_VOCAB` — ~50 seeded functional-safety concepts
- `ExtractionResult` dataclass: `concepts`, `entities`, `doc_role`, `key_assertion`
- `extract_chunk(text, doc_type, model)` — async, calls Ollama `/api/chat`, returns `ExtractionResult`
- `extract_chunks_batch(chunks, doc_type, model)` — sequential, fault-tolerant
- Structured JSON prompt with system + user messages
- `_parse_response()` — handles clean JSON, ```json fences, prose-wrapped JSON
- `EXTRACT_MODEL` env var (defaults to `DEFAULT_MODEL`); `EXTRACT_TIMEOUT` (default 120s)
- `DOC_ROLES` tuple: requirement, definition, finding, test_evidence, obligation, implementation, reference, informative, unknown

### graph.py — partially updated
Two edits applied, third interrupted:

1. ✅ `EDGE_WEIGHTS` — added `"addresses_concept": 0.70` and `"mentions_entity": 0.60`
2. ✅ `_concept_node(concept)` and `_entity_node(entity)` helper functions added after `_topic_node`
3. ❌ **INTERRUPTED** — `index_chunk_concepts()` function not yet written; `get_context()` step 4 (concept traversal) not yet written

---

## What Needs to Be Done Next (in order)

### 1. Finish graph.py — `index_chunk_concepts()` (HIGHEST PRIORITY)

Add after `add_clause_reference()` function (~line 358):

```python
def index_chunk_concepts(
    chunk_id: str,
    concepts: list[str],
    entities: list[str],
    doc_role: str = "unknown",
    key_assertion: str = "",
) -> None:
    """
    Link a chunk to its extracted concept and entity hub nodes.

    Creates concept/entity nodes if they don't exist, then adds
    addresses_concept and mentions_entity edges from the chunk.

    :param chunk_id:      Chunk ID (same as ChromaDB / RAG chunk ID).
    :param concepts:      List of concept strings from extractor.
    :param entities:      List of entity strings from extractor.
    :param doc_role:      Rhetorical role of the chunk (from extractor).
    :param key_assertion: One-sentence summary (stored on chunk node properties).
    """
    cnode = _chunk_node(chunk_id)

    # Update chunk node with extraction metadata.
    node = db.get_node(cnode)
    if node:
        props = node.get("properties") or {}
        props["doc_role"] = doc_role
        props["key_assertion"] = key_assertion[:200]
        db.upsert_node(
            node_id=cnode,
            node_type="chunk",
            label=node.get("label", chunk_id),
            properties=props,
        )

    for concept in concepts:
        cid = _concept_node(concept)
        db.upsert_node(
            node_id=cid,
            node_type="concept",
            label=concept,
            properties={"concept": concept},
        )
        db.upsert_edge(
            src_id=cnode,
            dst_id=cid,
            edge_type="addresses_concept",
            weight=EDGE_WEIGHTS["addresses_concept"],
        )

    for entity in entities:
        eid = _entity_node(entity)
        db.upsert_node(
            node_id=eid,
            node_type="entity",
            label=entity,
            properties={"entity": entity},
        )
        db.upsert_edge(
            src_id=cnode,
            dst_id=eid,
            edge_type="mentions_entity",
            weight=EDGE_WEIGHTS["mentions_entity"],
        )
```

### 2. Update `get_context()` — add concept traversal step

In `get_context()`, after Step 3 (clause references), add Step 4:

```python
    # ── Step 4: Cross-chunk via shared concept nodes ──────────────────────────
    concept_edges = db.get_edges_from(cnode, edge_type="addresses_concept")
    for ce in concept_edges:
        concept_node_id = ce["dst_id"]
        if not concept_node_id.startswith("concept:"):
            continue
        # All chunks that address the same concept.
        peer_edges = db.get_edges_to(concept_node_id, edge_type="addresses_concept")
        for pe in peer_edges:
            peer_chunk_node = pe["src_id"]
            if not peer_chunk_node.startswith("chunk:"):
                continue
            peer_chunk_id = peer_chunk_node[len("chunk:"):]
            if peer_chunk_id == chunk_id:
                continue
            node = db.get_node(peer_chunk_node)
            if not node:
                continue
            props = node.get("properties") or {}
            if not _scope_allowed(props, user_email, scope_types, scope_ids):
                continue
            decay = _recency_decay(props.get("uploaded_at"))
            score = EDGE_WEIGHTS["addresses_concept"] * decay
            if peer_chunk_id not in scored or scored[peer_chunk_id]["context_score"] < score:
                scored[peer_chunk_id] = {
                    **props,
                    "chunk_id":      peer_chunk_id,
                    "label":         node.get("label", ""),
                    "context_score": round(score, 4),
                    "context_edge":  "addresses_concept",
                }
```

Also update `_EDGE_LABELS` dict:
```python
_EDGE_LABELS = {
    "chunk_in_document":   "same document",
    "normative_reference": "normatively referenced standard",
    "clause_reference":    "cited standard",
    "addresses_concept":   "shared safety concept",
    "mentions_entity":     "related entity",
    "topic_shared":        "related topic",
    "related":             "related",
}
```

### 3. Wire extractor into rag.py `ingest()`

In `rag.py`, add import at top:
```python
import extractor
```

In `ingest()`, after the existing graph indexing block (after `graph.parse_and_index_references`), add:

```python
    # ── Concept extraction (LLM) ──────────────────────────────────────────────
    # Extract concepts/entities per chunk and index as graph hub nodes.
    # Failure is non-fatal — each chunk is independently fault-tolerant.
    for i, chunk in enumerate(chunks):
        result = await extractor.extract_chunk(chunk, doc_type="")
        if not result.is_empty():
            graph.index_chunk_concepts(
                chunk_ids[i],
                concepts=result.concepts,
                entities=result.entities,
                doc_role=result.doc_role,
                key_assertion=result.key_assertion,
            )
```

Note: `ingest()` receives no `doc_type` param currently. Consider adding it:
- Function signature: `async def ingest(doc_id, user_email, text, scope_type="global", scope_id=None, title="", uploaded_at=None, doc_type="")`
- Pass `doc_type` through to `extractor.extract_chunk(chunk, doc_type=doc_type)`
- Update callers in `routers/documents.py` to pass `doc_type`

### 4. Pass doc_type through from document upload

In `routers/documents.py`, the upload endpoint already captures `doc_type`. Update the `rag.ingest()` call to include it:
```python
await rag.ingest(..., doc_type=doc_type)
```

### 5. Add concept browser endpoint (optional, after 1-4 are done)

`GET /graph/concepts` — returns all concept nodes with their chunk counts.
In a new `routers/graph.py`:
```python
@router.get("/graph/concepts")
async def list_concepts(user_email: str = Query(...)):
    # db query: SELECT node_id, label, properties FROM nodes
    #           WHERE node_type='concept'
    # Join with edges to count chunks per concept
    ...
```

---

## Architecture Reminder

### Scope hierarchy
- `global` — ISO/IEC standards, apply to all
- `client:{id}` — contracts/requirements for a client
- `project:{id}` — THEOP, FMEA, HA, FAT/SAT, PLC code for one project
- `session:{conv_id}` — temporary uploads in a chat session

### Graph node types
- `document` — one per uploaded file
- `chunk` — one per RAG chunk (`<doc_id>__<index>`)
- `standard` — ISO/IEC standard identifier (hub, shared across users would be wrong — currently per-user due to scope filter)
- `concept` — abstract safety engineering topic (hub)
- `entity` — named entity, SIL/PL values, clause refs (hub)

### Edge types and weights
| Edge | Weight | Meaning |
|------|--------|---------|
| chunk_in_document | 1.00 | chunk belongs to document |
| normative_reference | 0.90 | document cites standard |
| clause_reference | 0.85 | chunk cites standard inline |
| addresses_concept | 0.70 | chunk addresses abstract concept |
| mentions_entity | 0.60 | chunk mentions specific entity |
| topic_shared | 0.50 | legacy keyword match |

### Key files
```
api/
  app.py          — thin FastAPI bootstrap
  config.py       — all env vars
  db.py           — SQLite WAL, all tables + graph nodes/edges
  rag.py          — ChromaDB ingest/search, calls graph + extractor
  graph.py        — graph indexing and context retrieval
  extractor.py    — LLM concept/entity extraction (NEW)
  models.py       — Pydantic models, DOC_TYPES
  parser.py       — file parsing
  ollama.py       — Ollama API helpers
  routers/
    health.py
    conversations.py
    documents.py
    library.py
    chat.py
```

---

## Longer-Term Backlog

1. **Workbench UI** — new frontend tab; project selector, document library, analysis query
2. **Compliance matrix** — requirements × standard clauses × status (covered/gap/conflict)
3. **Gap analysis** — "what's missing between spec X and standard Y?"
4. **PLC-aware chunker** — split IEC 61131-3 at FUNCTION_BLOCK/PROGRAM/FUNCTION boundaries
5. **Re-index endpoint** — `POST /documents/{doc_id}/reindex` without re-uploading
6. **Squire bridge** — project-scoped context injection from hexcaliper-squire correspondence
7. **Concept browser endpoint** — `GET /graph/concepts`

---

## Environment
- Hardware: dual NVIDIA P40 (24 GB × 2 = 48 GB VRAM)
- Target model: 2× 30B Qwen or 1× 70B Qwen via Ollama
- Stack: FastAPI + ChromaDB + SQLite + Ollama (fully self-hosted)
- `EXTRACT_MODEL` env var controls which model does concept extraction
