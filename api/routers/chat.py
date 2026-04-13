"""
routers/chat.py — Streaming chat endpoint with RAG + graph context.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import config
import db
import graph
import ollama as ollama_client
import rag
import web_fetch
import web_search
from models import ChatRequest

router = APIRouter()

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information, recent events, news, or prices "
            "that are not in the model's training data. "
            "Use this ONLY when the answer cannot be found in the conversation, "
            "the user's uploaded documents, or the model's existing knowledge. "
            "Do NOT use this for questions about documents the user has already uploaded."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query"}},
            "required": ["query"],
        },
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user(request: Request) -> str:
    return request.headers.get("cf-access-authenticated-user-email", "local@dev")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _build_scope(user_email: str, conv_id: str,
                 project_id: Optional[str]) -> tuple[list[str], list[Optional[str]]]:
    """Return (scope_types, scope_ids) for the current chat context."""
    scope_types: list[str]           = ["global"]
    scope_ids:   list[Optional[str]] = [None]
    if project_id:
        scope_types.append("project"); scope_ids.append(project_id)
        proj = db.get_project(project_id)
        if proj and proj.get("client_id"):
            scope_types.append("client"); scope_ids.append(proj["client_id"])
    scope_types.append("session"); scope_ids.append(conv_id)
    return scope_types, scope_ids


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    user_email = _user(request)
    model      = (req.model or config.DEFAULT_MODEL).strip() or config.DEFAULT_MODEL

    with db.lock:
        if req.conversation_id:
            conv = db.get_conversation(req.conversation_id)
            if not conv:
                raise HTTPException(status_code=404, detail="Conversation not found.")
            if conv["user_email"] != user_email:
                raise HTTPException(status_code=403, detail="Access denied.")
            conv_id = conv["id"]
            history = list(conv.get("messages", []))
        else:
            conv_id = str(uuid.uuid4())
            ts      = _now_iso()
            db.insert_conversation({
                "id": conv_id, "user_email": user_email, "title": req.message[:60],
                "model": model, "created_at": ts, "updated_at": ts, "messages": [],
            })
            history = []

    scope_types, scope_ids = _build_scope(user_email, conv_id, req.project_id)

    rag_errors: list[str] = []

    try:
        url_context = await web_fetch.fetch_context(req.message)
    except Exception:
        log.exception("Web fetch failed, proceeding without URL context")
        url_context = {}

    chunk_scores:  list[float] = []
    chunk_anchors: list[str]   = []
    try:
        doc_chunks, doc_ids, chunk_ids, chunk_scores, chunk_anchors = await rag.search(
            user_email, req.message,
            scope_types=scope_types, scope_ids=scope_ids,
        )
    except Exception as exc:
        log.exception("RAG vector search failed, proceeding without document context")
        rag_errors.append(f"vector search: {exc}")
        doc_chunks, doc_ids, chunk_ids, chunk_anchors = [], [], [], []

    with db.lock:
        all_docs = db.list_documents_for_scope(user_email, scope_types, scope_ids)

    has_client_docs = any(d.get("classification") == "client" for d in all_docs)

    # Graph context
    graph_chunks:      list[str] = []
    graph_context_str: str       = ""
    try:
        seen           = set(chunk_ids)
        doc_title_map  = {d["id"]: d.get("filename", d["id"]) for d in all_docs}
        collected_ctx: list[dict] = []
        graph_cids:    list[str]  = []
        for cid in chunk_ids:
            for ctx in graph.get_context(
                cid, user_email,
                scope_types=scope_types, scope_ids=scope_ids, max_n=3,
            ):
                gcid = ctx.get("chunk_id", "")
                if gcid and gcid not in seen:
                    seen.add(gcid)
                    graph_cids.append(gcid)
                    collected_ctx.append(ctx)
        if graph_cids:
            chunk_text_map = rag.get_chunks_by_ids(graph_cids)
            if chunk_text_map:
                graph_context_str = graph.format_context(
                    [c for c in collected_ctx if c.get("chunk_id","") in chunk_text_map],
                    doc_titles=doc_title_map,
                )
                # get_chunks_by_ids now returns (text, anchor) tuples so the
                # citation layer has the structural label available; only the
                # text is dropped into the LLM prompt here.
                graph_chunks = [entry[0] for entry in chunk_text_map.values()]
    except Exception as exc:
        log.exception("Graph context retrieval failed, proceeding without graph context")
        rag_errors.append(f"graph context: {exc}")

    # Copyright notices
    doc_copyright_notices: list[str] = []
    if doc_ids:
        with db.lock:
            for did in dict.fromkeys(doc_ids):
                doc = db.get_document(did)
                if doc:
                    doc_copyright_notices.extend(doc.get("copyright_notices") or [])

    # Look up saved system prompt on the conversation
    saved_system: Optional[str] = None
    with db.lock:
        _conv_row = db.get_conversation(conv_id)
        if _conv_row and _conv_row.get("system_prompt_id"):
            _sp = db.get_system_prompt(_conv_row["system_prompt_id"])
            if _sp:
                saved_system = _sp["content"]

    # Build message list
    messages = []
    if saved_system:
        messages.append({"role": "system", "content": saved_system})
    elif req.system:
        messages.append({"role": "system", "content": req.system.strip()})
    if all_docs:
        doc_lines = [
            f"  • {d['filename']}" + (f": {d['summary']}" if d.get("summary") else "")
            for d in all_docs
        ]
        messages.append({"role": "system", "content": (
            "The user has uploaded the following documents:\n" + "\n".join(doc_lines)
            + "\n\nRelevant excerpts will be provided below when they match the query. "
            "When asked about these documents, use the excerpts provided or the summaries above."
        )})

    context_parts = []
    if doc_chunks:
        if doc_copyright_notices:
            unique_notices = list(dict.fromkeys(doc_copyright_notices))
            notice_block = (
                "Copyright notices detected in the source documents:\n"
                + "\n".join(f"  • {n}" for n in unique_notices)
                + "\n\nThe user is responsible for compliance with these terms. "
                "Prefer summaries, citations, and analysis over verbatim reproduction."
            )
        else:
            notice_block = (
                "Note: These documents may be copyrighted. "
                "The user is responsible for compliance with applicable licenses. "
                "Prefer summaries, citations, and analysis over verbatim reproduction."
            )
        # Prefix each chunk with its structural anchor (when present) so the
        # LLM can cite "§4.3 Architectural constraints" instead of paraphrasing
        # or omitting the source location. Chunks without anchors (pre-#31 or
        # fixed-window fallback) fall through unchanged.
        annotated_doc_chunks = [
            f"[{anc}] {ch}" if anc else ch
            for ch, anc in zip(doc_chunks, chunk_anchors)
        ]
        context_parts.append(
            notice_block + "\n\nRelevant information from the user's documents:\n\n"
            + "\n\n---\n\n".join(annotated_doc_chunks)
        )
    if graph_chunks and graph_context_str:
        context_parts.append(
            graph_context_str + "\n\nContent from cross-referenced documents:\n\n"
            + "\n\n---\n\n".join(graph_chunks)
        )
    for url, content in url_context.items():
        context_parts.append(f"Content fetched from {url}:\n\n{content}")
    if context_parts:
        messages.append({"role": "system", "content": "\n\n===\n\n".join(context_parts)})
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": req.message})

    _THINKING_MODELS = ("deepseek-r1", "deepseek-r2", "qwq", "marco-o1")
    supports_think   = any(t in model.lower() for t in _THINKING_MODELS)
    payload  = {"model": model, "stream": True, "messages": messages}
    if supports_think:
        payload["think"] = True
    sources  = {
        "doc_chunks":   len(doc_chunks),
        "graph_chunks": len(graph_chunks),
        "urls":         list(url_context.keys()),
        "rag_status":   "error" if rag_errors else "ok",
        "rag_errors":   rag_errors,
    }

    # Build attribution data for the sources SSE event.
    doc_title_map_outer = {d["id"]: d.get("filename", d["id"]) for d in all_docs}
    # Structural anchors lag chunk_ids by length when a legacy fallback code
    # path produced no anchors (e.g. the RAG-error branch); pad defensively.
    padded_anchors = (chunk_anchors + [""] * len(doc_chunks))[:len(doc_chunks)]
    sources_docs: list[dict] = [
        {
            "name":   doc_title_map_outer.get(did, did),
            "chunk":  chunk[:100],
            "score":  score,
            "anchor": anchor,
        }
        for chunk, did, score, anchor in zip(
            doc_chunks, doc_ids, chunk_scores, padded_anchors,
        )
    ]
    sources_graph: list[dict] = [
        {
            "entity":   ctx.get("label") or ctx.get("chunk_id", ""),
            "relation": ctx.get("context_edge", "related"),
            "score":    ctx.get("context_score", 0.0),
        }
        for ctx in collected_ctx
    ]

    async def generate():
        reply_parts:    list[str] = []
        search_queries: list[str] = []
        _saved = False

        # Emit retrieval status before the first token so the UI can show
        # context indicators (doc count, graph nodes, errors) immediately.
        yield _sse({
            "type":        "rag_status",
            "status":      "error" if rag_errors else "ok",
            "docs_used":   len(doc_chunks),
            "graph_nodes": len(graph_chunks),
            "errors":      rag_errors,
        })

        def _save_to_db(partial: bool = False) -> None:
            nonlocal _saved
            if _saved:
                return
            text = "".join(reply_parts)
            if not text:
                return
            _saved  = True
            suffix  = " *(response interrupted)*" if partial else ""
            ts_now  = _now_iso()
            with db.lock:
                conv = db.get_conversation(conv_id)
                if conv:
                    updated = list(conv.get("messages", []))
                    updated.append({"role": "user",      "content": req.message,   "ts": ts_now})
                    updated.append({"role": "assistant",  "content": text + suffix, "ts": ts_now})
                    fields = {"messages": updated, "updated_at": ts_now, "model": model}
                    if not req.conversation_id:
                        fields["title"] = req.message[:60]
                    db.update_conversation(conv_id, fields)

        try:
            async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT, headers=config.OLLAMA_HEADERS) as client:
                first_payload        = {**payload, "tools": [WEB_SEARCH_TOOL]}
                tool_calls_received: list[dict] = []
                first_content:       list[str]  = []

                async for chunk in ollama_client.stream_chat(client, first_payload):
                    if "_error" in chunk:
                        if "does not support tools" in chunk["_error"]:
                            yield _sse({"type": "warning", "detail": f"{model} does not support web search (tool calling). Responding without it."})
                            async for chunk2 in ollama_client.stream_chat(client, payload):
                                if "_error" in chunk2:
                                    yield _sse({"type": "error", "detail": chunk2["_error"]}); return
                                msg2 = chunk2.get("message", {})
                                if msg2.get("thinking"):
                                    yield _sse({"type": "think", "content": msg2["thinking"]})
                                token2 = msg2.get("content", "")
                                if token2:
                                    first_content.append(token2)
                                    yield _sse({"type": "token", "content": token2})
                                if chunk2.get("done"): break
                            break
                        yield _sse({"type": "error", "detail": chunk["_error"]}); return
                    msg = chunk.get("message", {})
                    if msg.get("thinking"):
                        yield _sse({"type": "think", "content": msg["thinking"]})
                    if msg.get("tool_calls"):
                        tool_calls_received = msg["tool_calls"]
                    token = msg.get("content", "")
                    if token:
                        first_content.append(token)
                        yield _sse({"type": "token", "content": token})
                    if chunk.get("done"): break

                if tool_calls_received:
                    follow_up = list(messages)
                    follow_up.append({"role": "assistant", "content": "", "tool_calls": tool_calls_received})
                    for tc in tool_calls_received:
                        fn = tc.get("function", {})
                        if fn.get("name") == "web_search":
                            query = fn.get("arguments", {}).get("query", "")
                            search_queries.append(query)
                            yield _sse({"type": "search", "query": query})
                            results = web_search.search(query)
                            follow_up.append({"role": "tool", "content": web_search.format_results(results)})
                    async for chunk in ollama_client.stream_chat(client, {**payload, "messages": follow_up}):
                        if "_error" in chunk:
                            yield _sse({"type": "error", "detail": chunk["_error"]}); return
                        msg = chunk.get("message", {})
                        if msg.get("thinking"):
                            yield _sse({"type": "think", "content": msg["thinking"]})
                        token = msg.get("content", "")
                        if token:
                            reply_parts.append(token)
                            yield _sse({"type": "token", "content": token})
                        if chunk.get("done"): break
                else:
                    reply_parts = first_content

        except asyncio.CancelledError:
            _save_to_db(partial=True); return
        except httpx.ConnectError:
            yield _sse({"type": "error", "detail": f"Cannot reach Ollama at {config.OLLAMA_BASE_URL}."}); return
        except httpx.TimeoutException:
            yield _sse({"type": "error", "detail": "Ollama timed out."}); return

        if not "".join(reply_parts):
            yield _sse({"type": "error", "detail": "Ollama returned an empty reply."}); return
        _save_to_db()
        sources["web_searches"] = search_queries
        yield _sse({
            "type":        "sources",
            "documents":   sources_docs,
            "graph_nodes": sources_graph,
        })
        yield _sse({"type": "done", "conversation_id": conv_id, "model": model, "sources": sources,
                    "doc_ids": list(dict.fromkeys(doc_ids)), "has_client_docs": has_client_docs})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
