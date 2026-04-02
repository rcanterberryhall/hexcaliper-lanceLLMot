"""
app.py — FastAPI application entry point. Thin bootstrap only.
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

import config
import db
import rag
from routers import health, conversations, documents, library, chat, tech_library, acquisition, escalation, connections, system_prompts

app = FastAPI(title="Hexcaliper API", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://localhost:8081"],
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
app.include_router(chat.router)


@app.get("/site-config")
async def site_config(request: Request):
    """Return runtime configuration flags consumed by the frontend."""
    return {"public_library_mode": request.state.library_mode}


@app.on_event("startup")
async def startup():
    db.conn()
    db.migrate_from_tinydb()
    db.migrate_classification_column()
    db.migrate_library_source_column()
    db.migrate_credentials_encryption()
    db.migrate_system_prompt_id_column()
    rag.migrate_legacy_scopes()
