import io
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import pypdf
import docx as python_docx
import rag
import web_fetch
import web_search
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, field_validator
from tinydb import TinyDB, Query

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "llama3:8b")
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "20000"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "120"))
MAX_DOC_BYTES = 20 * 1024 * 1024  # 20 MB

db = TinyDB("/app/data/db.json")
conversations_table = db.table("conversations")
documents_table = db.table("documents")
db_lock = threading.Lock()

app = FastAPI(title="Hexcaliper API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


# ── Helpers ───────────────────────────────────────────────────

def get_user_email(request: Request) -> str:
    return request.headers.get("cf-access-authenticated-user-email", "local@dev")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_file(filename: str, data: bytes) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if ext == "docx":
        doc = python_docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()
    return data.decode("utf-8", errors="replace").strip()


# ── Models ────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    model: Optional[str] = None
    system: Optional[str] = None
    conversation_id: Optional[str] = None

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message must not be empty")
        return v.strip()

    @field_validator("message")
    @classmethod
    def message_length(cls, v: str) -> str:
        if len(v) > MAX_INPUT_CHARS:
            raise ValueError(f"message exceeds {MAX_INPUT_CHARS} character limit")
        return v


class ChatResponse(BaseModel):
    model: str
    reply: str
    conversation_id: str
    sources: dict


# ── Web-search tool definition for Ollama tool-calling ────────

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information, recent events, news, prices, "
            "or anything that may not be in the model's training data. "
            "Use this whenever the user's question likely requires up-to-date facts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
            },
            "required": ["query"],
        },
    },
}


# ── GPU stats ─────────────────────────────────────────────────

def _gpu_stats() -> dict:
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode()
        return {
            "ok": True,
            "name": name,
            "gpu_util": util.gpu,
            "mem_used": mem.used,
            "mem_total": mem.total,
            "temperature": temp,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/gpu")
async def gpu():
    return _gpu_stats()


# ── Health ────────────────────────────────────────────────────

@app.get("/models")
async def models():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        resp.raise_for_status()
        names = [m["name"] for m in resp.json().get("models", [])
                 if not any(m["name"].startswith(p) for p in ("nomic-", "mxbai-", "all-minilm"))]
        return {"models": sorted(names)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot fetch models from Ollama: {exc}")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "ollama_base_url": OLLAMA_BASE_URL,
        "default_model": DEFAULT_MODEL,
        "max_input_chars": MAX_INPUT_CHARS,
        "request_timeout_seconds": REQUEST_TIMEOUT,
    }


# ── Conversations ─────────────────────────────────────────────

@app.get("/conversations")
async def list_conversations(request: Request):
    user_email = get_user_email(request)
    Conv = Query()
    with db_lock:
        docs = conversations_table.search(Conv.user_email == user_email)
    docs.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    return [
        {
            "id": d["id"],
            "title": d.get("title", "Untitled"),
            "model": d.get("model", DEFAULT_MODEL),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
        }
        for d in docs
    ]


@app.post("/conversations")
async def create_conversation(request: Request):
    user_email = get_user_email(request)
    conv_id = str(uuid.uuid4())
    ts = now_iso()
    doc = {
        "id": conv_id,
        "user_email": user_email,
        "title": "New Conversation",
        "model": DEFAULT_MODEL,
        "created_at": ts,
        "updated_at": ts,
        "messages": [],
    }
    with db_lock:
        conversations_table.insert(doc)
    return {"id": conv_id, "title": doc["title"], "created_at": ts, "updated_at": ts}


@app.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, request: Request):
    user_email = get_user_email(request)
    Conv = Query()
    with db_lock:
        docs = conversations_table.search(Conv.id == conv_id)
    if not docs:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    doc = docs[0]
    if doc["user_email"] != user_email:
        raise HTTPException(status_code=403, detail="Access denied.")
    return doc


