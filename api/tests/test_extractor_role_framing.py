"""
test_extractor_role_framing.py — Pin the extractor's role framing and vocab.

Guards hexcaliper-lanceLLMot#50: the original framing cast the model as a
"functional-safety document analyst" with a ~50-term vocabulary that was
~96% FS-standards-heavy (IEC 62061/61508/13849, FSM). In practice the
document library mixes pure controls-engineering material (PLC programs,
HMI screens, VFD parameters, P&IDs, sequences of operations) with FS
material and hybrid docs. A FS-only framing suppressed recall on controls
chunks and pushed the model to interpret everything through a SIL/PL lens.

These tests lock in:
  - role framing: controls-engineering primary, FS as a named specialty
  - vocab coverage: controls-engineering anchors alongside the FS seeds,
    so the model has canonical terms to reuse on non-FS chunks (otherwise
    broadening the role alone just produces noisy ad-hoc concepts)
  - no regression: the FS seeds that were already in place are still here
"""
import extractor


def test_system_prompt_frames_role_as_controls_engineer():
    """The role line must lead with controls engineering; FS is a specialty,
    not the whole identity."""
    prompt = extractor._build_system_prompt(None)
    assert "controls-engineering document analyst" in prompt, (
        "role framing must lead with controls engineering"
    )
    assert "functional safety" in prompt.lower() or "functional-safety" in prompt.lower(), (
        "FS depth must still be named in the role line"
    )
    assert "You are a functional-safety document analyst." not in prompt, (
        "old FS-only framing must be gone"
    )


def test_concept_vocab_includes_controls_engineering_anchors():
    """Controls-engineering chunks need canonical terms to reuse. Without
    these anchors, broadening the role just produces noisy ad-hoc concepts."""
    expected_controls_anchors = {
        # Logic / programming
        "ladder logic",
        "function block",
        "structured text",
        "tag name",
        # Operational modes
        "interlock",
        "permissive",
        "bypass",
        # HMI / SCADA
        "hmi screen",
        "alarm setpoint",
        # Instrumentation
        "pid loop",
        "analog scaling",
        # Motors / drives
        "vfd drive",
        "motor starter",
        "drive parameter",
        # Engineering docs
        "p&id",
        "loop diagram",
        "sequence of operations",
    }
    vocab = set(extractor.CONCEPT_VOCAB)
    missing = expected_controls_anchors - vocab
    assert not missing, f"controls-engineering anchors missing from CONCEPT_VOCAB: {sorted(missing)}"


def test_concept_vocab_retains_functional_safety_seeds():
    """Regression guard: broadening the role must not drop FS seeds. These
    terms anchor the FS subset of the corpus and must remain."""
    expected_fs_seeds = {
        "safety integrity level",
        "performance level",
        "diagnostic coverage",
        "proof test",
        "hazard analysis",
        "safety instrumented function",
    }
    vocab = set(extractor.CONCEPT_VOCAB)
    missing = expected_fs_seeds - vocab
    assert not missing, f"FS seeds dropped from CONCEPT_VOCAB: {sorted(missing)}"


def test_concept_vocab_entries_are_lowercase_and_clean():
    """CONCEPT_VOCAB entries become node IDs directly (see the module-level
    comment at extractor.py:101). Must be lowercase with no leading/trailing
    whitespace. Added terms need to honor this invariant."""
    for term in extractor.CONCEPT_VOCAB:
        assert term == term.lower(), f"non-lowercase vocab entry: {term!r}"
        assert term == term.strip(), f"whitespace around vocab entry: {term!r}"
        assert term, "empty string in CONCEPT_VOCAB"
