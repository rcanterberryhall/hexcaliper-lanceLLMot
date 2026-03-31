"""
models.py — Pydantic request/response models and domain constants.
"""
from typing import Optional
from pydantic import BaseModel, field_validator
import config

DOC_TYPES = (
    "standard", "requirement", "theop", "fmea", "hazard_analysis",
    "fat", "sat", "contract", "correspondence", "plc_code", "misc",
)


class ChatRequest(BaseModel):
    message:         str
    model:           Optional[str] = None
    system:          Optional[str] = None
    conversation_id: Optional[str] = None
    project_id:      Optional[str] = None

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message must not be empty")
        return v.strip()

    @field_validator("message")
    @classmethod
    def message_length(cls, v: str) -> str:
        if len(v) > config.MAX_INPUT_CHARS:
            raise ValueError(f"message exceeds {config.MAX_INPUT_CHARS} character limit")
        return v


class ChatResponse(BaseModel):
    model:           str
    reply:           str
    conversation_id: str
    sources:         dict
