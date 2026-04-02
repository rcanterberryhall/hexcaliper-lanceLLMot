"""
routers/health.py — System health, model management, and GPU endpoints.
"""
from fastapi import APIRouter, HTTPException, Request

import psutil
psutil.cpu_percent()  # prime interval counter so first real call is accurate

import config
import ollama

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "ok":                      True,
        "ollama_base_url":         config.OLLAMA_BASE_URL,
        "default_model":           config.DEFAULT_MODEL,
        "analysis_model":          config.ANALYSIS_MODEL,
        "max_input_chars":         config.MAX_INPUT_CHARS,
        "request_timeout_seconds": config.REQUEST_TIMEOUT,
    }


@router.get("/models")
async def models():
    try:
        return {"models": await ollama.list_models()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot fetch models from Ollama: {exc}")


@router.get("/gpu")
async def gpu():
    return ollama.gpu_stats()


@router.get("/system")
async def system():
    mem = psutil.virtual_memory()
    return {
        "ok":       True,
        "cpu_util": int(psutil.cpu_percent(interval=None)),
        "mem_used": mem.used,
        "mem_total": mem.total,
    }


@router.post("/set-analysis-model")
async def set_analysis_model(request: Request):
    body  = await request.json()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    config.ANALYSIS_MODEL = model
    return {"ok": True, "model": model}


@router.get("/model-status")
async def get_model_status(model: str = ""):
    return await ollama.model_status(model)


@router.post("/warm-model")
async def post_warm_model(request: Request):
    body  = await request.json()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    try:
        await ollama.warm_model(model)
        return {"ok": True, "model": model}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load model: {exc}")
