"""
test_extract_model_runtime.py — Pin what the Analysis selector governs.

``POST /set-analysis-model`` (``/api/set-analysis-model`` through nginx) assigns
``config.ANALYSIS_MODEL`` in the running process, and the in-UI help row
promises the selector drives "document summarisation **and concept extraction**
on upload ... immediately without restart".

Summarisation always honoured that: ``ollama.summarize_document`` resolves
``model or config.ANALYSIS_MODEL`` at call time. Extraction did not.
``config.EXTRACT_MODEL`` was computed once at import as
``_get("EXTRACT_MODEL", "") or ANALYSIS_MODEL``, so ``_extract_model()``
faithfully re-read a value frozen at process start and no runtime assignment
could reach it — switching to a larger model for better graph quality changed
nothing, and switching to a smaller one to speed a bulk ingest did not
(lancellmot#83). The fallback now resolves per call.

These tests pin that, plus the two properties that make resolving late safe:
an explicit ``EXTRACT_MODEL`` remains a hard pin the selector cannot override,
and the model is resolved **once per document** rather than per chunk, so a
switch mid-ingest can change the model between documents but never split one.
"""
import os
import pathlib
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import extractor
import ollama


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
API_DIR   = pathlib.Path(__file__).resolve().parents[1]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _valid_extraction_json() -> str:
    return (
        '{"concepts": ["safety integrity level"], '
        '"entities": ["SIL 2"], '
        '"doc_role": "requirement", '
        '"key_assertion": "The SIF shall achieve SIL 2."}'
    )


def _mock_response(payload):
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json.return_value = payload
    return r


def _batch_client(submit_bodies: list[dict]):
    """An AsyncClient stand-in that records every /api/batch/submit body."""
    async def fake_post(url, json=None, timeout=None, **kw):
        if url.endswith("/api/batch/submit"):
            submit_bodies.append(json)
            return _mock_response({"ok": True, "id": f"job-{len(submit_bodies)}"})
        if url.endswith("/api/batch/status-by-ids"):
            return _mock_response([
                {"id": f"job-{i}", "status": "completed",
                 "result": _valid_extraction_json()}
                for i in range(1, len(submit_bodies) + 1)
            ])
        raise AssertionError(f"unexpected POST {url}")

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__  = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=fake_post)
    return client


# ── Premises: what the endpoint writes, and the half that already worked ──────


def test_set_analysis_model_assigns_the_live_config_value(app_client, monkeypatch):
    """The endpoint's only effect is assigning ``config.ANALYSIS_MODEL``.

    Everything else here rests on that, so it is pinned rather than assumed.
    Monkeypatching the attribute first means pytest restores the real startup
    value at teardown even though the endpoint writes to it directly.
    """
    monkeypatch.setattr(config, "ANALYSIS_MODEL", "startup:32b")

    r = app_client.post("/set-analysis-model", json={"model": "runtime:70b"})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "runtime:70b"}
    assert config.ANALYSIS_MODEL == "runtime:70b"


@pytest.mark.asyncio
async def test_summarisation_resolves_the_analysis_model_at_call_time(monkeypatch):
    """Summarisation is the half of the selector's promise that already held.

    It is the reference behaviour the extraction path is being brought into
    line with, so a regression here would make the help row false again from
    the other direction.
    """
    monkeypatch.setattr(config, "ANALYSIS_MODEL", "startup:32b")

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__  = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=_mock_response(
        {"message": {"content": "a summary"}}
    ))

    config.ANALYSIS_MODEL = "runtime:70b"
    with patch("ollama.httpx.AsyncClient", return_value=client):
        await ollama.summarize_document("some document text")

    _, kwargs = client.post.call_args
    assert kwargs["json"]["model"] == "runtime:70b"


# ── The defect: a runtime switch never reached extraction ────────────────────


def test_extraction_follows_a_runtime_analysis_model_change(monkeypatch):
    """With no explicit pin, ``_extract_model()`` tracks the live value.

    Pre-fix this returned the import-time ``config.EXTRACT_MODEL`` — here the
    empty string — because the ``or ANALYSIS_MODEL`` fallback had already been
    collapsed at import.
    """
    monkeypatch.setattr(config, "EXTRACT_MODEL", "")
    monkeypatch.setattr(config, "ANALYSIS_MODEL", "startup:32b")

    assert extractor._extract_model() == "startup:32b"

    config.ANALYSIS_MODEL = "runtime:70b"   # what POST /set-analysis-model does

    assert extractor._extract_model() == "runtime:70b", (
        "concept extraction must follow a runtime Analysis-model change, which "
        "the in-UI help row promises takes effect without a restart"
    )


def test_the_selector_reaches_extraction_end_to_end(app_client, monkeypatch):
    """The operator-visible path: POST the endpoint, then read the extractor.

    This is the scenario the help row describes, with nothing stubbed between
    the request and the model extraction would actually use.
    """
    monkeypatch.setattr(config, "EXTRACT_MODEL", "")
    monkeypatch.setattr(config, "ANALYSIS_MODEL", "startup:32b")

    r = app_client.post("/set-analysis-model", json={"model": "runtime:70b"})

    assert r.status_code == 200
    assert extractor._extract_model() == "runtime:70b"


