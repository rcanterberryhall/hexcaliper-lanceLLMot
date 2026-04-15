"""
chunker.py — Structure-aware document chunking.

Splits a document into chunks that follow the source's natural outline
(markdown headings, numbered clauses, paragraph breaks) before falling back
to fixed-size windows. Each chunk carries an optional ``anchor`` string
(e.g. ``"§4.3 Architectural constraints"``) that the embedding/graph
layers can surface as a human-readable citation.

This is the scaffolding phase of hexcaliper-lanceLLMot#31. The structural
detectors here are intentionally simple and heuristic — they operate on the
plaintext ``parser.parse_file`` already produces, so no change is needed in
the PDF/docx parsers. A follow-up can teach ``parser.py`` to emit richer
structure (PDF outline entries, docx heading runs) and feed it in via the
``source_hint`` parameter without changing this module's public contract.

Design goals
────────────
* **Deterministic** — same input text always produces the same chunk
  sequence. ``rag.ingest`` and ``rag.index_concepts_for_doc`` both call the
  chunker on the stored document text; if the output drifted between calls
  the chunk IDs would stop aligning and the concept graph would point at
  nothing. Tests pin this.
* **Envelope-bounded** — no chunk is smaller than ``MIN_CHUNK_CHARS`` (we
  merge forward) and none is larger than ``MAX_CHUNK_CHARS`` (we sub-split
  on paragraph boundaries, then on fixed windows as a last resort).
* **Always produces something** — even on structureless blobs the fallback
  yields the same output shape as the pre-existing fixed-window chunker so
  callers never have to special-case "no structure detected".

Public contract
───────────────
    result: list[StructuralChunk] = chunk_structured(text, source_hint=None)

``source_hint`` is reserved for future use (e.g. ``"pdf-outline"``) — for
now it's accepted and ignored so call sites can be wired today.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ── Envelope ──────────────────────────────────────────────────────────────────
#
# The min/max define a "comfortable" chunk size for the downstream extractor
# and embedder. They are soft limits — a section shorter than MIN_CHUNK_CHARS
# is merged into its successor, a section longer than MAX_CHUNK_CHARS is
# sub-split. MAX is bounded by the embedder, not the extractor: nomic-embed-text
# has a 2048-token architectural cap (BERT WordPiece, dense technical text
# tokenizes ~1 token per 1.2 chars), so 1500 chars leaves comfortable headroom.
MIN_CHUNK_CHARS = 200
MAX_CHUNK_CHARS = 1500

# Fixed-window fallback — intentionally matches the legacy rag.chunk_text
# behavior so structureless inputs stay bit-compatible with pre-#31 output.
FALLBACK_CHUNK_SIZE    = 1000
FALLBACK_CHUNK_OVERLAP = 150


# ── Result type ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StructuralChunk:
    """
    One unit of chunked document text.

    :ivar text:   The chunk contents (always non-empty, stripped).
    :ivar anchor: Human-readable citation label for the chunk, e.g.
                  ``"§4.3 Architectural constraints"`` or
                  ``"## Verification strategy"``. ``None`` when no structure
                  could be detected (the fallback fixed-window path).
    :ivar index:  Zero-based position in the chunk sequence. Used by callers
                  that still key ChromaDB / graph nodes on ``{doc_id}__{i}``.
    """
    text:   str
    anchor: Optional[str]
    index:  int


# ── Public entry point ────────────────────────────────────────────────────────


def chunk_structured(
    text: str,
    source_hint: Optional[str] = None,  # reserved for parser-supplied hints
) -> list[StructuralChunk]:
    """
    Split *text* into structure-aware chunks.

    Strategy, in order of preference:
      1. Markdown-style heading splits (``#``, ``##``, …).
      2. Numbered-clause splits (``4.3.1 Title…``), common in IEC/ISO docs.
      3. Paragraph splits (blank-line boundaries), when section markers
         aren't dense enough to carry the doc on their own.
      4. Fixed-window fallback — byte-compatible with ``rag.chunk_text`` so
         a structureless blob (scraped HTML, OCR, CSV dumps) still lands in
         the pipeline.

    Regardless of which strategy fires, the result is passed through
    :func:`_enforce_envelope` so min/max constraints hold.

    :param text: The full document text.
    :param source_hint: Reserved. The ``parser`` layer is expected to pass
                        a hint in a future change (e.g. ``"pdf-outline"``,
                        ``"docx-styles"``) so structural cues that don't
                        survive flattening to plaintext can be threaded
                        through. Today we ignore it and work entirely from
                        the text.
    :return: Ordered list of :class:`StructuralChunk`.
    """
    _ = source_hint  # not yet used; accept it for forward compatibility.

    stripped = text.strip()
    if not stripped:
        return []

    sections = _split_by_headings(stripped)
    if sections is None:
        sections = _split_by_numbered_clauses(stripped)
    if sections is None:
        sections = _split_by_paragraphs(stripped)

    if sections is None:
        # No structure at all — fall back to the legacy fixed-window chunker.
        pieces = _fixed_window(stripped)
        return [StructuralChunk(text=p, anchor=None, index=i)
                for i, p in enumerate(pieces)]

    return _finalize(sections)


# ── Strategy 1: markdown-style headings ───────────────────────────────────────


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _split_by_headings(text: str) -> Optional[list[tuple[str, str]]]:
    """
    Split on ``# … ###### …`` lines.

    Markdown ``#`` syntax is explicit enough that a single heading still
    gives us a valid anchor — the section's body is whatever follows.
    Contrast with the clause regex below, which can false-positive on
    bibliography lines and therefore requires multiple hits.

    :return: List of ``(anchor, body)`` pairs, or ``None`` if no headings
             were found at all.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return None

    sections: list[tuple[str, str]] = []

    # Any prose before the first heading becomes an anchorless "preamble".
    first_start = matches[0].start()
    if first_start > 0:
        preamble = text[:first_start].strip()
        if preamble:
            sections.append(("", preamble))

    for i, m in enumerate(matches):
        level, title = m.group(1), m.group(2).strip()
        body_start = m.end()
        body_end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body       = text[body_start:body_end].strip()
        if not body:
            # Heading with no body — likely a section index or a typo.
            # Skip rather than emit an empty-text chunk.
            continue
        anchor = f"{level} {title}"
        sections.append((anchor, body))

    return sections or None


# ── Strategy 2: numbered clauses (IEC/ISO style) ──────────────────────────────


# Matches lines that *start* with a dotted number followed by a title.
# Examples: "4.3.1 Architectural constraints", "7  Safety lifecycle"
# Rejects: "Figure 4.3.1", "see 4.3.1 below" (anything with prefix text).
_CLAUSE_RE = re.compile(
    r"^(?P<num>\d+(?:\.\d+){0,5})\s+(?P<title>[A-Z][^\n]{2,120})$",
    re.MULTILINE,
)


def _split_by_numbered_clauses(text: str) -> Optional[list[tuple[str, str]]]:
    """
    Split on lines like ``4.3.1 Architectural constraints``.

    A minimum of three clause hits is required — two or fewer is often a
    false positive (a reference in a bibliography, a table caption).
    """
    matches = list(_CLAUSE_RE.finditer(text))
    if len(matches) < 3:
        return None

    sections: list[tuple[str, str]] = []
    first_start = matches[0].start()
    if first_start > 0:
        preamble = text[:first_start].strip()
        if preamble:
            sections.append(("", preamble))

    for i, m in enumerate(matches):
        num   = m.group("num")
        title = m.group("title").strip()
        body_start = m.end()
        body_end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body       = text[body_start:body_end].strip()
        if not body:
            continue
        anchor = f"§{num} {title}"
        sections.append((anchor, body))

    return sections or None


# ── Strategy 3: paragraph splits ──────────────────────────────────────────────


def _split_by_paragraphs(text: str) -> Optional[list[tuple[str, str]]]:
    """
    Split on blank-line paragraph boundaries.

    Returns ``None`` if the result would be a single paragraph — at that
    point there's no structural split to be had and the fixed-window
    fallback is the honest answer.
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) < 2:
        return None
    return [("", p) for p in paras]


# ── Envelope enforcement and finalization ─────────────────────────────────────


def _finalize(sections: list[tuple[str, str]]) -> list[StructuralChunk]:
    """
    Apply min/max envelope rules then assign indexes.

    Merge pass walks forward, concatenating a section into its successor
    when the section (plus accumulated short tail) is below MIN_CHUNK_CHARS.
    Sub-split pass then breaks any section exceeding MAX_CHUNK_CHARS into
    paragraph-then-fixed-window pieces, re-using the parent anchor with a
    ``(part N/M)`` suffix so citations remain readable.
    """
    merged = _merge_short(sections)
    out: list[StructuralChunk] = []
    idx = 0
    for anchor, body in merged:
        pieces = _split_long(anchor, body)
        for piece_anchor, piece_body in pieces:
            out.append(StructuralChunk(
                text=piece_body,
                anchor=piece_anchor or None,
                index=idx,
            ))
            idx += 1
    return out


def _merge_short(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """
    Merge a short section into the next one, carrying the earlier anchor
    forward when the next section has no anchor of its own (e.g. paragraph
    splits) and keeping the later anchor otherwise (heading-followed-by-body
    is the dominant case — the heading body is what we want to cite).
    """
    if not sections:
        return []
    result: list[tuple[str, str]] = []
    buffer_anchor = ""
    buffer_body   = ""
    for anchor, body in sections:
        combined_body = f"{buffer_body}\n\n{body}".strip() if buffer_body else body
        combined_anchor = anchor or buffer_anchor
        if len(combined_body) < MIN_CHUNK_CHARS:
            buffer_anchor = combined_anchor
            buffer_body   = combined_body
            continue
        result.append((combined_anchor, combined_body))
        buffer_anchor = ""
        buffer_body   = ""
    if buffer_body:
        # Tail shorter than MIN_CHUNK_CHARS with no successor to merge into —
        # emit it as-is rather than drop content.
        if result:
            last_anchor, last_body = result[-1]
            result[-1] = (last_anchor, f"{last_body}\n\n{buffer_body}")
        else:
            result.append((buffer_anchor, buffer_body))
    return result


def _split_long(
    anchor: str,
    body: str,
) -> list[tuple[str, str]]:
    """
    Break *body* down to ``MAX_CHUNK_CHARS``-sized pieces while preserving
    *anchor* (with a part suffix when multiple pieces are produced).
    """
    if len(body) <= MAX_CHUNK_CHARS:
        return [(anchor, body)]

    # Try paragraph splits first.
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    pieces: list[str] = []
    buf = ""
    for p in paras:
        candidate = f"{buf}\n\n{p}".strip() if buf else p
        if len(candidate) > MAX_CHUNK_CHARS and buf:
            pieces.append(buf)
            buf = p
        else:
            buf = candidate
    if buf:
        pieces.append(buf)

    # Anything still too long gets fixed-windowed (paragraph was a wall of
    # text, e.g. OCR'd scan).
    expanded: list[str] = []
    for p in pieces:
        if len(p) <= MAX_CHUNK_CHARS:
            expanded.append(p)
        else:
            expanded.extend(_fixed_window(p, size=MAX_CHUNK_CHARS,
                                          overlap=FALLBACK_CHUNK_OVERLAP))

    if len(expanded) == 1:
        return [(anchor, expanded[0])]

    total = len(expanded)
    return [
        (f"{anchor} (part {i+1}/{total})" if anchor else "", piece)
        for i, piece in enumerate(expanded)
    ]


def _fixed_window(
    text: str,
    size: int = FALLBACK_CHUNK_SIZE,
    overlap: int = FALLBACK_CHUNK_OVERLAP,
) -> list[str]:
    """
    Legacy fixed-window chunker — intentionally mirrors ``rag.chunk_text`` so
    structureless inputs stay byte-compatible with pre-#31 behavior.
    """
    chunks: list[str] = []
    start = 0
    step = max(1, size - overlap)
    while start < len(text):
        piece = text[start:start + size].strip()
        if piece:
            chunks.append(piece)
        start += step
    return chunks
