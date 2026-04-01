"""
routers/tech_library.py — Technical document library.

All documents here are ``classification: public`` (manufacturer manuals,
datasheets, firmware notes, standards).  They are acquired automatically
by scrapers or added manually, and are indexed into ChromaDB for RAG.

In public library mode (request.state.library_mode = True):
  - M-Files-sourced items are hidden (source='mfiles' are client documents)
  - Add and delete operations return 403

Prefix: /library
"""
import os
import uuid
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import config
import db

router = APIRouter(prefix="/library")


class LibraryItemIn(BaseModel):
    source:     str            # organisation / standards body / manufacturer / client
    reference:  Optional[str] = None  # product ID, standard number, etc. — optional
    doc_type:   str
    version:    Optional[str] = None
    filename:   str
    source_url: Optional[str] = None


def _safe_path_component(s: str) -> str:
    """Lowercase, replace whitespace and path separators with underscores."""
    import re
    return re.sub(r'[\s/\\:]+', '_', s.strip()).lower()


# ── Listing ───────────────────────────────────────────────────────────────────

@router.get("/items")
async def list_items(
    request:   Request,
    source:    Optional[str] = None,
    reference: Optional[str] = None,
    doc_type:  Optional[str] = None,
):
    """Return all library items, optionally filtered."""
    library_mode = getattr(request.state, "library_mode", False)
    with db.lock:
        items = db.list_library_items(
            manufacturer=source,
            product_id=reference,
            doc_type=doc_type,
            public_only=library_mode,
        )
    return items


@router.get("/sources")
async def list_sources(request: Request):
    """Return distinct sources with doc counts."""
    library_mode = getattr(request.state, "library_mode", False)
    with db.lock:
        return db.list_library_manufacturers(public_only=library_mode)


# ── Download ──────────────────────────────────────────────────────────────────

@router.get("/items/{item_id}/download")
async def download_item(item_id: str, request: Request):
    library_mode = getattr(request.state, "library_mode", False)
    with db.lock:
        item = db.get_library_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found.")
    if library_mode and item.get("source") == "mfiles":
        raise HTTPException(status_code=403, detail="Not available in public library mode.")
    filepath = item["filepath"]
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="File not found on disk.")
    return FileResponse(
        path=filepath,
        filename=item["filename"],
        media_type="application/octet-stream",
    )


# ── Upload ────────────────────────────────────────────────────────────────────

@router.post("/items/upload", status_code=201)
async def upload_item(
    request:    Request,
    file:       UploadFile = File(...),
    source:     str            = Form(...),
    reference:  Optional[str] = Form(None),
    doc_type:   str            = Form("misc"),
    version:    Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
):
    """Upload a file directly into the library."""
    if getattr(request.state, "library_mode", False):
        raise HTTPException(status_code=403, detail="Read-only in public library mode.")

    data = await file.read()
    if len(data) > config.MAX_DOC_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB).")

    filename   = file.filename or "upload.bin"
    safe_src   = _safe_path_component(source)
    safe_ref   = _safe_path_component(reference) if reference and reference.strip() else ""

    if safe_ref:
        dir_path = os.path.join(config.LIBRARY_PATH, safe_src, safe_ref)
    else:
        dir_path = os.path.join(config.LIBRARY_PATH, safe_src)

    os.makedirs(dir_path, exist_ok=True)
    filepath = os.path.join(dir_path, filename)

    # Avoid silently overwriting; append a suffix if the file already exists.
    if os.path.exists(filepath):
        base, ext = os.path.splitext(filename)
        suffix = str(uuid.uuid4())[:8]
        filename = f"{base}_{suffix}{ext}"
        filepath = os.path.join(dir_path, filename)

    with open(filepath, "wb") as fh:
        fh.write(data)

    item_id = str(uuid.uuid4())
    item = {
        "id":           item_id,
        "manufacturer": source.strip(),
        "product_id":   reference.strip() if reference and reference.strip() else "",
        "doc_type":     doc_type,
        "version":      version,
        "filename":     filename,
        "filepath":     filepath,
        "source_url":   source_url,
        "checksum":     None,
        "indexed":      0,
        "source":       "manual",
    }
    with db.lock:
        db.insert_library_item(item)
    return {**item, "classification": "public"}


# ── Manual register (file already on disk) ────────────────────────────────────

@router.post("/items", status_code=201)
async def add_item(body: LibraryItemIn, request: Request):
    """Register a pre-existing file already present on disk."""
    if getattr(request.state, "library_mode", False):
        raise HTTPException(status_code=403, detail="Read-only in public library mode.")

    safe_src = _safe_path_component(body.source)
    safe_ref = _safe_path_component(body.reference) if body.reference and body.reference.strip() else ""

    if safe_ref:
        filepath = os.path.join(config.LIBRARY_PATH, safe_src, safe_ref, body.filename)
    else:
        filepath = os.path.join(config.LIBRARY_PATH, safe_src, body.filename)

    if not os.path.isfile(filepath):
        raise HTTPException(
            status_code=422,
            detail=f"File not found at expected path: {filepath}",
        )

    item_id = str(uuid.uuid4())
    item = {
        "id":           item_id,
        "manufacturer": body.source.strip(),
        "product_id":   body.reference.strip() if body.reference and body.reference.strip() else "",
        "doc_type":     body.doc_type,
        "version":      body.version,
        "filename":     body.filename,
        "filepath":     filepath,
        "source_url":   body.source_url,
        "checksum":     None,
        "indexed":      0,
        "source":       "manual",
    }
    with db.lock:
        db.insert_library_item(item)
    return {**item, "classification": "public"}


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: str, request: Request):
    if getattr(request.state, "library_mode", False):
        raise HTTPException(status_code=403, detail="Read-only in public library mode.")
    with db.lock:
        item = db.get_library_item(item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Library item not found.")
        db.delete_library_item(item_id)
    # Remove from disk if it exists.
    filepath = item.get("filepath", "")
    if filepath and os.path.isfile(filepath):
        try:
            os.remove(filepath)
        except OSError:
            pass  # Non-fatal — DB row is already removed.
    return Response(status_code=204)
