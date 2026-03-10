import asyncio
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
import openpyxl
import copyright_extract
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

# ── Startup migration ──────────────────────────────────────────
# Tag pre-scope TinyDB docs and ChromaDB chunks as "global" (idempotent).
_Doc = Query()
with db_lock:
    for _d in documents_table.search(~_Doc.scope.exists()):
        documents_table.update({"scope": "global"}, _Doc.id == _d["id"])
rag.migrate_legacy_scopes()

app = FastAPI(title="Hexcaliper API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080"],
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["Content-Type"],
)


# ── Helpers ───────────────────────────────────────────────────

def get_user_email(request: Request) -> str:
    """
    Retrieve the email address of an authenticated user from the request headers.

    This function accesses the "cf-access-authenticated-user-email" header in the
    provided request object to extract the email address of the authenticated user.
    If the header is not present, it returns a default value of "local@dev".

    :param request: The incoming HTTP request object that contains headers.
    :type request: Request

    :return: The email address of the authenticated user or a default email if the
             specific header is not found.
    :rtype: str
    """
    return request.headers.get("cf-access-authenticated-user-email", "local@dev")


def now_iso() -> str:
    """
    Converts the current UTC date and time to an ISO 8601 formatted string.

    This function retrieves the current date and time in UTC timezone using
    the `datetime.now` method and converts it into an ISO 8601 standard
    representation using the `isoformat` method.

    :return: The current UTC date and time in ISO 8601 string format
    :rtype: str
    """
    return datetime.now(timezone.utc).isoformat()


async def summarize_document(text: str) -> str:
    """
    Summarizes a given document by extracting its main topic and purpose in 2-3 sentences.

    :param text: The input document text to be summarized.
    :type text: str
    :return: A string containing the summarized content of the document. Returns an empty string if
        the summarization fails.
    :rtype: str
    :raises httpx.HTTPStatusError: If the HTTP request to the summarization endpoint fails with a bad status.
    """
    sample = text[:6000]
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": DEFAULT_MODEL,
                    "stream": False,
                    "messages": [{
                        "role": "user",
                        "content": (
                            "Summarize this document in 2-3 sentences, "
                            "focusing on its main topic and purpose:\n\n" + sample
                        ),
                    }],
                },
            )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    except Exception:
        return ""


