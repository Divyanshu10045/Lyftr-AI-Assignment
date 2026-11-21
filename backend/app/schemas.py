from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator

MAX_MESSAGE_CHARS = 4000
MAX_SESSION_ID_CHARS = 128
MAX_SYSTEM_PROMPT_CHARS = 8000


class ChatRequest(BaseModel):
    session_id: str
    message: str
    system_prompt: str | None = None

    @field_validator("session_id")
    @classmethod
    def _session_id(cls, v: str) -> str:
        v = v.strip()
        if not 1 <= len(v) <= MAX_SESSION_ID_CHARS:
            raise ValueError(f"session_id must be 1-{MAX_SESSION_ID_CHARS} characters")
        return v

    @field_validator("message")
    @classmethod
    def _message(cls, v: str) -> str:
        v = v.strip()
        if not 1 <= len(v) <= MAX_MESSAGE_CHARS:
            raise ValueError(f"message must be 1-{MAX_MESSAGE_CHARS} characters after trimming whitespace")
        return v

    @field_validator("system_prompt")
    @classmethod
    def _system_prompt(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(v) > MAX_SYSTEM_PROMPT_CHARS:
            raise ValueError(f"system_prompt must be at most {MAX_SYSTEM_PROMPT_CHARS} characters")
        return v or None  # blank = not provided


class ChatMetadata(BaseModel):
    resolution_source: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    metadata: ChatMetadata


class HistoryResponse(BaseModel):
    session_id: str
    messages: list[dict[str, Any]]
