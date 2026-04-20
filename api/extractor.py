"""
extractor.py — LLM-based concept and entity extraction for GraphRAG.

Each document chunk is analysed by a local Ollama model to produce:
  • concepts   — 3-7 abstract functional-safety or engineering topics addressed
                 by the chunk (drawn from a seeded vocabulary, plus open terms)
  • entities   — specific named things: SIL/PL levels, standard clause refs,
                 component names, quantitative parameters
  • doc_role   — the rhetorical function of the chunk (requirement, finding, …)
  • key_assertion — one-sentence summary of the chunk's main claim

The outputs become *concept* and *entity* hub nodes in the knowledge graph,
connected to chunk nodes via ``addresses_concept`` and ``mentions_entity``
edges.  This is the primary intelligence layer: chunks that address the same
concept (e.g. "diagnostic coverage") are linked regardless of whether they
share explicit citation numbers.

Usage
─────
    result = await extract_chunk(text, doc_type="theop")
    # result is an ExtractionResult with .concepts, .entities, .doc_role, .key_assertion

Configuration
─────────────
    EXTRACT_MODEL  — Ollama model for extraction (defaults to DEFAULT_MODEL).
                     A 30B+ model is recommended for consistent JSON output.
    EXTRACT_TIMEOUT — HTTP timeout for extraction calls (default: 120 s).
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import httpx

import config

logger = logging.getLogger(__name__)

# ── Model config ──────────────────────────────────────────────────────────────

# Extraction quality scales strongly with model size — 30B+ recommended.
# EXTRACT_MODEL env var takes priority; otherwise uses ANALYSIS_MODEL (settable at runtime).
EXTRACT_TIMEOUT = float(config._get("EXTRACT_TIMEOUT_SECONDS", "120"))

# ── Batch submission config ───────────────────────────────────────────────────
# Bulk extraction routes through merLLM's SQLite-backed batch queue instead of
# the in-memory proxy path (hexcaliper#29). merLLM re-enqueues orphaned jobs on
# restart, so a mid-ingest power outage or code-change redeploy no longer
# silently drops graph edges for half a document.
BATCH_SUBMIT_TIMEOUT = float(config._get("EXTRACT_BATCH_SUBMIT_TIMEOUT", "15"))
BATCH_POLL_INTERVAL  = float(config._get("EXTRACT_BATCH_POLL_INTERVAL", "3"))

# Cap on concurrent POSTs to merLLM's /api/batch/submit from a single
# extract_chunks_batch call. Parity with rag.py's EMBED_CONCURRENCY=4,
# but deliberately scoped to the submit POST only — *not* to the whole
# _extract_one_via_batch — because wrapping the full function would cap
# total in-flight jobs and re-introduce the client-side deadline removed
# in #48. merLLM owns a job's lifetime once submitted; this cap exists
# purely to keep the httpx connection pool and event-loop pressure
# bounded on large-doc ingests (a 703-chunk doc used to fan out 703
# simultaneous POSTs — see lancellmot#51). 8 is higher than the embed
# cap because submit is a fast round-trip that doesn't wait on GPU.
EXTRACT_SUBMIT_CONCURRENCY = int(config._get("EXTRACT_SUBMIT_CONCURRENCY", "8"))

# merLLM owns the lifetime of every accepted batch job — its slot-watchdog
# (_sweep_busy_timeouts) already reclaims wedged GPU slots, so the client
# has no business timing out a job that's merely waiting in queue. The only
# legitimate "give up" signal is that merLLM no longer recognises the job
# ID, meaning the row was manually drained / DB wiped / lost to a
# downgrade. We detect that by asking for status-by-ids and counting
# consecutive misses per job. /api/batch/submit writes the DB row before
# returning the ID, so a miss on the very next poll is already suspicious;
# 5 gives generous headroom against any transient DB-read anomaly.
BATCH_MISS_TOLERANCE = int(config._get("EXTRACT_BATCH_MISS_TOLERANCE", "5"))

# Cap on *learned* vocab entries fed into the system prompt on top of the
# seeded CONCEPT_VOCAB. The learned set grows unbounded as documents are
# ingested; without a cap the serialized vocab block eventually exceeds
# num_ctx, gets silently left-truncated by Ollama, and qwen3 emits nothing
# (hexcaliper-lanceLLMot#30). 200 keeps the full prompt well under 8k tokens
# even with the seeded vocab and chunk text, while retaining enough learned
# terms to steer concept reuse. Callers should pair this with the ranked
# `limit` on db.list_concept_vocab so the 200 retained are the most
# frequently used in the active scope.
MAX_LEARNED_VOCAB = int(config._get("EXTRACT_MAX_LEARNED_VOCAB", "200"))


def _extract_model() -> str:
    return config.EXTRACT_MODEL


# ── Seeded concept vocabulary ─────────────────────────────────────────────────
#
# These are the canonical concept labels used as hub node IDs in the graph.
# The extraction prompt instructs the model to prefer these terms when they fit,
# and to add new terms only when nothing in the list applies.
#
# IMPORTANT: keep these lowercase, no punctuation — they become node IDs directly.
#
CONCEPT_VOCAB: list[str] = [
    # IEC 62061 / IEC 61508 — probabilistic
    "safety integrity level",
    "sil verification",
    "pfhd",
    "pfd",
    "hardware fault tolerance",
    "safe failure fraction",
    "architectural constraint",
    "type a subsystem",
    "type b subsystem",
    "systematic capability",
    "diagnostic coverage",
    "common cause failure",
    "proof test interval",
    "mean time to dangerous failure",
    "demand mode",
    "continuous mode",
    # ISO 13849
    "performance level",
    "category b",
    "category 1",
    "category 2",
    "category 3",
    "category 4",
    "mttfd",
    "dcavg",
    # Functional safety management
    "safety function",
    "safe state",
    "safety requirements specification",
    "safety validation",
    "verification and validation",
    "functional safety management",
    "safety lifecycle",
    "safety case",
    "safety instrumented system",
    "safety instrumented function",
    "final element",
    "logic solver",
    "process safety time",
    "spurious trip rate",
    # Risk
    "hazard analysis",
    "risk assessment",
    "risk reduction",
    "tolerable risk",
    "residual risk",
    "failure mode",
    # Testing
    "factory acceptance test",
    "site acceptance test",
    "functional test",
    "proof test",
    # Engineering
    "response time",
    "emergency shutdown",
    "plc programming",
    "software validation",
    "change management",
    "configuration management",
    # Controls engineering — logic / programming
    "plc program",
    "ladder logic",
    "function block",
    "structured text",
    "scan cycle",
    "tag name",
    "io mapping",
    # Controls engineering — operational modes
    "interlock",
    "permissive",
    "bypass",
    "manual override",
    "hand/off/auto",
    # HMI / SCADA
    "hmi screen",
    "alarm setpoint",
    "trend group",
    "operator permission",
    # Instrumentation
    "pid loop",
    "loop tuning",
    "analog scaling",
    "instrument range",
    "transmitter",
    # Motors / drives / power
    "vfd drive",
    "motor starter",
    "soft start",
    "drive parameter",
    "motor control center",
    "safety relay",
    # Engineering documentation
    "p&id",
    "loop diagram",
    "wiring diagram",
    "sequence of operations",
    "functional description",
]

# ── Document roles ─────────────────────────────────────────────────────────────

DOC_ROLES = (
    "requirement",      # states what the system SHALL/MUST do
    "definition",       # defines a term or abbreviation
    "finding",          # records an observed fact, audit finding, test result
    "test_evidence",    # test steps, acceptance criteria, pass/fail records
    "obligation",       # contractual duty or commitment
    "implementation",   # describes how something is implemented (code/design)
    "reference",        # cross-references other documents without own content
    "informative",      # background, context, non-normative explanation
    "unknown",          # cannot be determined
)


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class ExtractionResult:
    """
    Structured output from ``extract_chunk()``.

    All fields default to safe empty values so callers can always unpack
    without None-checking every field.
    """
    concepts:      list[str] = field(default_factory=list)
    entities:      list[str] = field(default_factory=list)
    doc_role:      str       = "unknown"
    key_assertion: str       = ""

    def is_empty(self) -> bool:
        return not self.concepts and not self.entities


# ── Prompt construction ────────────────────────────────────────────────────────

_ROLES_BLOCK = ", ".join(DOC_ROLES)


def _build_system_prompt(extra_vocab: list[str] | None = None) -> str:
    """
    Build the extraction system prompt, merging the seeded vocabulary with any
    concepts already learned from previously ingested documents.

    :param extra_vocab: Learned concept labels from the graph DB.  These are
                        merged with ``CONCEPT_VOCAB`` and deduplicated so the
                        model sees the full accumulated vocabulary.
    """
    merged = list(CONCEPT_VOCAB)
    if extra_vocab:
        # Defensive cap: callers are expected to pass a pre-ranked, already-
        # limited list (db.list_concept_vocab(..., limit=MAX_LEARNED_VOCAB)),
        # but we clip here too so a careless caller can't blow past num_ctx.
        capped   = extra_vocab[:MAX_LEARNED_VOCAB]
        existing = set(merged)
        for c in capped:
            if c not in existing:
                merged.append(c)
                existing.add(c)
    vocab_block = "\n".join(f"  - {c}" for c in merged)
    return f"""You are a controls-engineering document analyst with functional-safety depth.
