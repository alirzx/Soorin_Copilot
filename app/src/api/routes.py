"""HTTP routes for the baseline Copilot API."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field, field_validator
from starlette.responses import StreamingResponse

from src.api.auth import verify_api_key
from src.api.dependencies import get_local_persistence, get_product_api_client
from src.api.schemas.chat import ChatResponse, HealthResponse, LLMHealthResponse
from src.config.settings import get_settings
from src.core.copilot.service import CopilotService
from src.core.context.models import approx_tokens, compact_preview
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMStreamEvent
from src.core.memory.routing_state import SessionRoutingStateStore
from src.core.identity import (
    IDENTIFIER_MAX_LENGTH,
    IDENTIFIER_PATTERN,
    RequestIdentity,
    normalize_identifier,
)
from src.core.memory.store import MemoryStore
from src.core.observability.llm_usage import ProductUsageReporter


logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()
memory_store = MemoryStore(
    settings.conversation_max_messages,
    max_episodes=settings.memory_episode_retention_limit,
)
routing_state_store = SessionRoutingStateStore()
product_client = get_product_api_client()
usage_reporter = ProductUsageReporter(settings, product_client)
llm_client = LLMClient(settings, usage_recorder=usage_reporter)
local_persistence = get_local_persistence()
copilot_service = CopilotService(
    settings,
    llm_client,
    memory_store,
    routing_state_store,
    product_client=product_client,
    usage_reporter=usage_reporter,
    chat_repository=local_persistence.chat_repository,
    thread_state_store=local_persistence.thread_state_store,
)


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
    conversation_id: str | None = Field(default=None)
    request_id: str | None = Field(default=None)
    message: str = Field(min_length=1)
    ui_context: ChatUIContext | None = Field(default=None)

    @field_validator("session_id", "conversation_id", "request_id")
    @classmethod
    def validate_identifiers(cls, value: str | None, info: Any) -> str | None:
        return normalize_identifier(value, field_name=info.field_name)


UserIdHeader = Annotated[
    str | None,
    Header(
        alias="X-User-ID",
        min_length=1,
        max_length=IDENTIFIER_MAX_LENGTH,
        pattern=IDENTIFIER_PATTERN,
    ),
]


def resolve_request_identity(request: ChatRequest, user_id: str | None) -> RequestIdentity:
    return RequestIdentity.resolve(
        user_id=user_id,
        conversation_id=request.conversation_id,
        session_id=request.session_id,
        request_id=request.request_id,
    )


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


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    logger.debug("event=http_health status=ok")
    return HealthResponse(status="ok")


@router.get("/llm/health", response_model=LLMHealthResponse)
def llm_health(
    _auth: None = Depends(verify_api_key),
) -> dict[str, Any]:
    result = llm_client.health()
    logger.info(
        "event=http_llm_health ready=%s deployment=%s provider=%s model=%s",
        result.get("ready"),
        result.get("deployment"),
        result.get("provider"),
        result.get("model"),
    )
    return envelope("ok", result)


@router.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    x_user_id: UserIdHeader = None,
    _auth: None = Depends(verify_api_key),
) -> dict[str, Any]:
    identity = resolve_request_identity(request, x_user_id)
    request_id = identity.request_id
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
            request_identity=identity,
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


@router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "UTF-8 Server-Sent Events carrying normalized Copilot stream events.",
            "content": {
                "text/event-stream": {
                    "schema": {
                        "type": "string",
                        "description": (
                            "SSE records use an event line and one JSON data line, followed by a blank line. "
                            "Event types are reasoning_delta, answer_delta, usage, done, and error."
                        ),
                    },
                    "example": (
                        'event: answer_delta\\ndata: {"type":"answer_delta","text":"test"}\\n\\n'
                        'event: done\\ndata: {"type":"done","data":{"session_id":"example",'
                        '"provider":"arvan","model":"example-model","warnings":[]}}\\n\\n'
                    ),
                }
            },
        }
    },
)
def chat_stream(
    request: ChatRequest,
    x_user_id: UserIdHeader = None,
    _auth: None = Depends(verify_api_key),
) -> StreamingResponse:
    """Stream only final-model events while preserving the existing chat route."""
    identity = resolve_request_identity(request, x_user_id)
    request_id = identity.request_id
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
                request_identity=identity,
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