def parse_file(filename: str, data: bytes) -> str:
    """
    Parses the content of a file based on its extension and returns the text representation.

    Supports multiple file formats including PDF, DOCX, XLSX/XLS, and plain text. For PDF
    files, it extracts the text from all pages. For DOCX files, it retrieves text from
    paragraphs. XLSX or XLS files are converted into a textual tabular format by processing
    each sheet and row. If the file extension is unsupported, it assumes plain text.

    :param filename: Name of the file, including the extension.
    :param data: File content in bytes.
    :return: Extracted text content from the file as a string.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if ext == "docx":
        doc = python_docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()
    if ext in ("xlsx", "xls"):
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        lines = []
        for sheet in wb.worksheets:
            lines.append(f"[Sheet: {sheet.title}]")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    lines.append("\t".join(cells))
        return "\n".join(lines).strip()
    return data.decode("utf-8", errors="replace").strip()


# ── Models ────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    """
    Represents a chat request containing a user message and optional context.

    This class is used to encapsulate the user's chatbot input message, as well as
    optional attributes such as the model to be used, the system that provides context
    for generating responses, and an optional conversation ID. The validation ensures that
    the message provided is not empty and falls within the allowed character limit.

    :ivar message: The input message from the user for the chat session.
    :type message: str
    :ivar model: The optional model identifier for processing the chat request.
    :type model: Optional[str]
    :ivar system: Optional system specifying additional context for the chat response.
    :type system: Optional[str]
    :ivar conversation_id: Optional identifier for tracking the conversation thread.
    :type conversation_id: Optional[str]
    """
    message: str
    model: Optional[str] = None
    system: Optional[str] = None
    conversation_id: Optional[str] = None

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        """
        Validates that a provided message string is not empty or consists only of
        whitespace. This method ensures the message is meaningful and returns the
        stripped version of the string if valid.

        :param v: The message string to validate.
        :type v: str
        :return: A stripped version of the validated message string.
        :rtype: str
        :raises ValueError: If the message string is empty or only contains whitespace.
        """
        if not v or not v.strip():
            raise ValueError("message must not be empty")
        return v.strip()

    @field_validator("message")
    @classmethod
    def message_length(cls, v: str) -> str:
        """
        Validates the length of a message field to ensure it does not exceed a maximum
        character limit. If the message length exceeds the defined limit, a ValueError
        is raised.

        :param v: The input string to validate.
        :type v: str

        :raises ValueError: If the length of the input string exceeds the maximum
            allowed number of characters.

        :return: The input string, if it passes the validation.
        :rtype: str
        """
        if len(v) > MAX_INPUT_CHARS:
            raise ValueError(f"message exceeds {MAX_INPUT_CHARS} character limit")
        return v


class ChatResponse(BaseModel):
    """
    Represents a response in a chat system.

    This class encapsulates the essential details of a chat response, including the
    model generating the response, the actual reply content, the conversation context,
    and any associated sources that provide additional information.

    :ivar model: The name or identifier of the chat model that generated the reply.
    :type model: str
    :ivar reply: The content of the reply generated by the chat model.
    :type reply: str
    :ivar conversation_id: The unique identifier for the conversation to which this
        response belongs.
    :type conversation_id: str
    :ivar sources: A dictionary containing additional sources or metadata associated
        with the response.
    :type sources: dict
    """
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
            "Search the web for current information, recent events, news, or prices "
            "that are not in the model's training data. "
            "Use this ONLY when the answer cannot be found in the conversation, "
            "the user's uploaded documents, or the model's existing knowledge. "
            "Do NOT use this for questions about documents the user has already uploaded."
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
    """
    Retrieves GPU statistics using NVIDIA Management Library (NVML). The function returns
    GPU utilization, memory usage, temperature, and GPU name. If an exception occurs
    (e.g., NVML is not available, or initialization fails), it returns an error message
    instead.

    :return: A dictionary containing the GPU information. The dictionary includes the
        following keys:
        - ok (bool): Indicates whether the operation was successful.
        - name (str): The name of the GPU if the operation was successful.
        - gpu_util (int): GPU utilization percentage if the operation was successful.
        - mem_used (int): Memory used in bytes if the operation was successful.
        - mem_total (int): Total memory in bytes if the operation was successful.
        - temperature (int): GPU temperature in Celsius if the operation was successful.
        - error (str): Error message if the operation was unsuccessful.
    :rtype: dict
    """
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
    """
    Handles the GET request to the "/gpu" endpoint and fetches GPU statistics.

    This asynchronous function serves as a route handler for retrieving GPU statistics
    by calling an internal utility function `_gpu_stats`.

    :return: A response containing the GPU statistics.
    :rtype: dict
    """
    return _gpu_stats()


@app.get("/model-status")
async def model_status(model: str = ""):
    """
    Fetches and returns the status of a specified machine learning model. The endpoint checks
    if the model is currently loaded and lists other active models. If an error occurs during
    the process, it returns a status indicating the model is not loaded.

    :param model: The name of the machine learning model to check. Defaults to an empty string.
    :type model: str

    :return: A dictionary containing the status of the specified model (whether it is loaded
        or not), and a list of other models currently active.
    :rtype: dict
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/ps")
        resp.raise_for_status()
        running = resp.json().get("models", [])
        loaded = any(m.get("name") == model or m.get("model") == model for m in running)
        active = [m.get("name") or m.get("model") for m in running if m.get("name") != model and m.get("model") != model]
        return {"model": model, "loaded": loaded, "active": active}
    except Exception:
        return {"model": model, "loaded": False}


@app.post("/warm-model")
async def warm_model(request: Request):
    """
    Handles the warm-up process for a specified model by sending a request to the model
    generation API. The endpoint ensures the model name is provided and communicates
    with the external service to trigger the model warm-up.

    :param request: The incoming request containing the JSON body with the required
        `model` key for specifying the model to warm up.
    :type request: Request
    :return: A dictionary indicating the success of the warm-up operation and the
        model that was processed.
    :rtype: dict
    :raises HTTPException: If the `model` is missing from the request body.
    :raises HTTPException: If unable to complete the warm-up process due to an
        error communicating with the model API.
    """
    body = await request.json()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={"model": model, "prompt": "", "keep_alive": "10m"},
            )
        resp.raise_for_status()
        return {"ok": True, "model": model}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load model: {exc}")




# ── Health ────────────────────────────────────────────────────

