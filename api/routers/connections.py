"""
routers/connections.py — External system connection management.

Manages connection configurations for external systems (M-Files, etc.).
Configs are stored in the `connections` table as JSON blobs.
Credentials are stored only in the DB — never logged or returned to the UI
(password fields are stripped on GET responses).

Prefix: /connections
"""
import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config as cfg
import db
from connectors import mfiles as mfiles_connector
from connectors import mfiles_indexer
from connectors import sharepoint as sp_connector
from connectors import webdav as webdav_connector

log = logging.getLogger(__name__)
router = APIRouter(prefix="/connections")

# ── Known connection types ────────────────────────────────────────────────────

KNOWN_TYPES = {
    "mfiles": {
        "label":       "M-Files",
        "description": "M-Files document management system (REST API / MFWS)",
        "fields": [
            {"key": "host",     "label": "Host",          "type": "text",     "required": True},
            {"key": "vault",    "label": "Vault GUID",    "type": "text",     "required": True},
            {"key": "username", "label": "Username",      "type": "text",     "required": True},
            {"key": "password", "label": "Password",      "type": "password", "required": True},
            {"key": "use_ssl",  "label": "Use HTTPS",     "type": "bool",     "required": False},
            {"key": "port",     "label": "Port override", "type": "number",   "required": False},
        ],
    },
    "sharepoint": {
        "label":       "SharePoint",
        "description": "Microsoft SharePoint via the Graph API (OAuth 2.0 client credentials)",
        "fields": [
            {"key": "tenant_id",     "label": "Tenant ID",      "type": "text",     "required": True},
            {"key": "client_id",     "label": "Client ID",      "type": "text",     "required": True},
            {"key": "client_secret", "label": "Client Secret",  "type": "password", "required": True},
            {"key": "site_url",      "label": "Site URL",       "type": "text",     "required": True},
        ],
    },
    "webdav": {
        "label":       "WebDAV / REST",
        "description": "Generic WebDAV or REST file server (Basic, Bearer, or unauthenticated)",
        "fields": [
            {"key": "url",        "label": "Base URL",    "type": "text",     "required": True},
            {"key": "auth_type",  "label": "Auth type",   "type": "select",   "required": False,
             "options": ["none", "basic", "bearer"]},
            {"key": "username",   "label": "Username",    "type": "text",     "required": False},
            {"key": "password",   "label": "Password",    "type": "password", "required": False},
            {"key": "token",      "label": "Bearer token","type": "password", "required": False},
            {"key": "verify_ssl", "label": "Verify TLS",  "type": "bool",     "required": False},
        ],
    },
}


_SECRET_KEYS = {"password", "client_secret", "token"}


def _strip_secrets(cfg_dict: dict) -> dict:
    """Return config dict with password/secret/token fields replaced by a placeholder."""
    return {
        k: ("••••••••" if k in _SECRET_KEYS and v else v)
        for k, v in cfg_dict.items()
    }


# ── Pydantic models ───────────────────────────────────────────────────────────

class ConnectionUpsert(BaseModel):
    config: dict[str, Any]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_connections():
    """
    Return all known connection types with their current config and enabled state.
    Password fields are masked.
    """
    stored = {c["type"]: c for c in db.list_connections()}
    result = []
    for conn_type, meta in KNOWN_TYPES.items():
        stored_entry = stored.get(conn_type, {})
        result.append({
            "type":        conn_type,
            "label":       meta["label"],
            "description": meta["description"],
            "fields":      meta["fields"],
            "enabled":     bool(stored_entry.get("enabled", False)),
            "config":      _strip_secrets(stored_entry.get("config", {})),
        })
    return result


@router.put("/{conn_type}")
async def upsert_connection(conn_type: str, body: ConnectionUpsert):
    """
    Save (create or update) a connection configuration.

    The password field is preserved from the existing stored config if the
    caller sends the placeholder value, so the UI doesn't need to re-enter it.
    """
    if conn_type not in KNOWN_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown connection type '{conn_type}'.")

    new_cfg = dict(body.config)

    # Preserve existing secrets if the UI sent back the masked placeholder.
    existing_for_secrets = db.get_connection(conn_type)
    for secret_key in _SECRET_KEYS:
        if new_cfg.get(secret_key) == "••••••••" and existing_for_secrets:
            new_cfg[secret_key] = existing_for_secrets["config"].get(secret_key, "")

    with db.lock:
        existing = db.get_connection(conn_type)
        enabled  = bool(existing["enabled"]) if existing else False
        db.upsert_connection(conn_type, new_cfg, enabled=enabled)

    return {"ok": True, "type": conn_type}


