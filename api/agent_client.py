"""HTTP client for the hermes-agent runs API (sheet REQ-F-001: agent
turns execute in the hermes service, not by a direct model call).

Three calls cover the whole lifecycle: ``create_run`` starts a turn,
``stream_events`` yields its Server-Sent Events as dicts, and
``stop_run`` ends it early (REQ-F-010). The hermes API server lives on
the shared docker network as ``http://hermes:8642`` (config.HERMES_URL)
behind a bearer key.

Events arrive as ``data: {json}`` SSE frames; comment lines and the
closing sentinel are dropped here so the router only sees event dicts.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

import config

# One turn can wait on model loads and several tool rounds; the events
# stream must outlive all of it. Connect stays short so an unreachable
# hermes fails fast (REQ-F-012's graceful degradation depends on it).
_TIMEOUT = httpx.Timeout(600.0, connect=5.0)


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.HERMES_API_KEY}"}


async def create_run(message: str,
                     session_id: Optional[str] = None) -> dict:
    """Start a hermes run; returns the response dict (``run_id`` etc.).

    ``session_id`` resumes an existing hermes session so the turn sees
    its conversation history (REQ-F-008).
    """
    body: dict = {"input": message}
    if session_id:
        body["session_id"] = session_id
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{config.HERMES_URL}/v1/runs",
                                 json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_run(run_id: str) -> dict:
    """Fetch a run's status record (used to learn its session id)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{config.HERMES_URL}/v1/runs/{run_id}",
                                headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def stream_events(run_id: str) -> AsyncIterator[dict]:
    """Yield each SSE event of a run as a dict, until the stream closes."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream(
            "GET", f"{config.HERMES_URL}/v1/runs/{run_id}/events",
            headers=_headers(),
        ) as resp:
            resp.raise_for_status()
            buf = b""
            async for chunk in resp.aiter_bytes():
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if not text.startswith("data: "):
                        continue  # SSE comments, blank keepalives
                    try:
                        yield json.loads(text[len("data: "):])
                    except json.JSONDecodeError:
                        continue


async def stop_run(run_id: str) -> None:
    """Ask hermes to end a run; pending tool rounds stop with it."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{config.HERMES_URL}/v1/runs/{run_id}/stop",
            headers=_headers())
        resp.raise_for_status()
