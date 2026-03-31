"""
app.py — FastAPI application entry point. Thin bootstrap only.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import db
import rag
from routers import health, conversations, documents, library, chat

app = FastAPI(title="Hexcaliper API", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080"],
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["Content-Type"],
)

app.include_router(health.router)
app.include_router(conversations.router)
app.include_router(documents.router)
app.include_router(library.router)
app.include_router(chat.router)


@app.on_event("startup")
async def startup():
    db.conn()
    db.migrate_from_tinydb()
    rag.migrate_legacy_scopes()
