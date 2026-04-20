"""
test_extractor_batch.py — Pin the durable batch-extraction path.

Bulk concept extraction routes through merLLM's ``/api/batch/submit``
endpoint (hexcaliper#29). Before the migration the extractor called
merLLM's proxy ``/api/chat`` synchronously per chunk; merLLM did not
persist those proxy calls, so a merLLM restart mid-ingest silently
dropped graph edges for every in-flight chunk. These tests pin the new
submit → shared-poll → assemble contract and the fault-tolerance
semantics.

The poll path is shared: one ``POST /api/batch/status-by-ids`` carries
every in-flight job ID per tick, instead of one
``GET /api/batch/results/{id}`` per chunk per tick (#40). merLLM owns
each job's lifetime — the client only gives up on a job if merLLM
forgets it for BATCH_MISS_TOLERANCE consecutive polls (#48).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import extractor


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_submit_response(job_id: str = "job-abc"):
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json.return_value = {"ok": True, "id": job_id}
    return r


def _mock_status_response(jobs: list[dict]):
    """Mirror merLLM's ``POST /api/batch/status-by-ids`` — list of job dicts."""
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json.return_value = jobs
    return r


def _valid_extraction_json() -> str:
    """A parseable extraction response the _parse_response helper accepts."""
    return (
        '{"concepts": ["safety integrity level"], '
        '"entities": ["SIL 2"], '
        '"doc_role": "requirement", '
        '"key_assertion": "The SIF shall achieve SIL 2."}'
    )


def _make_mock_client(fake_post):
    """Build an AsyncClient stand-in. Both submit and status-by-ids are
    POSTs, so tests supply a single ``fake_post`` that routes by URL."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=fake_post)

    async def _no_get(*a, **kw):
        # Any GET would be a regression on the #40 fix (no per-job results
        # polling) or the #48 refactor (status-by-ids is a POST).
        raise AssertionError("extractor should not issue GETs in the batch path")
    mock_client.get = AsyncMock(side_effect=_no_get)
    return mock_client


# ── Submission contract ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_chunks_batch_submits_to_merllm_batch_endpoint(monkeypatch):
    """Every chunk must be POSTed to /api/batch/submit, not /api/chat."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    submit_calls = []

    async def fake_post(url, json=None, timeout=None, **kw):
        if url.endswith("/api/batch/submit"):
            submit_calls.append((url, json))
            return _mock_submit_response(f"job-{len(submit_calls)}")
        if url.endswith("/api/batch/status-by-ids"):
            return _mock_status_response([
                {"id": f"job-{i}", "status": "completed",
                 "result": _valid_extraction_json()}
                for i in range(1, len(submit_calls) + 1)
            ])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        results = await extractor.extract_chunks_batch(
            ["chunk one", "chunk two"], doc_type="theop",
        )

    assert len(results) == 2
    # Both submit calls hit the batch submit endpoint.
    assert all(url.endswith("/api/batch/submit") for url, _ in submit_calls)
    # Source tag is lancellmot so merLLM can attribute queue entries.
    assert all(body["source_app"] == "lancellmot" for _, body in submit_calls)
    # Defensive options are populated so qwen3:* cannot wedge a slot.
    for _, body in submit_calls:
        assert body["options"]["think"] is False
        assert body["options"]["num_predict"] > 0
        assert body["options"]["num_ctx"] >= 8192


@pytest.mark.asyncio
async def test_extract_chunks_batch_flattens_messages_into_single_prompt(monkeypatch):
    """Batch endpoint runs /api/generate which takes a single prompt string —
    the system+user messages used by extract_chunk must be flattened."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    submitted = []

    async def fake_post(url, json=None, **kw):
        if url.endswith("/api/batch/submit"):
            submitted.append(json["prompt"])
            return _mock_submit_response()
        if url.endswith("/api/batch/status-by-ids"):
            return _mock_status_response([
                {"id": "job-abc", "status": "completed",
                 "result": _valid_extraction_json()},
            ])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        await extractor.extract_chunks_batch(["tell me about SIL 2"], doc_type="theop")

    assert len(submitted) == 1
    # Prompt must contain both the system-level instructions (vocabulary,
    # schema) and the user-level chunk text. Check for a durable marker of
    # each: the JSON-schema intro (system) and the chunk text (user).
    prompt = submitted[0]
    assert "JSON schema (all fields required)" in prompt
    assert "tell me about SIL 2" in prompt


# ── Polling contract ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_chunks_batch_polls_until_complete(monkeypatch):
    """A queued/running status must not end the poll; we keep polling until
    the job flips to completed."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    poll_count = {"n": 0}

    async def fake_post(url, json=None, **kw):
        if url.endswith("/api/batch/submit"):
            return _mock_submit_response("job-abc")
        if url.endswith("/api/batch/status-by-ids"):
            poll_count["n"] += 1
            if poll_count["n"] == 1:
                return _mock_status_response([
                    {"id": "job-abc", "status": "queued", "result": None},
                ])
            if poll_count["n"] == 2:
                return _mock_status_response([
                    {"id": "job-abc", "status": "running", "result": None},
                ])
            return _mock_status_response([
                {"id": "job-abc", "status": "completed",
                 "result": _valid_extraction_json()},
            ])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        results = await extractor.extract_chunks_batch(["only chunk"])

    assert len(results) == 1
    assert results[0].concepts == ["safety integrity level"]
    assert poll_count["n"] >= 3  # polled through queued + running before completed


