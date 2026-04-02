"""
connectors/mfiles_indexer.py — Background vault indexer for M-Files.

Downloads all documents from the configured M-Files vault, parses their
text, ingests them into ChromaDB, and records them in library_items.

Classification: all M-Files-sourced items are always ``classification='client'``
(they are never auto-escalatable to cloud models).

Supported file types: PDF, DOCX, XLSX, TXT, MD, ST, SCL.
SHA-256 dedup — files already in library_items by checksum are skipped.

SSE events emitted:
  {"type": "start",     "message": "..."}
  {"type": "progress",  "message": "...", "indexed": n, "skipped": n}
  {"type": "file",      "filename": "...", "object_title": "..."}
  {"type": "complete",  "indexed": n, "skipped": n, "errors": n}
  {"type": "error",     "error":   "..."}
"""
import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import config
import db
import parser
import rag
from connectors import mfiles as mfiles_connector

log = logging.getLogger(__name__)

# Supported file extensions (lowercase, no leading dot).
SUPPORTED_EXTENSIONS = {"pdf", "docx", "xlsx", "txt", "md", "st", "scl"}

# Vault object type 0 = Document.
OBJECT_TYPE_DOCUMENT = 0

# Objects fetched per API page.
PAGE_SIZE = 100

# Maximum single-file download size.
MAX_FILE_BYTES = 30 * 1024 * 1024

# ── SSE pub/sub ───────────────────────────────────────────────────────────────

_subscribers: list[asyncio.Queue] = []
_active = False          # True while an indexer run is in progress.


def is_active() -> bool:
    return _active


def _publish(event: dict) -> None:
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    """Return a filesystem-safe slug (max 64 chars)."""
    return re.sub(r"[^\w\-]", "_", text.strip())[:64].strip("_") or "unknown"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checksum_exists(checksum: str) -> bool:
    """Return True if a library item with this checksum already exists."""
    with db.lock:
        items = db.list_library_items()
    return any(i.get("checksum") == checksum for i in items)


def _ext(filename: str, extension: str) -> str:
    """
    Return *filename* with *.extension* appended if it doesn't already have one.
    """
    if "." in os.path.basename(filename):
        return filename
    return f"{filename}.{extension.lower().lstrip('.')}"


# ── Main indexer ──────────────────────────────────────────────────────────────

async def run_indexer() -> None:
    """
    Index the M-Files vault into library_items.

    Intended to be launched as a FastAPI BackgroundTask.
    """
    global _active
    if _active:
        _publish({"type": "error", "error": "Indexer is already running."})
        return

    _active = True
    _publish({"type": "start", "message": "Starting M-Files vault index…"})

    with db.lock:
        entry = db.get_connection("mfiles")
    if not entry or not entry.get("config"):
        _publish({"type": "error", "error": "M-Files connection is not configured."})
        _active = False
        return

    connector = mfiles_connector.from_config(entry["config"])

    indexed = 0
    skipped = 0
    errors  = 0
    offset  = 0

    try:
        while True:
            _publish({
                "type":    "progress",
                "message": f"Fetching objects (offset {offset})…",
                "indexed": indexed,
                "skipped": skipped,
            })

            try:
                objects = await connector.list_objects(
                    object_type=OBJECT_TYPE_DOCUMENT,
                    limit=PAGE_SIZE,
                    offset=offset,
                )
            except Exception as exc:
                _publish({"type": "error",
                          "error": f"Failed to list objects at offset {offset}: {exc}"})
                break

            if not objects:
                break

            for obj in objects:
                obj_id    = obj.get("id")
                obj_type  = obj.get("object_type", OBJECT_TYPE_DOCUMENT)
                obj_title = (obj.get("title") or str(obj_id)).strip()
                version   = obj.get("version", 0)

                if obj_id is None:
                    continue

                try:
                    files = await connector.get_object_files(
                        object_type=obj_type,
                        object_id=obj_id,
                        version=version,
                    )
                except Exception as exc:
                    log.warning("get_object_files failed for obj %s: %s", obj_id, exc)
                    errors += 1
                    continue

                for f in files:
                    ext = (f.get("extension") or "").lower().lstrip(".")
                    if ext not in SUPPORTED_EXTENSIONS:
                        continue

                    size = f.get("size", 0)
                    if size > MAX_FILE_BYTES:
                        log.info("Skipping %s — too large (%d bytes)", f.get("name"), size)
                        skipped += 1
                        continue

                    filename = _ext(f.get("name") or str(f["file_id"]), ext)

                    try:
                        data = await connector.download_file(
                            object_type=obj_type,
                            object_id=obj_id,
                            file_id=f["file_id"],
                            version=version,
                        )
                    except Exception as exc:
                        log.warning("Download failed for %s: %s", filename, exc)
                        errors += 1
                        continue

                    checksum = _sha256(data)
                    if _checksum_exists(checksum):
                        skipped += 1
                        continue

                    # Save to disk.
                    slug     = _slug(obj_title)
                    dest_dir = os.path.join(config.LIBRARY_PATH, "mfiles", slug)
                    os.makedirs(dest_dir, exist_ok=True)
                    filepath = os.path.join(dest_dir, filename)
                    with open(filepath, "wb") as fp:
                        fp.write(data)

                    # Parse text for RAG ingest.
                    text = parser.parse_file(filename, data)

                    # Record in library.
                    item_id = str(uuid.uuid4())
                    item = {
                        "id":           item_id,
                        "manufacturer": "M-Files",
                        "product_id":   slug,
                        "doc_type":     "misc",
                        "version":      str(version) if version else None,
                        "filename":     filename,
                        "filepath":     filepath,
                        "source_url":   None,
                        "checksum":     checksum,
                        "indexed":      0,
                        "source":       "mfiles",
                    }
                    with db.lock:
                        db.insert_library_item(item)

                    # Ingest into ChromaDB (library user, global scope).
                    if text:
                        await rag.ingest(
                            item_id, "library", text,
                            scope_type="global", scope_id=None,
                            title=filename, uploaded_at=_now_iso(),
                        )
                        with db.lock:
                            db.update_library_item(item_id, {"indexed": 1})

                    indexed += 1
                    _publish({
                        "type":         "file",
                        "filename":     filename,
                        "object_title": obj_title,
                    })

            if len(objects) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

    except Exception as exc:
        log.exception("M-Files vault indexer crashed")
        _publish({"type": "error", "error": str(exc)[:500]})
    finally:
        _active = False

    _publish({
        "type":    "complete",
        "indexed": indexed,
        "skipped": skipped,
        "errors":  errors,
    })
