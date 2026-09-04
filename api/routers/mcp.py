"""MCP server transport for lanceLLMot (sheet REQ-F-013: document
search, the concept graph, and the library exposed to the agent as MCP
tools).

JSON-RPC 2.0 over HTTP. The route is registered bare as ``/mcp``
because nginx rewrites ``^/api/(.*)`` to ``/$1``, so the reachable URL
is ``/api/mcp`` — the same shape as parsival's server, whose transport
this file adopts.

Authentication is a shared secret supplied in ``X-LanceLLMot-MCP-Token``
and compared in constant time. An unset ``config.MCP_TOKEN`` rejects
every request rather than disabling the check: this endpoint reaches
the whole document store, so failing open is not an acceptable
degradation.

Tool failures are returned as an ``isError`` result rather than a
JSON-RPC protocol error, so the calling agent can read the message and
retry; a protocol error would abort the call.
"""

from __future__ import annotations

import hmac
import inspect
import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Response

import config
import mcp_tools

_log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "lancellmot-docs"
SERVER_VERSION = "0.1.0"

router = APIRouter()


def _authorised(supplied: str | None) -> bool:
    """Return True when ``supplied`` matches the configured token.

    Compare bytes, not str: Starlette decodes raw header bytes as
    latin-1, so a header containing a byte >= 0x80 produces a non-ASCII
    str that ``hmac.compare_digest`` rejects with a TypeError instead of
    returning False. ``encode`` with surrogateescape can never raise
    here, so a hostile header degrades to a normal mismatch, not a 500.
    """
    expected = config.MCP_TOKEN or ""
    if not expected:
        return False
    supplied_bytes = (supplied or "").encode("utf-8", "surrogateescape")
    return hmac.compare_digest(supplied_bytes, expected.encode())


def _result(id_: Any, result: dict) -> dict:
    """Build a JSON-RPC success envelope."""
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    """Build a JSON-RPC error envelope."""
    return {"jsonrpc": "2.0", "id": id_,
            "error": {"code": code, "message": message}}


@router.post("/mcp")
async def mcp_endpoint(
    body: dict,
    x_lancellmot_mcp_token: str | None = Header(default=None),
):
    """Handle one JSON-RPC request against the tool registry.

    Async because ``docs.search`` dispatches to a coroutine (its query
    embed rides merLLM); coroutine results are awaited before
    serialisation.

    Raises:
        HTTPException: 401 when the token is absent, wrong, or
        unconfigured.
    """
    if not _authorised(x_lancellmot_mcp_token):
        raise HTTPException(status_code=401,
                            detail="invalid or missing MCP token")

    method = body.get("method")
    id_ = body.get("id")
    params = body.get("params") or {}

    if method == "notifications/initialized":
        return Response(status_code=202)

    if method == "initialize":
        return _result(
            id_,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME,
                               "version": SERVER_VERSION},
            },
        )

    if method == "tools/list":
        return _result(id_, {"tools": mcp_tools.tool_specs()})

    if method == "tools/call":
        name = params.get("name", "")
        if name not in mcp_tools.TOOL_REGISTRY:
            # MCP spec: -32602 (Invalid params) for an unknown tool name;
            # -32601 (Method not found) is for an unknown JSON-RPC method.
            return _error(id_, -32602, f"unknown tool: {name}")
        try:
            out = mcp_tools.dispatch(name, params.get("arguments") or {})
            if inspect.iscoroutine(out):
                out = await out
        except Exception as exc:  # noqa: BLE001 — tool errors travel on the wire
            _log.warning("mcp tool %s failed: %s", name, exc)
            return _result(id_, {"content": [{"type": "text",
                                              "text": str(exc)}],
                                 "isError": True})
        return _result(
            id_,
            {
                "content": [{"type": "text",
                             "text": json.dumps(out, default=str)}],
                "isError": False,
            },
        )

    return _error(id_, -32601, f"unknown method: {method}")