@pytest.mark.asyncio
async def test_extract_chunks_batch_shared_poll_is_one_request_per_tick(monkeypatch):
    """Regardless of concurrency, the poll path must issue exactly one
    ``POST /api/batch/status-by-ids`` per tick — not one per chunk (#40)."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    submit_n = {"n": 0}
    status_calls = []

    async def fake_post(url, json=None, **kw):
        if url.endswith("/api/batch/submit"):
            submit_n["n"] += 1
            return _mock_submit_response(f"job-{submit_n['n']}")
        if url.endswith("/api/batch/status-by-ids"):
            status_calls.append(json.get("ids") if json else None)
            return _mock_status_response([
                {"id": f"job-{i}", "status": "completed",
                 "result": _valid_extraction_json()}
                for i in range(1, 6)
            ])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        results = await extractor.extract_chunks_batch(["a", "b", "c", "d", "e"])

    assert len(results) == 5
    assert all(not r.is_empty() for r in results)
    # Every chunk resolved from a small, bounded number of shared polls —
    # emphatically not one poll per chunk per tick.
    assert len(status_calls) <= 3


@pytest.mark.asyncio
async def test_extract_chunks_batch_poll_sends_all_pending_ids(monkeypatch):
    """Each shared poll must include every still-pending job ID in the
    status-by-ids body — omitting one would silently hang that chunk."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    submit_n = {"n": 0}
    seen_ids_per_poll: list[set[str]] = []

    async def fake_post(url, json=None, **kw):
        if url.endswith("/api/batch/submit"):
            submit_n["n"] += 1
            return _mock_submit_response(f"job-{submit_n['n']}")
        if url.endswith("/api/batch/status-by-ids"):
            seen_ids_per_poll.append(set(json["ids"]))
            return _mock_status_response([
                {"id": f"job-{i}", "status": "completed",
                 "result": _valid_extraction_json()}
                for i in range(1, 4)
            ])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        await extractor.extract_chunks_batch(["a", "b", "c"])

    # The first poll that occurred after all 3 submits landed must have
    # carried every ID.
    expected = {"job-1", "job-2", "job-3"}
    assert any(expected.issubset(ids) for ids in seen_ids_per_poll)


# ── Fault tolerance — every failure mode returns empty, not an exception ────


@pytest.mark.asyncio
async def test_extract_chunks_batch_submit_failure_yields_empty(monkeypatch):
    """A submission failure (merLLM unreachable, 500, etc.) must not crash
    the caller. The slot gets an empty ExtractionResult."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)

    async def fake_post(url, **kw):
        if url.endswith("/api/batch/submit"):
            raise RuntimeError("merLLM unreachable")
        if url.endswith("/api/batch/status-by-ids"):
            return _mock_status_response([])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        results = await extractor.extract_chunks_batch(["chunk"])

    assert len(results) == 1
    assert results[0].is_empty()


@pytest.mark.asyncio
async def test_extract_chunks_batch_merllm_reported_failure_yields_empty(monkeypatch):
    """A job that transitions to 'failed' in merLLM must map to an empty
    ExtractionResult — same contract as the pre-migration sync path."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)

    async def fake_post(url, **kw):
        if url.endswith("/api/batch/submit"):
            return _mock_submit_response("job-abc")
        if url.endswith("/api/batch/status-by-ids"):
            return _mock_status_response([
                {"id": "job-abc", "status": "failed", "result": None,
                 "error": "slot died"},
            ])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        results = await extractor.extract_chunks_batch(["chunk"])

    assert len(results) == 1
    assert results[0].is_empty()


