"""
app.py — FastAPI application entry point. Thin bootstrap only.
"""
import logging
import time as _time
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

_req_log = logging.getLogger("hexcaliper.requests")
_log = logging.getLogger("hexcaliper")

import config
import db
import rag
from routers import health, conversations, documents, library, chat, tech_library, acquisition, escalation, connections, system_prompts, status
from routers.documents import active_upload_snapshot

app = FastAPI(title="LanceLLMot API", version="4.0.0")

if config.CORS_ORIGINS == ["*"]:
    import logging as _logging
    _logging.getLogger(__name__).warning("CORS_ORIGINS is '*' — all origins allowed. Do not use in production.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "PATCH", "PUT"],
    allow_headers=["Content-Type"],
)


class LibraryModeMiddleware(BaseHTTPMiddleware):
    """
    Set request.state.library_mode = True when either:
      - PUBLIC_LIBRARY_MODE env var is true, or
      - the nginx X-Site-Mode: library header is present.
    """
    async def dispatch(self, request: Request, call_next):
        request.state.library_mode = (
            config.PUBLIC_LIBRARY_MODE
            or request.headers.get("X-Site-Mode", "").lower() == "library"
        )
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every HTTP request with method, path, status, duration, and user."""
    async def dispatch(self, request: Request, call_next):
        start = _time.monotonic()
        response = await call_next(request)
        ms = int(((_time.monotonic() - start)) * 1000)
        user = request.headers.get("CF-Access-Authenticated-User-Email", "anonymous")
        status = response.status_code
        msg = "%s %s %d %dms [%s]", request.method, request.url.path, status, ms, user
        if status >= 500:
            _req_log.error(*msg)
        elif status >= 400:
            _req_log.warning(*msg)
        else:
            _req_log.info(*msg)
        return response


app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(LibraryModeMiddleware)

app.include_router(health.router)
app.include_router(conversations.router)
app.include_router(documents.router)
app.include_router(library.router)       # /workspace    — clients & projects
app.include_router(tech_library.router)  # /library      — technical document store
app.include_router(acquisition.router)   # /acquisition  — acquisition queue + SSE
app.include_router(escalation.router)   # /escalation   — cloud escalation queue + SSE
app.include_router(connections.router)    # /connections  — external system connections
app.include_router(system_prompts.router) # /system-prompts — saved system prompts
app.include_router(status.router)         # /status         — cross-service status
app.include_router(chat.router)


@app.get("/activity")
async def activity():
    """Server-side activity state — currently in-progress uploads."""
    uploads = active_upload_snapshot()
    return {"uploads": uploads}


@app.get("/site-config")
async def site_config(request: Request):
    """Return runtime configuration flags consumed by the frontend."""
    return {"public_library_mode": request.state.library_mode}


@app.on_event("startup")
async def startup():
    db.conn()

    # Startup diagnostics: database integrity
    try:
        integrity = db.conn().execute("PRAGMA integrity_check").fetchone()
        if integrity and integrity[0] != "ok":
            _log.error("database integrity check failed: %s", integrity[0])
        else:
            _log.info("database integrity check passed")
    except Exception as exc:
        _log.error("database integrity check error: %s", exc)

    # Startup diagnostics: check Ollama reachability
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            r = await client.get(f"{config.OLLAMA_BASE_URL}/api/tags")
            models = len(r.json().get("models", []))
            _log.info("Ollama reachable (%d models)", models)
    except Exception as exc:
        _log.warning("Ollama unreachable at startup: %s", exc)

    db.migrate_from_tinydb()
    db.migrate_classification_column()
    db.migrate_library_source_column()
    db.migrate_credentials_encryption()
    db.migrate_system_prompt_id_column()
    rag.migrate_legacy_scopes()
