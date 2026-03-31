"""
graph.py — Knowledge graph layer for Hexcaliper.

Designed for document corpora that contain explicit cross-references:
ISO/IEC standards (normative references), contracts (exhibit/clause references),
and technical specifications.  The graph enriches semantic search (ChromaDB)
with deterministic structural links that vector similarity alone will miss.

Primary entry points
────────────────────
  index_document(doc_id, ...)            — register a document node
  index_chunk(chunk_id, doc_id, ...)     — register a chunk, linking to parent doc
  add_normative_reference(src, target)   — add explicit doc→standard reference edge
  parse_and_index_references(text, doc_id) — extract and index all references from text
  get_context(chunk_id, ...)             — retrieve related chunks for prompt injection
  format_context(context_items)          — render context as a prompt string
  delete_document(doc_id)                — remove document and all its chunk nodes/edges

Node types
──────────
  document   — one per uploaded file; keyed by doc_id
  chunk      — one per RAG chunk; keyed by "<doc_id>__<index>"
  standard   — canonical standard identifier, e.g. "ISO 9001:2015"
               shared hub across documents — enables cross-doc traversal

Edge types and base weights
────────────────────────────
  chunk_in_document    chunk  → document   1.00  structural containment (certain)
  normative_reference  document → standard  0.90  explicit cross-reference (certain)
  clause_reference     chunk  → standard   0.85  inline clause citation in chunk text
  topic_shared         chunk  → topic      0.50  shared keyword (approximate)

Scoring
───────
  context_score = edge_weight × recency_decay(chunk_uploaded_at)

  recency_decay(t) = exp(−age_days × ln(2) / HALF_LIFE_DAYS)
    HALF_LIFE_DAYS = 30  →  30-day-old chunk ≈ 0.5, same-day ≈ 1.0
    (Documents go stale more slowly than emails; 30 days is conservative.)

Traversal in get_context(chunk_id)
───────────────────────────────────
  1. Find parent document D via chunk_in_document edges.
  2. Siblings: all other chunks in D → scored at base_weight 1.00.
  3. Cross-document: normative_reference edges from D → standard nodes S →
     normative_reference edges from other documents to same S → their chunks.
     Scored at base_weight 0.90.
  4. Inline references: clause_reference edges from this chunk → standard S →
     same cross-doc traversal as above, scored at 0.85.
  5. Filter by user_email + scope (never leak across users).
  6. Deduplicate, rank, return top max_n.
"""

import json
import math
import re
from datetime import datetime, timezone
from typing import Optional

import db

# ── Configuration ──────────────────────────────────────────────────────────────

HALF_LIFE_DAYS = 30.0

EDGE_WEIGHTS = {
    "chunk_in_document":   1.00,
    "normative_reference": 0.90,
    "clause_reference":    0.85,
    "addresses_concept":   0.70,
    "mentions_entity":     0.60,
    "topic_shared":        0.50,
}

# Regex patterns for standards cross-reference detection.
#
# Matches:
#   ISO 9001:2015   IEC 61508-1:2010   ISO/IEC 27001   IEC 62061
#   ISO13849        IEC61508-1          (no-space variants common in OCR'd PDFs)
#   IEC 61508 – 1   (en-dash with spaces, common in Word-converted documents)
#
# Note: the year edition (:YYYY) is intentionally optional — see _std_node() for
# how years are stripped so that "IEC 61508-1:2010" and "IEC 61508-1" share a node.
_STD_PATTERN = re.compile(
    r"\b(ISO(?:/IEC)?|IEC)\s*[\d]+(?:\s*[-\u2013]\s*\d+)?(?::\d{4})?\b",
    re.IGNORECASE,
)

# Matches inline clause citations: "clause 4.1", "§ 8.3.2", "section 7.2"
_CLAUSE_PATTERN = re.compile(
    r"(?:clause|section|§|see)\s+(\d+(?:\.\d+)+)",
    re.IGNORECASE,
)

