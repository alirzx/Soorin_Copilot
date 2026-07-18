"""HTTP routes for the baseline Copilot API."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from starlette.responses import StreamingResponse

from src.config.settings import get_settings
from src.core.copilot.service import CopilotService
from src.core.context.models import approx_tokens, compact_preview
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMStreamEvent
from src.core.memory.routing_state import SessionRoutingStateStore
from src.core.memory.store import MemoryStore


logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()
memory_store = MemoryStore(settings.conversation_max_messages)
routing_state_store = SessionRoutingStateStore()
llm_client = LLMClient(settings)
copilot_service = CopilotService(settings, llm_client, memory_store, routing_state_store)


class ChatUIContext(BaseModel):
    selected_ip: str | None = Field(default=None)

    @field_validator("selected_ip")
    @classmethod
    def normalize_selected_ip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None)
    message: str = Field(min_length=1)
    ui_context: ChatUIContext | None = Field(default=None)


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


def encode_sse_event(event: LLMStreamEvent) -> str:
    """Serialize one normalized event without exposing provider payloads."""
    data = json.dumps(event.to_public_dict(), ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.type}\ndata: {data}\n\n"


@router.get("/health")
def health() -> dict[str, str]:
    logger.debug("event=http_health status=ok")
    return {"status": "ok"}


@router.get("/llm/health")
def llm_health() -> dict[str, Any]:
    result = llm_client.health()
    logger.info(
        "event=http_llm_health ready=%s deployment=%s provider=%s model=%s",
        result.get("ready"),
        result.get("deployment"),
        result.get("provider"),
        result.get("model"),
    )
    return envelope("ok", result)


@router.post("/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    request_id = uuid4().hex[:12]
    started = time.perf_counter()
    selected_ip_present = bool(request.ui_context and request.ui_context.selected_ip)
    session_for_log = (request.session_id or "").strip()
    logger.info(
        "event=copilot_request_begin request_id=%s session_id=%s",
        request_id,
        session_for_log,
    )
    logger.info(
        "event=http_chat_request request_id=%s session_id=%s message_chars=%s approx_tokens=%s ui_context_present=%s selected_ip_present=%s user_preview=%r",
        request_id,
        session_for_log,
        len(request.message),
        approx_tokens(request.message),
        bool(request.ui_context),
        selected_ip_present,
        compact_preview(request.message),
    )
    try:
        result = copilot_service.chat(
            request.message,
            request.session_id,
            ui_context=request.ui_context.model_dump() if request.ui_context else None,
            request_id=request_id,
        )
    except LLMError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "event=http_chat_error request_id=%s reason=%s session_id=%s latency_ms=%s error_count=1",
            request_id,
            exc.reason,
            request.session_id or "",
            latency_ms,
        )
        logger.info(
            "event=copilot_request_end request_id=%s status=error latency_ms=%s",
            request_id,
            latency_ms,
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
    warnings = list(result.pop("_warnings", []))
    latency_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "event=http_chat_response request_id=%s session_id=%s status=ok provider=%s model=%s answer_chars=%s answer_approx_tokens=%s latency_ms=%s warning_count=%s error_count=0",
        request_id,
        result.get("session_id", ""),
        result.get("provider", ""),
        result.get("model", ""),
        len(result.get("answer", "")),
        approx_tokens(result.get("answer", "")),
        latency_ms,
        len(warnings),
    )
    logger.info(
        "event=copilot_request_end request_id=%s status=ok latency_ms=%s",
        request_id,
        latency_ms,
    )
    return envelope("ok", result, warnings=warnings)


@router.post("/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream only final-model events while preserving the existing chat route."""
    request_id = uuid4().hex[:12]
    session_for_log = (request.session_id or "").strip()
    started = time.perf_counter()
    logger.info(
        "event=http_chat_stream_request request_id=%s session_id=%s message_chars=%s approx_tokens=%s ui_context_present=%s selected_ip_present=%s",
        request_id,
        session_for_log,
        len(request.message),
        approx_tokens(request.message),
        bool(request.ui_context),
        bool(request.ui_context and request.ui_context.selected_ip),
    )

    def events() -> Iterator[str]:
        status = "error"
        try:
            for event in copilot_service.chat_stream(
                request.message,
                request.session_id,
                ui_context=request.ui_context.model_dump() if request.ui_context else None,
                request_id=request_id,
            ):
                if event.type == "done":
                    status = "ok"
                yield encode_sse_event(event)
        finally:
            logger.info(
                "event=http_chat_stream_response request_id=%s session_id=%s status=%s latency_ms=%s",
                request_id,
                session_for_log,
                status,
                int((time.perf_counter() - started) * 1000),
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