@pytest.mark.asyncio
async def test_extract_chunks_batch_missing_job_gives_up_after_miss_tolerance(
        monkeypatch):
    """If merLLM consistently reports the job as unknown (manual drain, DB
    wipe) the poller gives up after BATCH_MISS_TOLERANCE polls — no hang.
    Unlike the old wall-clock deadline, a queued-but-known job is never
    abandoned."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(extractor, "BATCH_MISS_TOLERANCE", 3)
    poll_count = {"n": 0}

    async def fake_post(url, **kw):
        if url.endswith("/api/batch/submit"):
            return _mock_submit_response("job-missing")
        if url.endswith("/api/batch/status-by-ids"):
            poll_count["n"] += 1
            return _mock_status_response([])  # never returned by merLLM
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        results = await extractor.extract_chunks_batch(["chunk"])

    assert len(results) == 1
    assert results[0].is_empty()
    assert poll_count["n"] >= 3  # took at least BATCH_MISS_TOLERANCE misses


@pytest.mark.asyncio
async def test_extract_chunks_batch_does_not_give_up_while_job_is_queued(
        monkeypatch):
    """A job that stays ``queued`` for many polls must not be abandoned —
    merLLM owns the lifetime. Proves there's no client-side wall-clock
    deadline anymore (the defining bug behind #48)."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(extractor, "BATCH_MISS_TOLERANCE", 3)
    poll_count = {"n": 0}

    async def fake_post(url, **kw):
        if url.endswith("/api/batch/submit"):
            return _mock_submit_response("job-slow")
        if url.endswith("/api/batch/status-by-ids"):
            poll_count["n"] += 1
            # Stay queued for 10 polls (4× miss-tolerance), then complete.
            if poll_count["n"] < 10:
                return _mock_status_response([
                    {"id": "job-slow", "status": "queued", "result": None},
                ])
            return _mock_status_response([
                {"id": "job-slow", "status": "completed",
                 "result": _valid_extraction_json()},
            ])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        results = await extractor.extract_chunks_batch(["chunk"])

    assert len(results) == 1
    assert not results[0].is_empty()
    assert poll_count["n"] >= 10


@pytest.mark.asyncio
async def test_extract_chunks_batch_preserves_slot_order_under_partial_failure(
        monkeypatch):
    """If chunk 2 of 3 fails, chunks 1 and 3 must still land in slots 0 and
    2 — callers rely on results[i] matching chunk_ids[i]."""
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    submit_n = {"n": 0}

    async def fake_post(url, json=None, **kw):
        if url.endswith("/api/batch/submit"):
            submit_n["n"] += 1
            if submit_n["n"] == 2:
                raise RuntimeError("simulated submit failure on chunk 2")
            return _mock_submit_response(f"job-{submit_n['n']}")
        if url.endswith("/api/batch/status-by-ids"):
            return _mock_status_response([
                {"id": "job-1", "status": "completed",
                 "result": _valid_extraction_json()},
                {"id": "job-3", "status": "completed",
                 "result": _valid_extraction_json()},
            ])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        results = await extractor.extract_chunks_batch(["a", "b", "c"])

    assert len(results) == 3
    assert not results[0].is_empty()
    assert results[1].is_empty()           # slot preserved for failed chunk
    assert not results[2].is_empty()


@pytest.mark.asyncio
async def test_extract_chunks_batch_empty_input_is_noop():
    """Zero chunks means zero submissions — don't even open an httpx client."""
    # No patching: if the function tried to call httpx.AsyncClient it would
    # hit the real network. Returning immediately proves the early-out.
    results = await extractor.extract_chunks_batch([])
    assert results == []


# ── Submit concurrency cap (#51) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_chunks_batch_caps_concurrent_submit_posts(monkeypatch):
    """#51: concurrent POSTs to /api/batch/submit must be bounded by
    EXTRACT_SUBMIT_CONCURRENCY. Parity with the embed path
    (rag.py EMBED_CONCURRENCY = 4). Without the cap a single N-chunk
    document fans out N simultaneous connections and can exhaust the
    httpx pool — observed as 700+ simultaneous submits on a 703-chunk
    reindex in 2026-04-19 telemetry.

    The cap must guard **only** the submit call, not the full
    _extract_one_via_batch (which also awaits the poll future). Wrapping
    the whole function would re-introduce the client-side in-flight cap
    removed by #48; merLLM owns a job's lifetime once submitted.
    """
    monkeypatch.setattr(extractor, "BATCH_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(extractor, "EXTRACT_SUBMIT_CONCURRENCY", 3)

    release = asyncio.Event()
    state = {"in_flight": 0, "peak": 0, "submit_n": 0}

    async def fake_post(url, json=None, **kw):
        if url.endswith("/api/batch/submit"):
            state["submit_n"] += 1
            job_n = state["submit_n"]
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            # Hold the POST in-flight until the test releases; lets the
            # peak observation stabilise against the cap, not against
            # scheduling race.
            await release.wait()
            state["in_flight"] -= 1
            return _mock_submit_response(f"job-{job_n}")
        if url.endswith("/api/batch/status-by-ids"):
            return _mock_status_response([
                {"id": f"job-{i}", "status": "completed",
                 "result": _valid_extraction_json()}
                for i in range(1, 21)
            ])
        raise AssertionError(f"unexpected POST {url}")

    mock_client = _make_mock_client(fake_post)

    with patch("extractor.httpx.AsyncClient", return_value=mock_client):
        task = asyncio.create_task(
            extractor.extract_chunks_batch(
                [f"chunk-{i}" for i in range(20)],
            )
        )
        # Yield the loop enough times for every would-be submitter to
        # reach either fake_post (cap window) or the semaphore (held
        # outside it). 50 ticks is comfortably past the ~20-task
        # scheduling wave.
        for _ in range(50):
            await asyncio.sleep(0)

        # The whole point: only EXTRACT_SUBMIT_CONCURRENCY POSTs may be
        # in-flight at once, even though 20 chunks are racing to submit.
        assert state["peak"] == 3, (
            f"expected concurrent submit POSTs capped at 3, got "
            f"{state['peak']} — submit semaphore missing or wrong scope"
        )

        release.set()
        results = await task

    # All 20 must still extract correctly once the cap releases; the
    # semaphore must not drop or mis-order chunks.
    assert len(results) == 20
    assert all(not r.is_empty() for r in results)