@pytest.mark.asyncio
async def test_batch_extraction_submits_the_runtime_model(monkeypatch):
    """The resolved model must reach merLLM, not just ``_extract_model()``.

    ``extract_chunks_batch`` is the only extraction entry point the ingest
    path uses (``rag.index_concepts_for_doc`` and the reindex route), so this
    is where a frozen value actually cost graph quality.
    """
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(config, "EXTRACT_MODEL", "")
    monkeypatch.setattr(config, "ANALYSIS_MODEL", "startup:32b")

    config.ANALYSIS_MODEL = "runtime:70b"
    submit_bodies: list[dict] = []
    with patch("extractor.httpx.AsyncClient", return_value=_batch_client(submit_bodies)):
        results = await extractor.extract_chunks_batch(["chunk one"], doc_type="theop")

    assert len(results) == 1
    assert [b["model"] for b in submit_bodies] == ["runtime:70b"]


def test_config_leaves_the_extraction_fallback_unbound_at_import():
    """``config.EXTRACT_MODEL`` must be empty when the env var is unset.

    This is the root cause, and it is only observable at *import*, so it runs
    in a subprocess: reloading ``config`` in-process would rebind the module
    attributes every other test reads through it.
    """
    env = dict(os.environ)
    env["ANALYSIS_MODEL"] = "startup:32b"
    env.pop("EXTRACT_MODEL", None)

    proc = subprocess.run(
        [sys.executable, "-c",
         "import config; print(repr(config.EXTRACT_MODEL)); "
         "print(repr(config.ANALYSIS_MODEL))"],
        cwd=str(API_DIR), env=env, capture_output=True, text=True,
    )

    assert proc.returncode == 0, proc.stderr
    extract_value, analysis_value = proc.stdout.strip().splitlines()[:2]
    assert analysis_value == "'startup:32b'", "env plumbing for the probe itself"
    assert extract_value == "''", (
        "EXTRACT_MODEL must stay empty so extractor._extract_model() can "
        "resolve the ANALYSIS_MODEL fallback per call; binding it at import "
        "freezes whichever model started the process"
    )


# ── Guards: the two properties that make resolving late safe ─────────────────


def test_an_explicit_extract_model_pins_against_the_selector(monkeypatch):
    """An ``EXTRACT_MODEL`` env pin outranks the selector, before and after.

    This is the behaviour the README documents and the reason the fix is
    ``EXTRACT_MODEL or ANALYSIS_MODEL`` rather than dropping the variable.
    """
    monkeypatch.setattr(config, "EXTRACT_MODEL", "pinned:7b")
    monkeypatch.setattr(config, "ANALYSIS_MODEL", "startup:32b")

    assert extractor._extract_model() == "pinned:7b"

    config.ANALYSIS_MODEL = "runtime:70b"

    assert extractor._extract_model() == "pinned:7b", (
        "an explicit EXTRACT_MODEL is a hard pin — the Analysis selector must "
        "not be able to override it"
    )


@pytest.mark.asyncio
async def test_batch_extraction_resolves_the_model_once_per_document(monkeypatch):
    """One document's chunks all carry one model, however the value moves.

    The invariant that makes per-call resolution safe: ``extract_chunks_batch``
    resolves the model before its per-chunk fan-out, so a selector change
    during an ingest lands between documents and can never leave one
    document's chunks extracted by two different models.
    """
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    resolver = MagicMock(side_effect=["first:32b", "second:70b", "third:8b"])
    monkeypatch.setattr(extractor, "_extract_model", resolver)

    seen: list[str] = []

    async def fake_one(client, poller, sem, chunk, system_prompt, doc_type, model):
        seen.append(model)
        return extractor.ExtractionResult()

    monkeypatch.setattr(extractor, "_extract_one_via_batch", fake_one)

    submit_bodies: list[dict] = []
    with patch("extractor.httpx.AsyncClient", return_value=_batch_client(submit_bodies)):
        await extractor.extract_chunks_batch(["a", "b", "c"], doc_type="theop")

    assert resolver.call_count == 1, "the model is resolved once, not per chunk"
    assert seen == ["first:32b"] * 3


def test_help_text_names_the_one_configuration_the_selector_does_not_govern():
    """The in-UI help must keep its extraction claim and name the pin.

    The claim is now true, but only while ``EXTRACT_MODEL`` is unset — so the
    help has to say so, or it is false again for anyone who sets it.
    """
    html = (REPO_ROOT / "web" / "index.html").read_text()

    assert "concept extraction on upload" in html, (
        "the Analysis selector's help row states extraction follows it"
    )
    assert "EXTRACT_MODEL" in html, (
        "the Models help must name EXTRACT_MODEL as the pin that takes "
        "extraction away from the selector"
    )