Your task is to extract structured metadata from a document chunk.
Return ONLY a JSON object — no prose, no markdown fences, no explanation.

JSON schema (all fields required):
{{
  "concepts":      [list of 3-7 strings — abstract topics the chunk addresses],
  "entities":      [list of 0-5 strings — specific named items: SIL/PL values,
                    standard clauses, component/function names, numeric parameters],
  "doc_role":      one of: {_ROLES_BLOCK},
  "key_assertion": one-sentence summary of the chunk's main claim (max 120 chars)
}}

Concept selection rules:
1. Prefer terms from this vocabulary when they apply:
{vocab_block}
2. If a concept is genuinely not in the vocabulary, add a new lowercase term.
3. Only include concepts that are meaningfully present — not every term above will apply.
4. Return 3-7 concepts per chunk; fewer is better than padding with weak matches.

Entity selection rules:
1. Include specific values: "SIL 2", "PLd", "Category 3", "IEC 61508-1 clause 7.4"
2. Include named components, functions, or parameters with quantitative values.
3. Omit vague or generic entity-like phrases.
4. Return 0-5 entities; empty list is valid.
"""


def _build_user_prompt(text: str, doc_type: str) -> str:
    doc_hint = f" (document type: {doc_type})" if doc_type else ""
    return f"Analyse this chunk{doc_hint} and return the JSON metadata:\n\n{text}"


# ── JSON extraction ────────────────────────────────────────────────────────────

def _parse_response(raw: str) -> ExtractionResult:
    """
    Parse the LLM response into an ExtractionResult.

    Handles: clean JSON, JSON wrapped in ```json … ```, and responses with
    leading/trailing prose.  Returns an empty result on any parse failure so
    the caller never crashes due to a bad extraction.
    """
    text = raw.strip()

    # Strip markdown code fences if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    else:
        # Try to find the first '{' ... last '}' in the response.
        start = text.find("{")
        end   = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("extractor: JSON parse failed — raw: %s", raw[:200])
        return ExtractionResult()

    concepts = [str(c).lower().strip() for c in data.get("concepts", []) if c]
    entities = [str(e).strip() for e in data.get("entities", []) if e]
    doc_role = str(data.get("doc_role", "unknown")).strip()
    if doc_role not in DOC_ROLES:
        doc_role = "unknown"
    key_assertion = str(data.get("key_assertion", "")).strip()[:200]

    return ExtractionResult(
        concepts=concepts[:7],
        entities=entities[:5],
        doc_role=doc_role,
        key_assertion=key_assertion,
    )


# ── Public API ─────────────────────────────────────────────────────────────────

async def extract_chunk(
    text: str,
    doc_type: str = "",
    model: str = "",
    learned_vocab: list[str] | None = None,
) -> ExtractionResult:
    """
    Extract concepts, entities, document role, and key assertion from a chunk.

    Calls the configured Ollama model with a structured prompt and parses the
    JSON response.  Returns an empty ``ExtractionResult`` on any error so the
    caller can always proceed — extraction failure is non-fatal; the chunk
    simply gets no concept graph edges.

    :param text:     Chunk text to analyse (typically ≤1000 chars).
    :param doc_type: Optional document type hint (e.g. ``"theop"``, ``"fmea"``)
                     included in the prompt for context.
    :param model:    Ollama model override; defaults to ``EXTRACT_MODEL``.
    :return:         Structured extraction result.
    :rtype:          ExtractionResult
    """
    m = model or _extract_model()
    messages = [
        {"role": "system", "content": _build_system_prompt(learned_vocab)},
        {"role": "user",   "content": _build_user_prompt(text, doc_type)},
    ]
    try:
        async with httpx.AsyncClient(timeout=EXTRACT_TIMEOUT, headers=config.OLLAMA_EXTRACTOR_HEADERS) as client:
            resp = await client.post(
                f"{config.OLLAMA_BASE_URL}/api/chat",
                json={
                    "model":    m,
                    "stream":   False,
                    "messages": messages,
                    "think":    False,
                    "options":  {"num_predict": 384},
                },
            )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        return _parse_response(content)
    except Exception as exc:
        # Log the exception *type* as well as its str() — several httpx errors
        # (ReadTimeout, RemoteProtocolError, ConnectError) have empty __str__,
        # which made earlier failures invisible in the logs.
        logger.warning(
            "extractor: extract_chunk failed (%s: %s)",
            type(exc).__name__,
            exc or "<no message>",
        )
        return ExtractionResult()


async def _submit_extract_batch_job(
    client: httpx.AsyncClient,
    prompt: str,
    model: str,
) -> str | None:
    """
    Submit a single extraction prompt to merLLM's ``/api/batch/submit``.

    Returns the merLLM job ID on success, or None on submission failure.
    ``options`` mirrors what merLLM's batch runner auto-fills on the proxy
    path — we specify them explicitly so the batch runner's defensive
    defaults do not drift from what the extractor actually asked for.
    """
    try:
        resp = await client.post(
            f"{config.MERLLM_URL}/api/batch/submit",
            json={
                "source_app": "lancellmot",
                "prompt":     prompt,
                "model":      model,
                "options": {
                    "think":       False,
                    "num_predict": 384,
                    "num_ctx":     8192,
                    "temperature": 0.1,
                },
            },
            timeout=BATCH_SUBMIT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("id")
    except Exception as exc:
        logger.warning(
            "extractor: batch submit failed (%s: %s)",
            type(exc).__name__, exc or "<no message>",
        )
        return None


class _BatchPoller:
    """
    Shared batch-job poller for one ``extract_chunks_batch`` run.

    One task issues ``POST /api/batch/status-by-ids`` every
    BATCH_POLL_INTERVAL, passing the full set of still-pending job IDs,
    and resolves per-job futures as each job reaches a terminal state.
    Replaces the prior per-chunk poll loop that issued N concurrent
    ``GET /api/batch/results/{id}`` calls — on a 100-chunk ingest merLLM
    saw ~100 requests every 3 s, almost all returning 409 "still running"
    (#40).

    status-by-ids is explicitly scoped to our in-flight IDs, so it is
    immune to the 200-row LIMIT on ``GET /api/batch/status`` — a
    concurrent ingest from another client can no longer push our jobs
    out of visibility. merLLM owns the job's lifetime; we only declare a
    job lost if it drops out of merLLM's DB entirely for
    ``BATCH_MISS_TOLERANCE`` consecutive polls (manual drain, DB wipe).
    """

    def __init__(self, client: httpx.AsyncClient):
        self._client  = client
        self._pending: dict[str, asyncio.Future[str | None]] = {}
        self._misses:  dict[str, int] = {}
        self._stopped = False

    def register(self, job_id: str) -> asyncio.Future[str | None]:
        fut = asyncio.get_running_loop().create_future()
        self._pending[job_id] = fut
        self._misses[job_id] = 0
        return fut

    def _drop(self, job_id: str) -> None:
        self._pending.pop(job_id, None)
        self._misses.pop(job_id, None)

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        while not self._stopped:
            await asyncio.sleep(BATCH_POLL_INTERVAL)
            if self._stopped:
                break
            if not self._pending:
                continue

            ids = list(self._pending.keys())
            try:
                resp = await self._client.post(
                    f"{config.MERLLM_URL}/api/batch/status-by-ids",
                    json={"ids": ids},
                    timeout=BATCH_SUBMIT_TIMEOUT,
                )
                resp.raise_for_status()
                jobs = resp.json()
            except Exception as exc:
                logger.warning(
                    "extractor: shared batch poll failed (%s: %s) — retrying",
                    type(exc).__name__, exc or "<no message>",
                )
                continue

            by_id = {j.get("id"): j for j in jobs if j.get("id")}
            for job_id in ids:
                fut = self._pending.get(job_id)
                if fut is None or fut.done():
                    continue
                rec = by_id.get(job_id)
                if rec is None:
                    self._misses[job_id] = self._misses.get(job_id, 0) + 1
                    if self._misses[job_id] >= BATCH_MISS_TOLERANCE:
                        logger.warning(
                            "extractor: batch job %s missing from merLLM "
                            "after %d consecutive polls — giving up",
                            job_id[:8], self._misses[job_id],
                        )
                        fut.set_result(None)
                        self._drop(job_id)
                    continue

                # Job present → reset miss counter and check status.
                self._misses[job_id] = 0
                status = rec.get("status")
                if status == "completed":
                    fut.set_result(rec.get("result") or "")
                    self._drop(job_id)
                elif status in ("failed", "cancelled"):
                    logger.warning(
                        "extractor: batch job %s reached terminal state %s",
                        job_id[:8], status,
                    )
                    fut.set_result(None)
                    self._drop(job_id)
                # else queued / running — keep waiting indefinitely.


async def _extract_one_via_batch(
    client: httpx.AsyncClient,
    poller: "_BatchPoller",
    submit_sem: asyncio.Semaphore,
    chunk: str,
    system_prompt: str,
    doc_type: str,
    model: str,
) -> ExtractionResult:
    """Submit one chunk as a batch job and parse its eventual result."""
    # merLLM's ``/api/batch/submit`` runs the job against ``/api/generate``,
    # which takes a single prompt string. Flatten system+user into one prompt
    # with a separator the model can distinguish.
    user_prompt = _build_user_prompt(chunk, doc_type)
    prompt = f"{system_prompt}\n\n{user_prompt}"

    # Semaphore wraps only the submit POST — releasing as soon as merLLM
    # has the job ID, so the subsequent ``await fut`` on the shared poller
    # does not hold a slot. This is the #51 fix; wrapping the whole body
    # would re-create the in-flight cap removed in #48.
    async with submit_sem:
        job_id = await _submit_extract_batch_job(client, prompt, model)
    if not job_id:
        return ExtractionResult()

    fut = poller.register(job_id)
    # No wall-clock deadline: merLLM owns the job's lifetime. The poller
    # resolves the future only when merLLM reports a terminal status or
    # forgets the job (BATCH_MISS_TOLERANCE misses in a row).
    response_text = await fut

    if not response_text:
        return ExtractionResult()

    return _parse_response(response_text)


async def extract_chunks_batch(
    chunks: list[str],
    doc_type: str = "",
    model: str = "",
    learned_vocab: list[str] | None = None,
) -> list[ExtractionResult]:
    """
    Extract metadata for a list of chunks via merLLM's durable batch queue.

    Every chunk is submitted as a ``/api/batch/submit`` job, which merLLM
    persists in its SQLite ``batch_jobs`` table. Jobs survive merLLM
    restarts (power outage, code redeploy) because merLLM's
    ``requeue_orphaned_jobs`` path resets them to ``queued`` at startup —
    the pre-migration sync ``/api/chat`` path silently lost every in-flight
    chunk when merLLM was restarted. See hexcaliper#29 for the incident.

    Submission runs in a single async session. A single shared poller task
    (``_BatchPoller``) issues one ``GET /api/batch/status`` per poll
    interval and resolves per-chunk futures as jobs reach terminal states —
    O(1) request rate regardless of chunk concurrency (#40). merLLM itself
    serialises execution across the BACKGROUND bucket.

    Per-chunk failure (submit error, poll timeout, merLLM-reported failure,
    JSON parse error) is non-fatal and yields an empty ``ExtractionResult``
    in that slot — same contract as the pre-migration path.

    :param chunks:        List of chunk texts.
    :param doc_type:      Document type hint included in every chunk's prompt.
    :param model:         Ollama model override; defaults to ``EXTRACT_MODEL``.
    :param learned_vocab: Accumulated concept vocabulary from the graph DB;
                          merged with the seeded vocab in the prompt.
    :return:              ``ExtractionResult`` list, one per input chunk, in
                          the same order.
    """
    if not chunks:
        return []

    m = model or _extract_model()
    system_prompt = _build_system_prompt(learned_vocab)

    # Per-call cap on simultaneous submit POSTs; mirrors rag.py's embed
    # path. Scope is the submit round-trip only — not the full chunk
    # lifetime — so merLLM's queue still absorbs work in bulk while the
    # httpx connection pool stays bounded regardless of doc size (#51).
    submit_sem = asyncio.Semaphore(EXTRACT_SUBMIT_CONCURRENCY)

    # One httpx session for submit + the shared status poll — cheaper than
    # reopening a client per chunk, and still fully async.
    async with httpx.AsyncClient() as client:
        poller = _BatchPoller(client)
        poller_task = asyncio.create_task(poller.run())
        try:
            tasks = [
                _extract_one_via_batch(
                    client, poller, submit_sem, chunk,
                    system_prompt, doc_type, m,
                )
                for chunk in chunks
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            poller.stop()
            await poller_task

    # gather with return_exceptions keeps the slot order intact even if one
    # coroutine blew up — map any exception to an empty result so callers
    # get len(results) == len(chunks) unconditionally.
    normalised: list[ExtractionResult] = []
    for idx, r in enumerate(results):
        if isinstance(r, Exception):
            logger.warning(
                "extractor: batch chunk %d raised (%s: %s)",
                idx, type(r).__name__, r or "<no message>",
            )
            normalised.append(ExtractionResult())
        else:
            normalised.append(r)
    return normalised
