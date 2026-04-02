"""
routers/system_prompts.py — System prompt CRUD + conversation assignment.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

import db

router = APIRouter()


def _user(request: Request) -> str:
    return request.headers.get("cf-access-authenticated-user-email", "local@dev")


@router.get("/system-prompts")
async def list_system_prompts(request: Request):
    with db.lock:
        return db.list_system_prompts(_user(request))


@router.post("/system-prompts", status_code=201)
async def create_system_prompt(request: Request):
    body = await request.json()
    name    = (body.get("name") or "").strip()
    content = (body.get("content") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    with db.lock:
        return db.insert_system_prompt(_user(request), name, content)


@router.put("/system-prompts/{prompt_id}")
async def update_system_prompt(prompt_id: int, request: Request):
    body = await request.json()
    name    = (body.get("name") or "").strip()
    content = (body.get("content") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    user_email = _user(request)
    with db.lock:
        sp = db.get_system_prompt(prompt_id)
        if not sp:
            raise HTTPException(status_code=404, detail="System prompt not found.")
        if sp["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        db.update_system_prompt(prompt_id, {"name": name, "content": content})
    return {"id": prompt_id, "name": name, "content": content}


@router.delete("/system-prompts/{prompt_id}")
async def delete_system_prompt(prompt_id: int, request: Request):
    user_email = _user(request)
    with db.lock:
        sp = db.get_system_prompt(prompt_id)
        if not sp:
            raise HTTPException(status_code=404, detail="System prompt not found.")
        if sp["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        db.delete_system_prompt(prompt_id)
    return Response(status_code=204)


@router.patch("/conversations/{conv_id}/system-prompt")
async def assign_system_prompt(conv_id: str, request: Request):
    body = await request.json()
    sp_id = body.get("system_prompt_id")  # None to clear
    user_email = _user(request)
    with db.lock:
        conv = db.get_conversation(conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if conv["user_email"] != user_email:
            raise HTTPException(status_code=403, detail="Access denied.")
        if sp_id is not None:
            sp = db.get_system_prompt(sp_id)
            if not sp or sp["user_email"] != user_email:
                raise HTTPException(status_code=404, detail="System prompt not found.")
        db.update_conversation(conv_id, {"system_prompt_id": sp_id})
    return {"conversation_id": conv_id, "system_prompt_id": sp_id}
