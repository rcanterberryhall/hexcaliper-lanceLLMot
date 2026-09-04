"""Tool registry and dispatch for the lanceLLMot MCP server.

Adopted from parsival's proven pattern: tools register with the
:func:`tool` decorator, which records the callable, its JSON Schema and
its description in module-level dicts. ``dispatch`` invokes a tool by
keyword expansion, so **the schema's property names are the wire
contract** — renaming a parameter breaks callers even when the tool
name is unchanged. ``tests/test_mcp.py::test_schema_matches_signature``
pins that invariant.

One extension over parsival's registry: ``docs.search`` is async (its
query embed rides merLLM), so ``dispatch`` may return a coroutine and
the endpoint awaits it.

Every tool is read-only: handlers call existing query functions only.
MCP calls carry no Cloudflare identity header, so tools act as the
configured service identity ``config.MCP_USER_EMAIL`` (default
``local@dev``, the same default the app uses when the header is
absent). Backing modules are imported lazily inside each handler so
this module stays importable without ChromaDB or the graph store.

This module must never import ``app`` — that would create an import
cycle, since ``app`` mounts the router that imports this module.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

import config

TOOL_REGISTRY: dict[str, Callable[..., Any]] = {}
TOOL_SCHEMAS: dict[str, dict] = {}
TOOL_DESCRIPTIONS: dict[str, str] = {}


def tool(name: str, description: str, schema: dict) -> Callable:
    """Register a function as an MCP tool.

    Args:
        name: Wire name, ``noun.verb`` by convention.
        description: One line shown to the calling agent in ``tools/list``.
        schema: JSON Schema for the arguments object.

    Returns:
        The undecorated function, so it stays directly callable in tests.
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOL_REGISTRY[name] = fn
        TOOL_SCHEMAS[name] = schema
        TOOL_DESCRIPTIONS[name] = description
        return fn

    return deco


def tool_specs() -> list[dict]:
    """Return the MCP ``tools/list`` payload for every registered tool."""
    return [
        {
            "name": name,
            "description": TOOL_DESCRIPTIONS[name],
            "inputSchema": TOOL_SCHEMAS[name],
        }
        for name in TOOL_REGISTRY
    ]


def dispatch(name: str, arguments: dict) -> Any:
    """Invoke a registered tool by keyword expansion.

    Args:
        name: Registered tool name.
        arguments: Keyword arguments from the MCP ``tools/call`` params.

    Returns:
        The tool's return value — possibly a coroutine, which the caller
        awaits (``docs.search`` embeds its query through merLLM).
    """
    return TOOL_REGISTRY[name](**arguments)


@tool(
    "docs.search",
    "Semantic search over the indexed document store; returns the most "
    "relevant text chunks with document ids, chunk ids, scores, and "
    "section anchors.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "natural-language search query"},
            "top_k": {"type": "integer",
                      "description": "maximum chunks to return (default 8)"},
        },
        "required": ["query"],
    },
)
async def docs_search(query: str, top_k: Optional[int] = None) -> dict:
    import rag

    chunks, doc_ids, chunk_ids, scores, anchors = await rag.search(
        config.MCP_USER_EMAIL,
        query,
        top_k=top_k or 8,
        # An agent turn is waiting on this embed, so it rides the CHAT
        # bucket rather than queueing behind ingest-chunk embeds.
        priority="chat",
    )
    return {
        "results": [
            {"text": c, "doc_id": d, "chunk_id": ch,
             "score": round(s, 4), "anchor": a}
            for c, d, ch, s, a in
            zip(chunks, doc_ids, chunk_ids, scores, anchors)
        ]
    }


@tool(
    "docs.get",
    "Fetch one indexed document's metadata and summary by document id.",
    {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string",
                       "description": "document id from docs.search"},
        },
        "required": ["doc_id"],
    },
)
def docs_get(doc_id: str) -> dict:
    import db

    doc = db.get_document(doc_id)
    if doc is None:
        return {"error": f"no document with id {doc_id}"}
    return doc


@tool(
    "graph.context",
    "Expand a chunk through the concept and reference graph: sibling "
    "chunks, chunks sharing a cited standard, and clause-referenced "
    "chunks, ranked by relevance.",
    {
        "type": "object",
        "properties": {
            "chunk_id": {"type": "string",
                         "description": "chunk id from docs.search"},
            "max_n": {"type": "integer",
                      "description": "maximum related chunks (default 5)"},
        },
        "required": ["chunk_id"],
    },
)
def graph_context(chunk_id: str, max_n: Optional[int] = None) -> dict:
    import graph

    items = graph.get_context(chunk_id, config.MCP_USER_EMAIL,
                              max_n=max_n or 5)
    return {"related": items}


@tool(
    "library.list",
    "List technical library documents (datasheets, manuals, standards), "
    "filterable by manufacturer, product id, and document type.",
    {
        "type": "object",
        "properties": {
            "manufacturer": {"type": "string"},
            "product_id": {"type": "string"},
            "doc_type": {"type": "string"},
        },
        "required": [],
    },
)
def library_list(manufacturer: Optional[str] = None,
                 product_id: Optional[str] = None,
                 doc_type: Optional[str] = None) -> dict:
    import db

    items = db.list_library_items(manufacturer=manufacturer,
                                  product_id=product_id,
                                  doc_type=doc_type)
    return {"items": items}