# Extracts the base family from a part number.
# "IEC 61508-1" → "IEC 61508"   "ISO 13849-2" → "ISO 13849"   "IEC 62061" → None
_PART_PATTERN = re.compile(r"^((?:ISO(?:/IEC)?\s+|IEC\s+)\d+)-\d+$", re.IGNORECASE)


# ── Node ID helpers ────────────────────────────────────────────────────────────

def _doc_node(doc_id: str) -> str:
    return f"doc:{doc_id}"


def _chunk_node(chunk_id: str) -> str:
    return f"chunk:{chunk_id}"


def _std_node(identifier: str) -> str:
    """
    Canonical node ID for a standard.

    Normalises to uppercase, collapses whitespace, and strips the edition year
    so that ``"IEC 61508-1:2010"`` and ``"IEC 61508-1:2000"`` and
    ``"IEC 61508-1"`` all map to the same node ``"std:IEC 61508-1"``.
    """
    norm = re.sub(r"\s+", " ", identifier).strip().upper()
    # Normalise no-space variants: "IEC61508" → "IEC 61508"
    norm = re.sub(r"^(ISO(?:/IEC)?|IEC)(\d)", r"\1 \2", norm)
    # Normalise en-dash with spaces around part number: "IEC 61508 – 1" → "IEC 61508-1"
    norm = re.sub(r"\s+[–-]\s+(\d)", r"-\1", norm)
    # Strip edition year: "IEC 61508-1:2010" → "IEC 61508-1"
    norm = re.sub(r":\d{4}$", "", norm)
    return f"std:{norm}"


def _family_node(identifier: str) -> str | None:
    """
    Return the family node ID for a part number, or None if not a part.

    ``"IEC 61508-1"`` → ``"std:IEC 61508"``
    ``"ISO 13849-2"`` → ``"std:ISO 13849"``
    ``"IEC 62061"``   → ``None``  (not a multi-part standard citation)
    """
    # Work on the normalised form (year already stripped by _std_node logic)
    norm = re.sub(r"\s+", " ", identifier).strip().upper()
    norm = re.sub(r"^(ISO(?:/IEC)?|IEC)(\d)", r"\1 \2", norm)
    norm = re.sub(r"\s+[–-]\s+(\d)", r"-\1", norm)
    norm = re.sub(r":\d{4}$", "", norm)
    m = _PART_PATTERN.match(norm)
    if m:
        return f"std:{m.group(1).upper()}"
    return None


def _topic_node(keyword: str) -> str:
    return f"topic:{keyword.lower().strip()}"


def _concept_node(concept: str) -> str:
    """Canonical node ID for an abstract concept hub."""
    return f"concept:{concept.lower().strip()}"


def _entity_node(entity: str) -> str:
    """Canonical node ID for a named entity hub."""
    return f"entity:{entity.strip()}"


# ── Scoring ────────────────────────────────────────────────────────────────────

