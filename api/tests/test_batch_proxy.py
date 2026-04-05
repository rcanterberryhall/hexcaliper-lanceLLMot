"""
test_batch_proxy.py — Tests for the merLLM batch proxy routes in health.py.

Routes: POST /api/batch/submit
        GET  /api/batch/status/{job_id}
        GET  /api/batch/results/{job_id}
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx


# ── POST /api/batch/submit ─────────────────────────────────────────────────────

def test_batch_submit_proxies_to_merllm(app_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True, "id": "job-111"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
        r = app_client.post(
            "/batch/submit",
            json={"source_app": "lancellmot", "prompt": "Analyze this deeply."},
        )

    assert r.status_code == 200
    assert r.json()["id"] == "job-111"


def test_batch_submit_502_when_merllm_unreachable(app_client):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=Exception("connection refused"))

    with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
        r = app_client.post(
            "/batch/submit",
            json={"source_app": "lancellmot", "prompt": "Analyze."},
        )

    assert r.status_code == 502
    assert "merLLM unreachable" in r.json()["detail"]


# ── GET /api/batch/status/{job_id} ────────────────────────────────────────────

def test_batch_status_returns_job_info(app_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "job-222", "status": "queued"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
        r = app_client.get("/batch/status/job-222")

    assert r.status_code == 200
    assert r.json()["status"] == "queued"


def test_batch_status_404_when_job_not_found(app_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"detail": "Job not found"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
        r = app_client.get("/batch/status/no-such-job")

    assert r.status_code == 404


def test_batch_status_502_when_merllm_unreachable(app_client):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("timeout"))

    with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
        r = app_client.get("/batch/status/job-abc")

    assert r.status_code == 502


# ── GET /api/batch/results/{job_id} ───────────────────────────────────────────

def test_batch_results_returns_completed_output(app_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "job-333", "result": "The deep analysis result."}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
        r = app_client.get("/batch/results/job-333")

    assert r.status_code == 200
    assert r.json()["result"] == "The deep analysis result."


def test_batch_results_409_when_job_not_complete(app_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 409
    mock_resp.json.return_value = {"detail": "Job status: running"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
        r = app_client.get("/batch/results/job-running")

    assert r.status_code == 409


def test_batch_results_502_when_merllm_unreachable(app_client):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("timeout"))

    with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
        r = app_client.get("/batch/results/job-abc")

    assert r.status_code == 502
