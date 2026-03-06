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
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
        )
    resp.raise_for_status()
    return resp.json()["embedding"]


async def ingest(doc_id: str, user_email: str, text: str) -> int:
    col = get_collection()
    chunks = chunk_text(text)
    if not chunks:
        return 0
    embeddings = [await embed(c) for c in chunks]
    col.add(
        ids=[f"{doc_id}__{i}" for i in range(len(chunks))],
        embeddings=embeddings,
        documents=chunks,
        metadatas=[{"doc_id": doc_id, "user_email": user_email} for _ in chunks],
    )
    return len(chunks)


async def search(user_email: str, query: str, top_k: int = TOP_K) -> list[str]:
    col = get_collection()
    if col.count() == 0:
        return []
    query_emb = await embed(query)
    try:
        results = col.query(
            query_embeddings=[query_emb],
            n_results=top_k,
            where={"user_email": user_email},
            include=["documents", "distances"],
        )
        if not results["documents"]:
            return []
        return [
            doc for doc, dist in zip(results["documents"][0], results["distances"][0])
            if dist < DISTANCE_THRESHOLD
        ]
    except Exception:
        return []


def delete_chunks(doc_id: str) -> None:
    col = get_collection()
    col.delete(where={"doc_id": doc_id})
