"""
test_chunker.py — Pin the structure-aware chunker contract.

Covers hexcaliper-lanceLLMot#31:
  * heading-anchored splits produce per-section chunks with anchor set
  * numbered-clause splits (IEC/ISO style) are detected
  * short sections merge forward (MIN_CHUNK_CHARS)
  * long sections sub-split with part suffixes (MAX_CHUNK_CHARS)
  * structureless text falls back to fixed-window, anchor=None
  * re-chunking the same text produces identical output (deterministic —
    ingest and index_concepts_for_doc rely on this to keep chunk_ids aligned)
"""
import chunker


# ── Heading-style splits ─────────────────────────────────────────────────────


def test_markdown_headings_produce_section_per_heading():
    body_a = "A" * 300
    body_b = "B" * 300
    text = f"""# Scope
{body_a}

## Verification strategy
{body_b}
"""
    result = chunker.chunk_structured(text)
    assert len(result) == 2
    assert result[0].anchor == "# Scope"
    assert result[1].anchor == "## Verification strategy"
    assert all(sc.index == i for i, sc in enumerate(result))


def test_single_heading_still_anchors_the_body():
    """Markdown ``#`` syntax is explicit — even a single heading is a
    valid anchor for the section that follows. (Contrast numbered clauses,
    which require a higher signal-to-noise threshold because the regex
    can false-positive on bibliographies and table captions.)"""
    text = "# Only Heading\n\n" + ("para one. " * 30) + "\n\n" + ("para two. " * 30)
    result = chunker.chunk_structured(text)
    assert any(sc.anchor == "# Only Heading" for sc in result)


# ── Numbered clauses ─────────────────────────────────────────────────────────


def test_numbered_clauses_are_split_and_anchored():
    body = "Some substantive clause body that easily exceeds the min chars envelope. " * 5
    text = (
        "Introduction prose before any clause.\n\n"
        f"4.3.1 Architectural constraints\n{body}\n\n"
        f"4.3.2 Safe failure fraction\n{body}\n\n"
        f"4.3.3 Diagnostic coverage\n{body}\n"
    )
    result = chunker.chunk_structured(text)
    anchors = [sc.anchor for sc in result]
    assert "§4.3.1 Architectural constraints" in anchors
    assert "§4.3.2 Safe failure fraction"      in anchors
    assert "§4.3.3 Diagnostic coverage"        in anchors


def test_two_clause_hits_insufficient_signal():
    """A bibliography-style doc with only two "N.M Something" lines should
    NOT be split as if it were a standards doc."""
    text = (
        "Random preamble.\n\n"
        "1.1 Something\nThe thing happens.\n\n"
        "2.2 Another\nAnother thing.\n"
    )
    result = chunker.chunk_structured(text)
    # Should have dropped into the paragraph strategy, not the clause one,
    # so no "§" anchors appear.
    assert all(not (sc.anchor or "").startswith("§") for sc in result)


# ── Envelope: short sections merge forward ───────────────────────────────────


def test_short_sections_merge_into_successor():
    text = (
        "# Tiny\n"
        "brief.\n\n"
        "## Full section\n"
        + ("body paragraph text. " * 40)
    )
    result = chunker.chunk_structured(text)
    assert len(result) == 1
    # Both bodies end up in the merged chunk.
    assert "brief" in result[0].text
    assert "body paragraph text" in result[0].text


def test_short_trailing_section_merges_back():
    """A short tail with nothing after it should still not be dropped."""
    long_body = "long paragraph body. " * 40
    text = f"# First\n{long_body}\n\n# Tail\nshort"
    result = chunker.chunk_structured(text)
    joined = " ".join(sc.text for sc in result)
    assert "long paragraph body" in joined
    assert "short" in joined


# ── Envelope: long sections sub-split with part suffix ───────────────────────


def test_long_section_subsplits_with_part_suffix():
    huge = "X " * (chunker.MAX_CHUNK_CHARS)  # ~2× MAX
    text = f"# Big section\n\n{huge}"
    result = chunker.chunk_structured(text)
    assert len(result) >= 2
    for sc in result:
        assert "# Big section" in (sc.anchor or "")
        assert "part " in (sc.anchor or "")
        assert len(sc.text) <= chunker.MAX_CHUNK_CHARS


# ── Structureless fallback ──────────────────────────────────────────────────


def test_structureless_text_falls_back_to_fixed_window():
    blob = "nostructureblob " * 500  # no headings, no clauses, single paragraph
    result = chunker.chunk_structured(blob)
    # Fixed-window path → anchor is always None.
    assert all(sc.anchor is None for sc in result)
    # And produces multiple chunks because blob > FALLBACK_CHUNK_SIZE.
    assert len(result) >= 2


def test_empty_input_returns_empty():
    assert chunker.chunk_structured("") == []
    assert chunker.chunk_structured("   \n\n  ") == []


# ── Determinism (critical for chunk_id alignment) ───────────────────────────


def test_chunk_structured_is_deterministic():
    """ingest() and index_concepts_for_doc() re-chunk the same text and
    assume the i-th chunk from one call matches the i-th chunk from the
    other. If this test ever flakes, the concept graph silently
    misaligns with ChromaDB."""
    text = (
        "# Scope\n" + ("scope body text. " * 30) + "\n\n"
        "## Method\n" + ("method body text. " * 30) + "\n\n"
        "## Results\n" + ("results body text. " * 30)
    )
    a = chunker.chunk_structured(text)
    b = chunker.chunk_structured(text)
    assert [(sc.text, sc.anchor, sc.index) for sc in a] == \
           [(sc.text, sc.anchor, sc.index) for sc in b]
