"""
routers/library.py — Client and project management.

Clients own projects. Documents are scoped to global / client / project / session.
"""
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

import db

router = APIRouter(prefix="/workspace")


class ClientIn(BaseModel):
    name: str


class ProjectIn(BaseModel):
    name:      str
    client_id: str


@router.get("/clients")
async def list_clients():
    with db.lock:
        return db.list_clients()


@router.post("/clients", status_code=201)
async def create_client(body: ClientIn):
    with db.lock:
        try:
            return db.insert_client(str(uuid.uuid4()), body.name.strip())
        except Exception:
            raise HTTPException(status_code=409, detail="Client name already exists.")


@router.delete("/clients/{client_id}", status_code=204)
async def delete_client(client_id: str):
    with db.lock:
        if not db.get_client(client_id):
            raise HTTPException(status_code=404, detail="Client not found.")
        db.delete_client(client_id)
    return Response(status_code=204)


@router.get("/projects")
async def list_projects(client_id: str = ""):
    with db.lock:
        return db.list_projects(client_id or None)


@router.post("/projects", status_code=201)
async def create_project(body: ProjectIn):
    with db.lock:
        if not db.get_client(body.client_id):
            raise HTTPException(status_code=404, detail="Client not found.")
        try:
            return db.insert_project(str(uuid.uuid4()), body.name.strip(), body.client_id)
        except Exception:
            raise HTTPException(status_code=409, detail="Project name already exists for this client.")


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    with db.lock:
        proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")
    return proj


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str):
    with db.lock:
        if not db.get_project(project_id):
            raise HTTPException(status_code=404, detail="Project not found.")
        db.delete_project(project_id)
    return Response(status_code=204)
