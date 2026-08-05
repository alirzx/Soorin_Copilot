"""Focused offline tests for backward-compatible request identity contracts."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from src.api.auth import verify_api_key
from src.api.main import create_app
from src.api.routes import ChatRequest, chat, chat_stream
from src.core.agent.workflow import BoundedCopilotWorkflow
from src.core.llm.providers.base import LLMStreamEvent
from src.core.identity import RequestIdentity
from src.tests.test_phase3_langgraph_workflow import FakeNodeRuntime


class IdentityCapturingRuntime(FakeNodeRuntime):
    def resolve_entities(self, state):
        self.request_identity = state["request_identity"]
        self.thread_id = state["thread_id"]
        return super().resolve_entities(state)


def test_legacy_request_and_session_fallback_remain_supported() -> None:
    request = ChatRequest.model_validate({"session_id": "legacy-session", "message": "hello"})
    identity = RequestIdentity.resolve(session_id=request.session_id)

    assert request.conversation_id is None
    assert request.request_id is None
    assert identity.session_id == "legacy-session"
    assert identity.thread_key == "legacy-session"
    assert len(identity.request_id) == 12


def test_conversation_and_supplied_request_id_are_preserved_by_chat() -> None:
    expected = {
        "session_id": "runtime-session",
        "answer": "test",
        "provider": "fixture",
        "model": "fixture",
        "_warnings": [],
    }
    request = ChatRequest.model_validate(
        {
            "session_id": "runtime-session",
            "conversation_id": "product-chatroom-1",
            "request_id": "product-turn-1",
            "message": "hello",
        }
    )
    with patch("src.api.routes.copilot_service.chat", return_value=expected) as service:
        response = chat(request, x_user_id="product-user-1")

    identity = service.call_args.kwargs["request_identity"]
    assert identity == RequestIdentity(
        user_id="product-user-1",
        conversation_id="product-chatroom-1",
        session_id="runtime-session",
        request_id="product-turn-1",
        thread_key="product-chatroom-1",
    )
    assert service.call_args.kwargs["request_id"] == "product-turn-1"
    assert set(response) == {"status", "data", "warnings", "errors"}
    assert set(response["data"]) == {"session_id", "answer", "provider", "model"}


def test_stream_accepts_identity_without_changing_sse_contract() -> None:
    request = ChatRequest.model_validate(
        {
            "conversation_id": "product-chatroom-2",
            "request_id": "product-turn-2",
            "message": "hello",
        }
    )
    events = [
        LLMStreamEvent("answer_delta", text="ok"),
        LLMStreamEvent(
            "done",
            data={
                "session_id": "generated-session",
                "provider": "fixture",
                "model": "fixture",
                "warnings": [],
            },
        ),
    ]
    with patch(
        "src.api.routes.copilot_service.chat_stream",
        return_value=iter(events),
    ) as service:
        response = chat_stream(request, x_user_id="product-user-2")

        async def consume() -> str:
            chunks = [chunk async for chunk in response.body_iterator]
            return "".join(
                chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                for chunk in chunks
            )

        body = asyncio.run(consume())

    identity = service.call_args.kwargs["request_identity"]
    assert identity.user_id == "product-user-2"
    assert identity.thread_key == "product-chatroom-2"
    assert identity.request_id == "product-turn-2"
    assert body == (
        'event: answer_delta\ndata: {"type":"answer_delta","text":"ok"}\n\n'
        'event: done\ndata: {"type":"done","data":{"session_id":"generated-session",'
        '"provider":"fixture","model":"fixture","warnings":[]}}\n\n'
    )


def test_workflow_uses_conversation_as_thread_key_without_changing_session() -> None:
    identity = RequestIdentity.resolve(
        conversation_id="product-chatroom-3",
        session_id="legacy-session-3",
        request_id="product-turn-3",
    )
    runtime = IdentityCapturingRuntime()
    response = BoundedCopilotWorkflow().run(
        message="Investigate 192.0.2.10",
        session_id=identity.session_id,
        ui_context=None,
        request_id=identity.request_id,
        request_identity=identity,
        node_runtime=runtime,
    )

    assert response["session_id"] == "legacy-session-3"
    assert response["answer"] == "ok"
    assert runtime.request_identity == identity
    assert runtime.thread_id == "product-chatroom-3"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", ""),
        ("conversation_id", "   "),
        ("request_id", "line\nbreak"),
        ("session_id", "control\x00character"),
        ("conversation_id", "x" * 129),
        ("request_id", "unsupported value"),
    ],
)
def test_malformed_identifiers_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": "hello", field: value})


def test_missing_session_uses_existing_generated_fallback() -> None:
    identity = RequestIdentity.resolve(conversation_id="product-chatroom-4")

    assert len(identity.session_id) == 32
    assert identity.thread_key == "product-chatroom-4"


def test_openapi_exposes_optional_identity_fields_and_user_header() -> None:
    schema = create_app().openapi()
    request_properties = schema["components"]["schemas"]["ChatRequest"]["properties"]

    assert {"conversation_id", "request_id"} <= set(request_properties)
    for path in ("/chat", "/chat/stream"):
        parameters = schema["paths"][path]["post"]["parameters"]
        user_header = next(item for item in parameters if item["name"] == "X-User-ID")
        assert user_header["required"] is False
        variants = user_header["schema"]["anyOf"]
        text_schema = next(item for item in variants if item.get("type") == "string")
        assert text_schema["maxLength"] == 128


def test_invalid_user_id_header_returns_standard_422_before_service() -> None:
    app = create_app()
    app.dependency_overrides[verify_api_key] = lambda: None

    async def post_invalid_header():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/chat",
                headers={"X-User-ID": "invalid user"},
                json={"message": "hello"},
            )

    with patch("src.api.routes.copilot_service.chat") as service:
        response = asyncio.run(post_invalid_header())

    assert response.status_code == 422
    assert service.call_count == 0