@app.get("/models")
async def models():
    """
    Fetches a list of model names from the Ollama API, excluding any models whose names
    start with predefined prefixes such as 'nomic-', 'mxbai-', or 'all-minilm'.

    The data is fetched asynchronously using an HTTP client with a specified timeout. Only
    valid model names are included, and the result is sorted alphabetically before being
    returned to the client. If the fetch operation fails or experiences an error, an HTTP
    exception is raised with a 502 status code and an appropriate error message.

    :raises HTTPException: Raised with a status code of 502 if there is any failure in
        fetching model data from the Ollama API.
    :return: A dictionary containing a sorted list of model names under the key "models".
    :rtype: dict
    """
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
    """
    Handles the health status endpoint for the API.

    This function is an asynchronous HTTP GET endpoint that provides health
    and configuration status of the application. It returns a dictionary
    containing the application's health status, base URL, default model
    used, input character limit, and request timeout duration.

    :return: A dictionary containing health status and configuration details
    :rtype: dict
    """
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
    """
    Handles the retrieval of conversation records associated with the authenticated user.

    This function fetches conversations from the database, filters them by the user's email,
    and returns a sorted list of conversations based on their update timestamps. Each conversation
    in the result includes specific relevant details.

    :param request: The HTTP request instance containing user authentication and context.
    :type request: Request
    :return: A list of dictionaries, each representing a conversation record with attributes
             such as id, title, model, created_at, and updated_at, sorted by updated_at in descending order.
    :rtype: list[dict]
    """
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
    """
    Handles the creation of a new conversation and stores it in the database.

    A unique conversation ID is generated, and initial metadata such as the user's email,
    default title, associated model, and timestamps are recorded. The new conversation
    is then inserted into the database and the conversation details are returned.

    :param request: The HTTP request object containing authentication and context information.
    :type request: Request

    :return: A dictionary containing the newly created conversation's ID, title, creation
             timestamp, and update timestamp.
    :rtype: dict
    """
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
    """
    Retrieve a specific conversation by its ID.

    This endpoint fetches a conversation document from the database using the provided
    conversation ID. It verifies the request origin and checks that the conversation
    belongs to the authenticated user, ensuring security and data privacy. If the
    conversation does not exist or access is denied, it raises appropriate HTTP errors.

    :param conv_id: The unique identifier of the conversation to retrieve.
    :type conv_id: str
    :param request: The HTTP request object, used to extract user authentication details.
    :type request: Request
    :return: The conversation document if found and accessible by the requesting user.
    :rtype: dict
    :raises HTTPException: Raises 404 if the conversation is not found. Raises 403 if
        the user does not have access to the conversation.
    """
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
    """
    Deletes a conversation by its identifier and removes associated documents. This
    endpoint ensures that only the authorized user can delete the conversation and
    its related resources such as documents.

    :param conv_id: The unique identifier of the conversation to delete.
    :type conv_id: str
    :param request: The request object containing metadata about the current HTTP request.
    :type request: Request
    :return: An empty response with a 204 No Content status code.
    :rtype: Response
    :raises HTTPException: If the conversation is not found or if the user is not
        authorized to delete the conversation.
    """
    user_email = get_user_email(request)
    Conv = Query()
    with db_lock:
        docs = conversations_table.search(Conv.id == conv_id)
        if not docs:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if docs[0]["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        conversations_table.remove(Conv.id == conv_id)

    # Delete any documents scoped to this conversation
    scope = f"conversation:{conv_id}"
    Doc = Query()
    with db_lock:
        scoped_docs = documents_table.search(Doc.scope == scope)
        documents_table.remove(Doc.scope == scope)
    for d in scoped_docs:
        rag.delete_chunks(d["id"])

    return Response(status_code=204)


@app.patch("/conversations/{conv_id}")
async def rename_conversation(conv_id: str, request: Request):
    """
    Rename a conversation by updating its title.

    :param conv_id: The ID of the conversation to rename.
    :type conv_id: str
    :param request: The HTTP request containing a JSON body with a ``title`` key.
    :type request: Request
    :return: A dict with ``id`` and the new ``title``.
    :rtype: dict
    :raises HTTPException: 400 if title is missing/empty, 404 if not found, 403 if access denied.
    """
    user_email = get_user_email(request)
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    Conv = Query()
    with db_lock:
        docs = conversations_table.search(Conv.id == conv_id)
        if not docs:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if docs[0]["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        conversations_table.update({"title": title, "updated_at": now_iso()}, Conv.id == conv_id)
    return {"id": conv_id, "title": title}


# ── Documents ─────────────────────────────────────────────────

@app.get("/documents")
async def list_documents(request: Request, conversation_id: Optional[str] = None):
    """
    Retrieve a list of documents associated with a user.

    This endpoint fetches documents based on the provided conversation scope or
    falls back to a global scope. The documents list is sorted in reverse
    chronological order based on the creation date.

    :param request: The incoming Python `Request` object, which provides details
        about the HTTP request.
    :param conversation_id: (Optional) Identifier of the conversation used to scope
        the documents. If not provided, the global scope is used.
    :return: A list of dictionaries containing document details. Each dictionary includes
        the document ID, filename, size in bytes, chunk count, creation timestamp,
        and scope.
    """
    user_email = get_user_email(request)
    Doc = Query()
    scope = f"conversation:{conversation_id}" if conversation_id else "global"
    with db_lock:
        docs = documents_table.search(
            (Doc.user_email == user_email) & (Doc.scope == scope)
        )
    docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return [
        {
            "id": d["id"],
            "filename": d.get("filename", "unknown"),
            "size_bytes": d.get("size_bytes", 0),
            "chunk_count": d.get("chunk_count", 0),
            "created_at": d.get("created_at"),
            "scope": d.get("scope", "global"),
            "summary": d.get("summary", ""),
        }
        for d in docs
    ]


@app.post("/documents")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    conversation_id: Optional[str] = None,
):
    """
    Upload a document, process its content, and store metadata in the database.

    This function uploads a document file, extracts its content, summarizes it, and
    stores necessary metadata, including copyright notices, into the database. It
    is optimized to handle embedding and summarization processes efficiently to
    prevent model swapping, which can cause timeouts. Files exceeding the maximum
    allowed size are rejected, and unsupported or corrupted files that cannot have
    their text extracted are also disallowed.

    :param request: The incoming HTTP request containing user and file data.
    :type request: Request
    :param file: The file to be uploaded. Must be a valid document in supported formats.
    :type file: UploadFile
    :param conversation_id: An optional conversation ID for scoping document processing.
      If not provided, the scope defaults to "global".
    :type conversation_id: Optional[str]
    :return: A dictionary containing metadata about the uploaded document, including
      its unique ID, user email, filename, size, processing summary, and copyright
      notices extracted from its content.
    :rtype: dict
    :raises HTTPException: Raised with status code 413 if the file exceeds the maximum
      allowed size. Raised with status code 422 if text extraction from the file fails.
    """
    user_email = get_user_email(request)
    data = await file.read()

    if len(data) > MAX_DOC_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB).")

    filename = file.filename or "upload.txt"
    text = parse_file(filename, data)
    if not text:
        raise HTTPException(status_code=422, detail="Could not extract text from file.")

    scope = f"conversation:{conversation_id}" if conversation_id else "global"
    doc_id = str(uuid.uuid4())
    # Ingest first so the embedding model finishes before the chat model is loaded for summarization.
    # Running them concurrently forces Ollama to swap models repeatedly, causing timeouts.
    chunk_count = await rag.ingest(doc_id, user_email, text, scope=scope)
    summary, notices = await asyncio.gather(
        summarize_document(text),
        asyncio.to_thread(copyright_extract.extract, text),
    )

    ts = now_iso()
    meta = {
        "id": doc_id,
        "user_email": user_email,
        "filename": filename,
        "size_bytes": len(data),
        "chunk_count": chunk_count,
        "created_at": ts,
        "scope": scope,
        "summary": summary,
        "copyright_notices": notices,
    }
    with db_lock:
        documents_table.insert(meta)

    return meta


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, request: Request):
    """
    Deletes a document specified by its ID. Ensures that the document belongs to the user
    making the request and removes the document from the database. Associated chunks
    are also deleted. This endpoint responds with a successful status if the operation
    is completed or raises appropriate exceptions for errors (e.g., document not found,
    access denied).

    :param doc_id: The unique identifier of the document to be deleted
    :type doc_id: str
    :param request: The HTTP request object, used to authenticate the user
      and fetch user-related details
    :type request: Request
    :return: An HTTP response indicating the success of the deletion process
    :rtype: Response
    :raises HTTPException: If the document is not found (404) or access is denied (403)
    """
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
    """
    Formats an event dictionary into a Server-Sent Events (SSE) data string.

    The function takes a dictionary representation of an event and converts it into a
    string suitable for Server-Sent Events (SSE) communication by encoding the dictionary
    in JSON format and adding the appropriate `data` prefix and line termination.

    :param event: A dictionary containing event data to be formatted for SSE.
    :type event: dict
    :return: A string formatted according to the Server-Sent Events (SSE) specification.
    :rtype: str
    """
    return f"data: {json.dumps(event)}\n\n"


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """
    Handles chat conversations by processing user requests, retrieving relevant
    context, and streaming AI-generated responses based on the conversation history
    and user-provided inputs. Manages state of conversations, includes logic for
    document relevance retrieval, and enforces access control.

    :param req: The user-provided input intended for the chat system, containing
        the conversation ID (optional), message, and other parameters.
        :type req: ChatRequest
    :param request: The HTTP request object, used to extract headers, cookies, or
        associated metadata like user authentication details.
        :type request: Request

    :return: A streaming response that contains AI-generated chat responses,
        additional retrieved context, and any system-level notices.
    :rtype: Streaming response object appropriate for asynchronous streaming.

    :raises HTTPException: If the conversation specified by the user ID is not found
        or if access to it is unauthorized.
    """
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
        doc_chunks, doc_ids = await rag.search(user_email, req.message, conversation_id=conv_id)
    except Exception:
        doc_chunks, doc_ids = [], []

    # Collect all available documents for this user+conversation (for awareness listing)
    _DocQ = Query()
    scope_filter = (
        (_DocQ.user_email == user_email)
        & ((_DocQ.scope == "global") | (_DocQ.scope == f"conversation:{conv_id}"))
    )
    with db_lock:
        all_docs = documents_table.search(scope_filter)

    # Collect copyright notices from the matched documents
    doc_copyright_notices: list[str] = []
    if doc_ids:
        with db_lock:
            for _did in dict.fromkeys(doc_ids):  # unique, order-preserving
                _rows = documents_table.search(_DocQ.id == _did)
                if _rows:
                    doc_copyright_notices.extend(_rows[0].get("copyright_notices") or [])

    # Build Ollama message list
    messages = []
    if req.system:
        messages.append({"role": "system", "content": req.system.strip()})

    # Always tell the model what documents are available, even when RAG found no chunks
    if all_docs:
        doc_lines = []
        for d in all_docs:
            line = f"  • {d['filename']}"
            if d.get("summary"):
                line += f": {d['summary']}"
            doc_lines.append(line)
        messages.append({"role": "system", "content": (
            "The user has uploaded the following documents:\n"
            + "\n".join(doc_lines)
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
        context_parts.append(
            notice_block
            + "\n\nRelevant information from the user's documents:\n\n"
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
        """
        Asynchronously generates a response by interacting with the Ollama API, performing web searches
        when required, and streaming intermediate results.

        This function communicates with the Ollama API to generate conversation messages, supports
        tool-based requests such as web searches, and streams intermediate and final responses. It
        manages error handling, tool integration, and persistence of conversation data.

        :yield: A dictionary containing various response types such as warnings, error details,
                search query indications, processing tokens, and the final completion information.

        :raises httpx.ConnectError: If the connection to the Ollama API cannot be established.
        :raises httpx.TimeoutException: If the request to the Ollama API times out.
        """
        reply_parts: list[str] = []
        search_queries: list[str] = []

        _saved = False

        def _save_to_db(partial: bool = False) -> None:
            """Persist accumulated reply to DB; idempotent via _saved flag."""
            nonlocal _saved
            if _saved:
                return
            text = "".join(reply_parts)
            if not text:
                return
            _saved = True
            suffix = " *(response interrupted)*" if partial else ""
            ts_now = now_iso()
            with db_lock:
                rows = conversations_table.search(Conv.id == conv_id)
                if rows:
                    updated = list(rows[0].get("messages", []))
                    updated.append({"role": "user", "content": req.message, "ts": ts_now})
                    updated.append({"role": "assistant", "content": text + suffix, "ts": ts_now})
                    update_fields = {"messages": updated, "updated_at": ts_now, "model": model}
                    if not req.conversation_id:
                        update_fields["title"] = req.message[:60]
                    conversations_table.update(update_fields, Conv.id == conv_id)

        async def _stream_ollama(client: httpx.AsyncClient, pl: dict):
            """
            Streams data asynchronously from the Ollama chat API using the given HTTP client and payload.

            This coroutine sends a POST request to the Ollama chat API with the provided payload, leveraging
            HTTP streaming to process responses line-by-line. If the request is unsuccessful, an error object
            is yielded. Otherwise, valid lines of the response are parsed as JSON and yielded iteratively.

            :param client: The asynchronous HTTP client (`httpx.AsyncClient`) to be used for making the request.
            :param pl: The payload to send in the POST request as a dictionary.

            :return: Yields parsed JSON objects line-by-line from the API response if available. If an error
                occurs, an error dictionary containing the response code and part of the response body is yielded.
            """
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
                        # Model doesn't support tools — notify client and retry without them
                        if "does not support tools" in chunk["_error"]:
                            yield _sse({"type": "warning", "detail": f"{model} does not support web search (tool calling). Responding without it."})
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

        except asyncio.CancelledError:
            # Client disconnected mid-stream — persist whatever was received.
            _save_to_db(partial=True)
            return
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

        _save_to_db(partial=False)

        sources["web_searches"] = search_queries
        yield _sse({"type": "done", "conversation_id": conv_id, "model": model, "sources": sources})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