@router.patch("/{conn_type}/enable")
async def enable_connection(conn_type: str):
    """Enable a connection."""
    if conn_type not in KNOWN_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown connection type '{conn_type}'.")
    with db.lock:
        if not db.get_connection(conn_type):
            raise HTTPException(status_code=409, detail="Connection not configured yet.")
        db.set_connection_enabled(conn_type, True)
    return {"ok": True, "type": conn_type, "enabled": True}


@router.patch("/{conn_type}/disable")
async def disable_connection(conn_type: str):
    """Disable a connection without deleting its config."""
    if conn_type not in KNOWN_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown connection type '{conn_type}'.")
    with db.lock:
        db.set_connection_enabled(conn_type, False)
    return {"ok": True, "type": conn_type, "enabled": False}


@router.post("/{conn_type}/test")
async def test_connection(conn_type: str):
    """
    Test connectivity for a stored connection.
    Returns {"ok": true, ...info} or {"ok": false, "error": "..."}.
    """
    if conn_type not in KNOWN_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown connection type '{conn_type}'.")

    with db.lock:
        entry = db.get_connection(conn_type)
    if not entry or not entry.get("config"):
        raise HTTPException(status_code=409, detail="Connection is not configured.")

    try:
        if conn_type == "mfiles":
            connector = mfiles_connector.from_config(entry["config"])
            info = await connector.test_connection()
            return {"ok": True, **info}
        if conn_type == "sharepoint":
            connector = sp_connector.from_config(entry["config"])
            info = await connector.test_connection()
            return {"ok": True, **info}
        if conn_type == "webdav":
            connector = webdav_connector.from_config(entry["config"])
            info = await connector.test_connection()
            return {"ok": True, **info}
        return {"ok": False, "error": f"No test implementation for '{conn_type}'."}
    except Exception as exc:
        log.warning("Connection test failed for %s: %s", conn_type, exc)
        return {"ok": False, "error": str(exc)}


@router.get("/{conn_type}/env-hint")
async def env_hint(conn_type: str):
    """
    Return whether environment variables are set for this connection type.
    Used by the UI to pre-fill configs from env vars on first use.
    """
    if conn_type == "mfiles":
        has_env = bool(cfg.MFILES_HOST and cfg.MFILES_VAULT and cfg.MFILES_USER)
        return {
            "has_env": has_env,
            "config": {
                "host":     cfg.MFILES_HOST,
                "vault":    cfg.MFILES_VAULT,
                "username": cfg.MFILES_USER,
                # Never expose password via API — user must enter it in the UI.
            } if has_env else {},
        }
    if conn_type == "sharepoint":
        has_env = bool(cfg.SP_TENANT_ID and cfg.SP_CLIENT_ID and cfg.SP_SITE_URL)
        return {
            "has_env": has_env,
            "config": {
                "tenant_id": cfg.SP_TENANT_ID,
                "client_id": cfg.SP_CLIENT_ID,
                "site_url":  cfg.SP_SITE_URL,
                # client_secret is never pre-filled.
            } if has_env else {},
        }
    if conn_type == "webdav":
        has_env = bool(cfg.WEBDAV_URL)
        return {
            "has_env": has_env,
            "config": {
                "url":      cfg.WEBDAV_URL,
                "username": cfg.WEBDAV_USERNAME,
            } if has_env else {},
        }
    return {"has_env": False, "config": {}}


@router.post("/mfiles/index", status_code=202)
async def start_mfiles_index(background_tasks: BackgroundTasks):
    """
    Trigger a full M-Files vault index in the background.

    Downloads all documents, parses text, ingests into ChromaDB, and records
    in library_items.  Progress is streamed via GET /connections/mfiles/index/stream.

    Returns 409 if an indexer run is already in progress.
    """
    with db.lock:
        entry = db.get_connection("mfiles")
    if not entry or not entry.get("config"):
        raise HTTPException(status_code=409, detail="M-Files connection is not configured.")
    if not entry.get("enabled"):
        raise HTTPException(status_code=409, detail="M-Files connection is not enabled.")
    if mfiles_indexer.is_active():
        raise HTTPException(status_code=409, detail="Indexer is already running.")

    background_tasks.add_task(mfiles_indexer.run_indexer)
    return {"ok": True, "message": "M-Files vault index started."}


@router.get("/mfiles/index/stream")
async def mfiles_index_stream():
    """
    Server-Sent Events stream for M-Files indexer progress.

    Events:
      {"type": "start",    "message": "..."}
      {"type": "progress", "message": "...", "indexed": n, "skipped": n}
      {"type": "file",     "filename": "...", "object_title": "..."}
      {"type": "complete", "indexed": n, "skipped": n, "errors": n}
      {"type": "error",    "error":   "..."}
    """
    async def generate():
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        mfiles_indexer._subscribers.append(q)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") in ("complete", "error"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                mfiles_indexer._subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )
