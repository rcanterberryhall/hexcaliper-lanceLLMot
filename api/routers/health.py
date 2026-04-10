"""
routers/health.py — System health, model management, and GPU endpoints.
"""
from fastapi import APIRouter, HTTPException, Request

import httpx
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


@router.get("/merllm/status")
async def merllm_status():
    """Proxy GET /api/merllm/status from merLLM for the frontend status indicator."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{config.MERLLM_URL}/api/merllm/status")
            return r.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "routing": "unknown"}


@router.get("/merllm/default-model")
async def merllm_default_model():
    """Proxy GET /api/merllm/default-model from merLLM."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{config.MERLLM_URL}/api/merllm/default-model")
            return r.json()
    except Exception as exc:
        return {"model": None, "error": str(exc)}


@router.post("/batch/submit")
async def batch_submit(request: Request):
    """Proxy POST /api/batch/submit to merLLM for deep analysis job submission."""
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{config.MERLLM_URL}/api/batch/submit",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            return r.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"merLLM unreachable: {exc}")


@router.get("/batch/status/{job_id}")
async def batch_status(job_id: str):
    """Proxy GET /api/batch/status/{job_id} to merLLM."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{config.MERLLM_URL}/api/batch/status/{job_id}")
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail="Job not found")
            return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"merLLM unreachable: {exc}")


@router.get("/batch/results/{job_id}")
async def batch_results(job_id: str):
    """Proxy GET /api/batch/results/{job_id} to merLLM."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{config.MERLLM_URL}/api/batch/results/{job_id}")
            if r.status_code in (404, 409):
                raise HTTPException(status_code=r.status_code,
                                    detail=r.json().get("detail", ""))
            return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"merLLM unreachable: {exc}")


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
