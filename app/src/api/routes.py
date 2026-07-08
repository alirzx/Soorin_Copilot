"""HTTP routes for the baseline Copilot API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.config.settings import get_settings
from src.core.copilot.service import CopilotService
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.memory.store import MemoryStore


logger = logging.getLogger(__name__)
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
    logger.info("event=http_health status=ok")
    return {"status": "ok"}


@router.get("/llm/health")
def llm_health() -> dict[str, Any]:
    result = llm_client.health()
    logger.info(
        "event=http_llm_health ready=%s provider=%s model=%s",
        result.get("ready"),
        result.get("provider"),
        result.get("model"),
    )
    return envelope("ok", result)


@router.post("/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    user_preview = request.message.strip().replace("\n", " ")[:120]
    logger.info(
        "event=http_chat_request session_id=%s user_preview=%r",
        request.session_id or "",
        user_preview,
    )
    try:
        result = copilot_service.chat(request.message, request.session_id)
    except LLMError as exc:
        logger.warning(
            "event=http_chat_error reason=%s session_id=%s",
            exc.reason,
            request.session_id or "",
        )
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
