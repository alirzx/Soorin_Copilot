"""HTTP routes for the baseline Copilot API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.config.settings import get_settings
from src.core.copilot.service import CopilotService
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.memory.store import MemoryStore


router = APIRouter()
settings = get_settings()
memory_store = MemoryStore(settings.chat_max_history_messages)
llm_client = LLMClient(settings)
copilot_service = CopilotService(settings, llm_client, memory_store)


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None)
    message: str = Field(min_length=1)


def envelope(
    status: str,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "data": data,
        "warnings": warnings or [],
        "errors": errors or [],
    }


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/llm/health")
def llm_health() -> dict[str, Any]:
    return envelope("ok", llm_client.health())


@router.post("/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    try:
        result = copilot_service.chat(request.message, request.session_id)
    except LLMError as exc:
        return envelope(
            "error",
            errors=[
                {
                    "reason": exc.reason,
                    "message": str(exc),
                }
            ],
        )
    return envelope("ok", result)
