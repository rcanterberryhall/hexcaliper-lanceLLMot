"""
ollama.py — Ollama API client helpers.

Provides: document summarisation, model listing/warming, GPU stats,
and the low-level async streaming helper used by the chat router.
"""
import json
import logging
from typing import AsyncIterator

import httpx

import config

log = logging.getLogger(__name__)


async def list_models() -> list[str]:
    """Return chat-capable model names, filtering out embedding models."""
    _EMBED_PREFIXES = ("nomic-", "mxbai-", "all-minilm")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{config.OLLAMA_BASE_URL}/api/tags")
    resp.raise_for_status()
    names = [
        m["name"] for m in resp.json().get("models", [])
        if not any(m["name"].startswith(p) for p in _EMBED_PREFIXES)
    ]
    return sorted(names)


async def model_status(model: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{config.OLLAMA_BASE_URL}/api/ps")
        resp.raise_for_status()
        running = resp.json().get("models", [])
        loaded  = any(m.get("name") == model or m.get("model") == model for m in running)
        active  = [
            m.get("name") or m.get("model") for m in running
            if m.get("name") != model and m.get("model") != model
        ]
        return {"model": model, "loaded": loaded, "active": active}
    except Exception as exc:
        log.warning("model_info failed: %s", exc)
        return {"model": model, "loaded": False}


async def warm_model(model: str) -> None:
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": "10m"},
        )
    resp.raise_for_status()


async def summarize_document(text: str, model: str = "") -> str:
    """Return a 2-3 sentence summary of the document's first 6000 chars."""
    sample = text[:6000]
    m = model or config.ANALYSIS_MODEL
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{config.OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": m,
                    "stream": False,
                    "messages": [{"role": "user", "content": (
                        "Summarize this document in 2-3 sentences, "
                        "focusing on its main topic and purpose:\n\n" + sample
                    )}],
                },
            )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    except Exception as exc:
        log.warning("classify failed: %s", exc)
        return ""


async def stream_chat(client: httpx.AsyncClient, payload: dict) -> AsyncIterator[dict]:
    """
    Stream a /api/chat request, yielding parsed JSON chunks.
    Yields {"_error": "..."} on HTTP error instead of raising.
    """
    async with client.stream(
        "POST", f"{config.OLLAMA_BASE_URL}/api/chat", json=payload
    ) as resp:
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


def gpu_stats() -> dict:
    """Return NVML stats for all GPUs, or an error dict."""
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        gpus  = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            util   = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem    = pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp   = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            name   = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            gpus.append({"index": i, "name": name, "gpu_util": util.gpu,
                         "mem_used": mem.used, "mem_total": mem.total, "temperature": temp})
        return {"ok": True, "gpus": gpus}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