def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _recency_decay(timestamp: str | None) -> float:
    """
    Exponential recency weight in (0, 1].
    Returns 0.1 for missing/unparseable timestamps (old-enough to demote).
    """
    dt = _parse_ts(timestamp)
    if dt is None:
        return 0.1
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    return math.exp(-age_days * math.log(2) / HALF_LIFE_DAYS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Indexing ───────────────────────────────────────────────────────────────────

def index_document(
    doc_id: str,
    user_email: str,
    title: str,
    scope_type: str = "global",
    scope_id: str | None = None,
    uploaded_at: str | None = None,
) -> None:
    """
    Register a document in the graph.

    Safe to call multiple times — all operations are idempotent.

    :param doc_id:      Unique document ID (matches SQLite / ChromaDB).
    :param user_email:  Owner email, used for scope filtering in get_context().
    :param title:       Human-readable document title.
    :param scope_type:  ``"global"``, ``"session"``, ``"project"``, or ``"client"``.
    :param scope_id:    ID qualifying the scope, or None for global.
    :param uploaded_at: ISO timestamp; defaults to now.
    """
    db.upsert_node(
        node_id=_doc_node(doc_id),
        node_type="document",
        label=title[:120],
        properties={
            "doc_id":      doc_id,
            "user_email":  user_email,
            "scope_type":  scope_type,
            "scope_id":    scope_id or "",
            "uploaded_at": uploaded_at or _now_iso(),
            "title":       title,
        },
    )


def index_chunk(
    chunk_id: str,
    doc_id: str,
    user_email: str,
    scope_type: str = "global",
    scope_id: str | None = None,
    uploaded_at: str | None = None,
    label: str = "",
) -> None:
    """
    Register a chunk node and link it to its parent document.

    :param chunk_id:    Chunk ID, e.g. ``"<doc_id>__0"``.
    :param doc_id:      Parent document ID.
    :param user_email:  Owner email (propagated from document).
    :param scope_type:  Visibility scope type (propagated from document).
    :param scope_id:    ID qualifying the scope (propagated from document).
    :param uploaded_at: ISO timestamp; defaults to now.
    :param label:       Optional short label (first ~80 chars of chunk text).
    """
    ts = uploaded_at or _now_iso()
    db.upsert_node(
        node_id=_chunk_node(chunk_id),
        node_type="chunk",
        label=label[:80] if label else chunk_id,
        properties={
            "chunk_id":    chunk_id,
            "doc_id":      doc_id,
            "user_email":  user_email,
            "scope_type":  scope_type,
            "scope_id":    scope_id or "",
            "uploaded_at": ts,
        },
    )
    db.upsert_edge(
        src_id=_chunk_node(chunk_id),
        dst_id=_doc_node(doc_id),
        edge_type="chunk_in_document",
        weight=EDGE_WEIGHTS["chunk_in_document"],
    )


def _ensure_standard_nodes(identifier: str) -> str:
    """
    Upsert a standard node (and its family node if applicable) and return the
    part node ID.

    For a part like ``"IEC 61508-1:2010"`` this creates:
      • ``std:IEC 61508-1``     (part node, year stripped)
      • ``std:IEC 61508``       (family hub node)
      • edge: ``std:IEC 61508-1`` → normative_reference → ``std:IEC 61508``

    This means any document that references *any* part of IEC 61508 is linked
    through the shared family hub, so get_context() can traverse from a doc
    referencing Part 1 to docs referencing Parts 2–7 in a single extra hop.

    :param identifier: Raw standard identifier, e.g. ``"IEC 61508-1:2010"``.
    :return: The part-level node ID (year-stripped).
    """
    # Normalise label (strip year for display consistency too)
    norm_label = re.sub(r"\s+", " ", identifier).strip().upper()
    norm_label = re.sub(r"^(ISO(?:/IEC)?|IEC)(\d)", r"\1 \2", norm_label)
    norm_label = re.sub(r"\s+[–-]\s+(\d)", r"-\1", norm_label)
    norm_label = re.sub(r":\d{4}$", "", norm_label)

    std_node_id = _std_node(identifier)
    db.upsert_node(
        node_id=std_node_id,
        node_type="standard",
        label=norm_label,
        properties={"identifier": norm_label},
    )

    # If this is a part number, also create the family hub and link to it.
    family_id = _family_node(identifier)
    if family_id:
        family_label = family_id[len("std:"):]  # e.g. "IEC 61508"
        db.upsert_node(
            node_id=family_id,
            node_type="standard",
            label=family_label,
            properties={"identifier": family_label, "is_family": True},
        )
        # Part → family edge (same weight class as normative_reference since
        # parts of a standard are normatively interdependent).
        db.upsert_edge(
            src_id=std_node_id,
            dst_id=family_id,
            edge_type="normative_reference",
            weight=EDGE_WEIGHTS["normative_reference"],
        )

    return std_node_id


def add_normative_reference(
    src_doc_id: str,
    target_identifier: str,
    target_doc_id: str | None = None,
) -> None:
    """
    Record that a document explicitly references a standard.

    Creates a ``standard`` hub node for *target_identifier* (e.g.
    ``"IEC 61508-1:2010"``) plus a family hub node when applicable (e.g.
    ``"IEC 61508"``), and adds a ``normative_reference`` edge from the source
    document.  If *target_doc_id* is given, also links the target document to
    the same standard so get_context() can traverse from reference to content.

    :param src_doc_id:         ID of the document that contains the reference.
    :param target_identifier:  Standard number, e.g. ``"IEC 61508-1:2010"``.
    :param target_doc_id:      If the referenced standard is in the collection,
                               its doc_id; otherwise ``None``.
    """
    std_node_id = _ensure_standard_nodes(target_identifier)
    db.upsert_edge(
        src_id=_doc_node(src_doc_id),
        dst_id=std_node_id,
        edge_type="normative_reference",
        weight=EDGE_WEIGHTS["normative_reference"],
    )
    if target_doc_id:
        db.upsert_edge(
            src_id=_doc_node(target_doc_id),
            dst_id=std_node_id,
            edge_type="normative_reference",
            weight=EDGE_WEIGHTS["normative_reference"],
        )


def add_clause_reference(chunk_id: str, standard_identifier: str) -> None:
    """
    Record that a chunk contains an inline citation to a standard/clause.

    :param chunk_id:             The chunk that contains the citation.
    :param standard_identifier:  Cited standard, e.g. ``"IEC 61508-1:2010"``.
    """
    std_node_id = _ensure_standard_nodes(standard_identifier)
    db.upsert_edge(
        src_id=_chunk_node(chunk_id),
        dst_id=std_node_id,
        edge_type="clause_reference",
        weight=EDGE_WEIGHTS["clause_reference"],
    )


# ── Reference parsing ──────────────────────────────────────────────────────────

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
    ``addresses_concept`` and ``mentions_entity`` edges from the chunk.
    Also updates the chunk node with ``doc_role`` and ``key_assertion``
    metadata from the extractor.

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


def parse_and_index_references(text: str, doc_id: str) -> list[str]:
    """
    Scan *text* for ISO/IEC standard citations and index them as graph edges.

    Finds patterns like ``ISO 9001:2015``, ``IEC 61511-1``, ``ISO/IEC 27001``
    and records them as ``normative_reference`` edges from the document.

    This is a deterministic complement to LLM-based extraction: the regex is
    reliable for well-structured standards (which have a "Normative references"
    section) and inline citations in contracts.

    :param text:   Full document text or a section of it.
    :param doc_id: ID of the document being indexed.
    :return:       List of found standard identifiers.
    """
    found = []
    for match in _STD_PATTERN.finditer(text):
        identifier = match.group(0).strip()
        add_normative_reference(doc_id, identifier)
        found.append(identifier)
    # Deduplicate while preserving first-occurrence order.
    seen: set[str] = set()
    unique = []
    for s in found:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def parse_and_index_chunk_references(text: str, chunk_id: str) -> list[str]:
    """
    Scan a chunk's text for inline standard citations and index clause_reference edges.

    :param text:     Chunk text.
    :param chunk_id: ID of the chunk.
    :return:         List of found standard identifiers.
    """
    found = []
    for match in _STD_PATTERN.finditer(text):
        identifier = match.group(0).strip()
        add_clause_reference(chunk_id, identifier)
        found.append(identifier)
    seen: set[str] = set()
    unique = []
    for s in found:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


# ── Context retrieval ──────────────────────────────────────────────────────────

def _scope_allowed(node_props: dict, user_email: str,
                   scope_types: list[str], scope_ids: list) -> bool:
    """
    Scope-guard: returns True only if the chunk is visible to this user/context.
    Mirrors the scope rules in rag.py search().

    :param node_props:  Properties dict from the chunk node.
    :param user_email:  Requesting user email.
    :param scope_types: List of allowed scope type strings.
    :param scope_ids:   Parallel list of allowed scope IDs (None = any for that type).
    """
    if node_props.get("user_email") != user_email:
        return False
    chunk_st = node_props.get("scope_type", "global")
    chunk_si = node_props.get("scope_id", "")
    for st, si in zip(scope_types, scope_ids):
        if chunk_st == st:
            if si is None or si == "" or chunk_si == si:
                return True
    return False


def _collect_chunks_from_document(
    doc_node_id: str,
    exclude_chunk_id: str,
    base_weight: float,
    user_email: str,
    scope_types: list[str],
    scope_ids: list,
    scored: dict,
) -> None:
    """
    Find all chunks belonging to doc_node_id and add them to *scored* dict.

    Only adds a chunk if it scores higher than any existing entry for that
    chunk (keeps the best path).
    """
    chunk_edges = db.get_edges_to(doc_node_id, edge_type="chunk_in_document")
    for e in chunk_edges:
        cnode_id = e["src_id"]
        if not cnode_id.startswith("chunk:"):
            continue
        chunk_id = cnode_id[len("chunk:"):]
        if chunk_id == exclude_chunk_id:
            continue
        node = db.get_node(cnode_id)
        if not node:
            continue
        props = node.get("properties") or {}
        if not _scope_allowed(props, user_email, scope_types, scope_ids):
            continue
        decay = _recency_decay(props.get("uploaded_at"))
        score = base_weight * decay
        if chunk_id not in scored or scored[chunk_id]["context_score"] < score:
            scored[chunk_id] = {
                **props,
                "chunk_id":      chunk_id,
                "label":         node.get("label", ""),
                "context_score": round(score, 4),
                "context_edge":  _edge_type_for_weight(base_weight),
            }


def _edge_type_for_weight(weight: float) -> str:
    """Map a base weight back to its edge label for context formatting."""
    for k, v in EDGE_WEIGHTS.items():
        if abs(v - weight) < 0.01:
            return k
    return "related"


def get_context(
    chunk_id: str,
    user_email: str,
    scope_types: list[str] = None,
    scope_ids: list = None,
    max_n: int = 5,
) -> list[dict]:
    """
    Retrieve the most relevant chunks from the graph for a given chunk.

    Traversal order (highest signal first):
      1. Sibling chunks in the same document (chunk_in_document, weight 1.00)
      2. Chunks in documents that share a normative_reference hub (weight 0.90)
      3. Chunks in documents referenced by an inline clause_reference (weight 0.85)

    Results are filtered by user_email + scope and ranked by
    ``base_weight × recency_decay``.

    :param chunk_id:    The query chunk (same ID space as ChromaDB chunk IDs).
    :param user_email:  Requesting user — enforces ownership isolation.
    :param scope_types: List of allowed scope type strings.  Defaults to
                        ``["global"]``.
    :param scope_ids:   Parallel list of allowed scope IDs.  Defaults to
                        ``[None]``.
    :param max_n:       Maximum context chunks to return.
    :return:            List of chunk property dicts with added
                        ``"context_score"`` and ``"context_edge"`` keys,
                        sorted by descending score.
    """
    if scope_types is None:
        scope_types = ["global"]
        scope_ids   = [None]
    if scope_ids is None:
        scope_ids = [None] * len(scope_types)

    cnode = _chunk_node(chunk_id)
    scored: dict[str, dict] = {}

    # ── Step 1: Find parent document ──────────────────────────────────────────
    parent_edges = db.get_edges_from(cnode, edge_type="chunk_in_document")
    parent_doc_nodes = [e["dst_id"] for e in parent_edges if e["dst_id"].startswith("doc:")]

    for doc_node_id in parent_doc_nodes:
        # Sibling chunks in same document.
        _collect_chunks_from_document(
            doc_node_id, chunk_id, EDGE_WEIGHTS["chunk_in_document"],
            user_email, scope_types, scope_ids, scored,
        )

        # ── Step 2: Cross-document via normative references ───────────────────
        # Find standards referenced by this document.
        norm_edges = db.get_edges_from(doc_node_id, edge_type="normative_reference")
        for ne in norm_edges:
            std_node_id = ne["dst_id"]
            if not std_node_id.startswith("std:"):
                continue

            # 2a. Direct siblings: other docs referencing the exact same standard.
            back_edges = db.get_edges_to(std_node_id, edge_type="normative_reference")
            for be in back_edges:
                other_doc = be["src_id"]
                if other_doc == doc_node_id or not other_doc.startswith("doc:"):
                    continue
                _collect_chunks_from_document(
                    other_doc, chunk_id, EDGE_WEIGHTS["normative_reference"],
                    user_email, scope_types, scope_ids, scored,
                )

            # 2b. Family traversal: if this standard is a part (e.g. IEC 61508-1),
            # also traverse via the family hub (IEC 61508) to find:
            #   • docs referencing the whole family ("compliant with IEC 61508")
            #   • docs referencing sibling parts (IEC 61508-2 … IEC 61508-7)
            family_edges = db.get_edges_from(std_node_id, edge_type="normative_reference")
            for fe in family_edges:
                family_id = fe["dst_id"]
                if not family_id.startswith("std:"):
                    continue
                # Docs that directly reference the family node.
                family_back = db.get_edges_to(family_id, edge_type="normative_reference")
                for fbe in family_back:
                    src = fbe["src_id"]
                    if src == doc_node_id:
                        continue
                    if src.startswith("doc:"):
                        # Contract/spec referencing the whole family standard.
                        _collect_chunks_from_document(
                            src, chunk_id, EDGE_WEIGHTS["normative_reference"],
                            user_email, scope_types, scope_ids, scored,
                        )
                    elif src.startswith("std:") and src != std_node_id:
                        # Sibling part (e.g. IEC 61508-2 when we started from IEC 61508-1).
                        sibling_back = db.get_edges_to(src, edge_type="normative_reference")
                        for sb in sibling_back:
                            if sb["src_id"].startswith("doc:") and sb["src_id"] != doc_node_id:
                                _collect_chunks_from_document(
                                    sb["src_id"], chunk_id, EDGE_WEIGHTS["normative_reference"],
                                    user_email, scope_types, scope_ids, scored,
                                )

    # ── Step 3: Cross-document via inline clause references ───────────────────
    clause_edges = db.get_edges_from(cnode, edge_type="clause_reference")
    for ce in clause_edges:
        std_node_id = ce["dst_id"]
        if not std_node_id.startswith("std:"):
            continue
        back_edges = db.get_edges_to(std_node_id, edge_type="normative_reference")
        for be in back_edges:
            other_doc = be["src_id"]
            if not other_doc.startswith("doc:"):
                continue
            _collect_chunks_from_document(
                other_doc, chunk_id, EDGE_WEIGHTS["clause_reference"],
                user_email, scope_types, scope_ids, scored,
            )

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

    ranked = sorted(scored.values(), key=lambda x: x["context_score"], reverse=True)
    return ranked[:max_n]


# ── Prompt formatting ──────────────────────────────────────────────────────────

_EDGE_LABELS = {
    "chunk_in_document":   "same document",
    "normative_reference": "normatively referenced standard",
    "clause_reference":    "cited standard",
    "addresses_concept":   "shared safety concept",
    "mentions_entity":     "related entity",
    "topic_shared":        "related topic",
    "related":             "related",
}


def format_context(context_items: list[dict], doc_titles: dict[str, str] | None = None) -> str:
    """
    Render graph context items as a prompt section.

    :param context_items: Output of ``get_context()``.
    :param doc_titles:    Optional ``{doc_id: title}`` mapping for richer labels.
    :return:              Multi-line prompt string, or empty string if no context.
    """
    if not context_items:
        return ""

    lines = ["Relevant content from related documents (graph context):"]
    for c in context_items:
        edge_label = _EDGE_LABELS.get(c.get("context_edge", ""), "related")
        doc_id = c.get("doc_id", "")
        title = (doc_titles or {}).get(doc_id) or c.get("title", doc_id)
        chunk_label = c.get("label", "")[:80]
        score = c.get("context_score", 0.0)
        ts = (c.get("uploaded_at") or "")[:10]

        lines.append(
            f"  [{edge_label}] {title!r} — {chunk_label}"
            f"  (relevance: {score:.2f}, uploaded: {ts})"
        )

    return "\n".join(lines)


# ── Deletion ───────────────────────────────────────────────────────────────────

def delete_document(doc_id: str) -> None:
    """
    Remove a document node, all its chunk nodes, and all associated edges.

    Delegates to ``db.delete_graph_for_document()`` which owns the SQLite
    connection and locking.

    :param doc_id: Document ID to remove.
    """
    db.delete_graph_for_document(doc_id)