@app.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, request: Request):
    user_email = get_user_email(request)
    Conv = Query()
    with db_lock:
        docs = conversations_table.search(Conv.id == conv_id)
        if not docs:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if docs[0]["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        conversations_table.remove(Conv.id == conv_id)
    return Response(status_code=204)


# ── Documents ─────────────────────────────────────────────────

@app.get("/documents")
async def list_documents(request: Request):
    user_email = get_user_email(request)
    Doc = Query()
    with db_lock:
        docs = documents_table.search(Doc.user_email == user_email)
    docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return [
        {
            "id": d["id"],
            "filename": d.get("filename", "unknown"),
            "size_bytes": d.get("size_bytes", 0),
            "chunk_count": d.get("chunk_count", 0),
            "created_at": d.get("created_at"),
        }
        for d in docs
    ]


@app.post("/documents")
async def upload_document(request: Request, file: UploadFile = File(...)):
    user_email = get_user_email(request)
    data = await file.read()

    if len(data) > MAX_DOC_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB).")

    filename = file.filename or "upload.txt"
    text = parse_file(filename, data)
    if not text:
        raise HTTPException(status_code=422, detail="Could not extract text from file.")

    doc_id = str(uuid.uuid4())
    chunk_count = await rag.ingest(doc_id, user_email, text)

    ts = now_iso()
    meta = {
        "id": doc_id,
        "user_email": user_email,
        "filename": filename,
        "size_bytes": len(data),
        "chunk_count": chunk_count,
        "created_at": ts,
    }
    with db_lock:
        documents_table.insert(meta)

    return meta


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, request: Request):
    user_email = get_user_email(request)
    Doc = Query()
    with db_lock:
        docs = documents_table.search(Doc.id == doc_id)
        if not docs:
            raise HTTPException(status_code=404, detail="Document not found.")
        if docs[0]["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        documents_table.remove(Doc.id == doc_id)
    rag.delete_chunks(doc_id)
    return Response(status_code=204)


# ── Chat (streaming SSE) ──────────────────────────────────────

def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    user_email = get_user_email(request)
    model = (req.model or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    Conv = Query()

    # Load or create conversation — raise synchronously before streaming starts
    with db_lock:
        if req.conversation_id:
            docs = conversations_table.search(Conv.id == req.conversation_id)
            if not docs:
                raise HTTPException(status_code=404, detail="Conversation not found.")
            if docs[0]["user_email"] != user_email:
                raise HTTPException(status_code=403, detail="Access denied.")
            conv_id = docs[0]["id"]
            history = list(docs[0].get("messages", []))
        else:
            conv_id = str(uuid.uuid4())
            ts = now_iso()
            conversations_table.insert({
                "id": conv_id,
                "user_email": user_email,
                "title": req.message[:60],
                "model": model,
                "created_at": ts,
                "updated_at": ts,
                "messages": [],
            })
            history = []

    # Gather context (best-effort)
    try:
        url_context = await web_fetch.fetch_context(req.message)
    except Exception:
        url_context = {}

    try:
        doc_chunks = await rag.search(user_email, req.message)
    except Exception:
        doc_chunks = []

    # Build Ollama message list
    messages = []
    if req.system:
        messages.append({"role": "system", "content": req.system.strip()})

    context_parts = []
    if doc_chunks:
        context_parts.append(
            "Relevant information from the user's documents:\n\n"
            + "\n\n---\n\n".join(doc_chunks)
        )
    for url, content in url_context.items():
        context_parts.append(f"Content fetched from {url}:\n\n{content}")
    if context_parts:
        messages.append({"role": "system", "content": "\n\n===\n\n".join(context_parts)})

    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": req.message})

    _THINKING_MODELS = ("deepseek-r1", "deepseek-r2", "qwq", "marco-o1")
    supports_think = any(t in model.lower() for t in _THINKING_MODELS)
    payload = {"model": model, "stream": True, "messages": messages}
    if supports_think:
        payload["think"] = True
    sources = {"doc_chunks": len(doc_chunks), "urls": list(url_context.keys())}

    async def generate():
        reply_parts: list[str] = []
        search_queries: list[str] = []

        async def _stream_ollama(client: httpx.AsyncClient, pl: dict):
            """Yield (chunk_dict, line) from an Ollama streaming call."""
            async with client.stream("POST", f"{OLLAMA_BASE_URL}/api/chat", json=pl) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield {"_error": f"Ollama {resp.status_code}: {body[:200].decode()}"}
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                # ── First pass: offer web_search tool ─────────────────
                first_payload = {**payload, "tools": [WEB_SEARCH_TOOL]}
                tool_calls_received: list[dict] = []
                first_content: list[str] = []

                async for chunk in _stream_ollama(client, first_payload):
                    if "_error" in chunk:
                        # Model doesn't support tools — retry without them
                        if "does not support tools" in chunk["_error"]:
                            async for chunk2 in _stream_ollama(client, payload):
                                if "_error" in chunk2:
                                    yield _sse({"type": "error", "detail": chunk2["_error"]})
                                    return
                                msg2 = chunk2.get("message", {})
                                if msg2.get("thinking"):
                                    yield _sse({"type": "think", "content": msg2["thinking"]})
                                token2 = msg2.get("content", "")
                                if token2:
                                    first_content.append(token2)
                                    yield _sse({"type": "token", "content": token2})
                                if chunk2.get("done"):
                                    break
                            break
                        yield _sse({"type": "error", "detail": chunk["_error"]})
                        return
                    msg = chunk.get("message", {})
                    if msg.get("thinking"):
                        yield _sse({"type": "think", "content": msg["thinking"]})
                    if msg.get("tool_calls"):
                        tool_calls_received = msg["tool_calls"]
                    token = msg.get("content", "")
                    if token:
                        first_content.append(token)
                        yield _sse({"type": "token", "content": token})
                    if chunk.get("done"):
                        break

                if tool_calls_received:
                    # ── Tool-call branch: execute search, stream follow-up ─
                    follow_up_messages = list(messages)
                    follow_up_messages.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": tool_calls_received,
                    })

                    for tc in tool_calls_received:
                        fn = tc.get("function", {})
                        if fn.get("name") == "web_search":
                            query = fn.get("arguments", {}).get("query", "")
                            search_queries.append(query)
                            yield _sse({"type": "search", "query": query})
                            results = web_search.search(query)
                            follow_up_messages.append({
                                "role": "tool",
                                "content": web_search.format_results(results),
                            })

                    second_payload = {**payload, "messages": follow_up_messages}
                    async for chunk in _stream_ollama(client, second_payload):
                        if "_error" in chunk:
                            yield _sse({"type": "error", "detail": chunk["_error"]})
                            return
                        msg = chunk.get("message", {})
                        if msg.get("thinking"):
                            yield _sse({"type": "think", "content": msg["thinking"]})
                        token = msg.get("content", "")
                        if token:
                            reply_parts.append(token)
                            yield _sse({"type": "token", "content": token})
                        if chunk.get("done"):
                            break
                else:
                    # No tool call — first pass content is the reply
                    reply_parts = first_content

        except httpx.ConnectError:
            yield _sse({"type": "error", "detail": f"Cannot reach Ollama at {OLLAMA_BASE_URL}."})
            return
        except httpx.TimeoutException:
            yield _sse({"type": "error", "detail": "Ollama timed out."})
            return

        reply_text = "".join(reply_parts)
        if not reply_text:
            yield _sse({"type": "error", "detail": "Ollama returned an empty reply."})
            return

        # Persist to DB
        ts_now = now_iso()
        with db_lock:
            docs = conversations_table.search(Conv.id == conv_id)
            if docs:
                updated = list(docs[0].get("messages", []))
                updated.append({"role": "user", "content": req.message, "ts": ts_now})
                updated.append({"role": "assistant", "content": reply_text, "ts": ts_now})
                update_fields = {"messages": updated, "updated_at": ts_now, "model": model}
                if not req.conversation_id:
                    update_fields["title"] = req.message[:60]
                conversations_table.update(update_fields, Conv.id == conv_id)

        sources["web_searches"] = search_queries
        yield _sse({"type": "done", "conversation_id": conv_id, "model": model, "sources": sources})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
