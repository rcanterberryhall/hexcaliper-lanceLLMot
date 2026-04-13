"""
test_extractor_prompt_cap.py — Bound the extractor's system prompt size.

Guards hexcaliper-lanceLLMot#30: before the cap, the learned concept
vocabulary was serialized unbounded into every extraction prompt. At ~2,800
learned concepts the system prompt was ~70k chars (~17k tokens), overflowing
the configured num_ctx=8192 budget; Ollama silently left-truncated and
qwen3:* with think:false emitted nothing. merLLM's EmptyResponseError guard
reclassified 158 batch jobs as failed.

The cap (MAX_LEARNED_VOCAB) must hold regardless of how much vocab is
accumulated in the DB.
"""
import extractor


def test_build_system_prompt_caps_learned_vocab_at_10k_entries():
    """A pathological 10k-entry learned vocab must not produce an unbounded
    prompt. The serialized prompt must stay under 4×num_ctx chars (a sane
    safety ceiling well below the silent-truncation threshold)."""
    pathological = [f"learned-concept-{i}" for i in range(10_000)]
    prompt = extractor._build_system_prompt(pathological)

    # num_ctx budget is 8192 tokens ≈ 32k chars at the low end. A 4× margin
    # guarantees we never get near silent truncation.
    assert len(prompt) < 4 * 8192, (
        f"system prompt grew to {len(prompt)} chars — the learned-vocab cap "
        f"is not being applied"
    )


def test_build_system_prompt_respects_max_learned_vocab_constant():
    """The number of *learned* entries in the prompt must not exceed
    MAX_LEARNED_VOCAB. Seeded CONCEPT_VOCAB entries are always kept on top
    of the cap — they're the curated baseline."""
    N = extractor.MAX_LEARNED_VOCAB
    # Give it twice the cap, with unique labels that don't collide with the
    # seeded vocabulary so we can count them cleanly.
    learned = [f"zzz-learned-{i:05d}" for i in range(N * 2)]
    prompt = extractor._build_system_prompt(learned)

    kept = sum(1 for line in prompt.splitlines() if line.startswith("  - zzz-learned-"))
    assert kept == N, f"expected exactly {N} learned entries, got {kept}"


def test_build_system_prompt_preserves_seeded_vocab():
    """Seeded CONCEPT_VOCAB must always appear in the prompt — capping the
    learned additions must not drop curated baseline terms."""
    prompt = extractor._build_system_prompt([f"filler-{i}" for i in range(5000)])
    # Spot-check a handful of seeded concepts.
    for seed in ("safety integrity level", "diagnostic coverage", "proof test"):
        assert f"  - {seed}" in prompt, f"seeded concept '{seed}' missing"


def test_build_system_prompt_empty_learned_vocab_works():
    """No learned vocab is a valid case (fresh DB, first ingest)."""
    prompt = extractor._build_system_prompt(None)
    assert "safety integrity level" in prompt
    assert len(prompt) < 10_000  # seeded-only prompt is small
