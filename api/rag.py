import os

import chromadb
import httpx

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
TOP_K = 4
DISTANCE_THRESHOLD = 0.45  # cosine distance; 0=identical, 1=unrelated

_collection = None


def get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path="/app/data/chroma")
        _collection = client.get_or_create_collection(
            "documents",
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def chunk_text(text: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start : start + CHUNK_SIZE].strip()
        if chunk:
            chunks.append(chunk)
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


async def embed(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
        )
    resp.raise_for_status()
    return resp.json()["embedding"]


async def ingest(doc_id: str, user_email: str, text: str, scope: str = "global") -> int:
    col = get_collection()
    chunks = chunk_text(text)
    if not chunks:
        return 0
    embeddings = [await embed(c) for c in chunks]
    col.add(
        ids=[f"{doc_id}__{i}" for i in range(len(chunks))],
        embeddings=embeddings,
        documents=chunks,
        metadatas=[{"doc_id": doc_id, "user_email": user_email, "scope": scope} for _ in chunks],
    )
    return len(chunks)


def migrate_legacy_scopes() -> None:
    """Tag any pre-scope ChromaDB chunks as 'global' (one-time, idempotent)."""
    try:
        col = get_collection()
        results = col.get(include=["metadatas"])
        ids_to_update, new_metas = [], []
        for id_, meta in zip(results["ids"], results["metadatas"]):
            if not meta.get("scope"):
                ids_to_update.append(id_)
                new_metas.append({**meta, "scope": "global"})
        if ids_to_update:
            col.update(ids=ids_to_update, metadatas=new_metas)
    except Exception:
        pass


async def search(
    user_email: str,
    query: str,
    top_k: int = TOP_K,
    conversation_id: str | None = None,
) -> tuple[list[str], list[str]]:
    """Return (text_chunks, doc_ids) for chunks passing the distance threshold."""
    col = get_collection()
    if col.count() == 0:
        return [], []
    query_emb = await embed(query)

    if conversation_id:
        where: dict = {
            "$and": [
                {"user_email": {"$eq": user_email}},
                {"$or": [
                    {"scope": {"$eq": "global"}},
                    {"scope": {"$eq": f"conversation:{conversation_id}"}},
                ]},
            ]
        }
    else:
        where = {
            "$and": [
                {"user_email": {"$eq": user_email}},
                {"scope": {"$eq": "global"}},
            ]
        }

    try:
        results = col.query(
            query_embeddings=[query_emb],
            n_results=top_k,
            where=where,
            include=["documents", "distances", "metadatas"],
        )
        if not results["documents"]:
            return [], []
        chunks, doc_ids = [], []
        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0],
        ):
            if dist < DISTANCE_THRESHOLD:
                chunks.append(doc)
                doc_ids.append(meta.get("doc_id", ""))
        return chunks, doc_ids
    except Exception:
        return [], []


def delete_chunks(doc_id: str) -> None:
    col = get_collection()
    col.delete(where={"doc_id": doc_id})
