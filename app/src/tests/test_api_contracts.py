"""Focused offline checks for documented public API contracts."""

from __future__ import annotations

from unittest.mock import patch

from src.api.main import create_app
from src.api.routes import ChatRequest, chat, chat_stream, encode_sse_event
from src.api.schemas.chat import ChatResponse
from src.core.llm.providers.base import LLMStreamEvent


def test_openapi_exposes_typed_health_chat_and_sse_contracts() -> None:
    schema = create_app().openapi()

    assert schema["paths"]["/health"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/HealthResponse")
    assert schema["paths"]["/llm/health"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/LLMHealthResponse")
    assert schema["paths"]["/chat"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/ChatResponse")
    stream_content = schema["paths"]["/chat/stream"]["post"]["responses"]["200"]["content"]
    assert set(stream_content) == {"text/event-stream"}


def test_chat_response_contract_and_nullable_ui_context() -> None:
    expected = {
        "session_id": "contract-session",
        "answer": "test",
        "provider": "arvan",
        "model": "fixture-model",
        "_warnings": [],
    }
    with patch("src.api.routes.copilot_service.chat", return_value=expected.copy()) as service:
        payload = chat(
            ChatRequest.model_validate({
                "session_id": "contract-session",
                "message": "Just say test.",
                "ui_context": None,
            })
        )

    assert ChatResponse.model_validate(payload).model_dump() == {
        "status": "ok",
        "data": {
            "session_id": "contract-session",
            "answer": "test",
            "provider": "arvan",
            "model": "fixture-model",
        },
        "warnings": [],
        "errors": [],
    }
    assert service.call_args.kwargs["ui_context"] is None


def test_selected_ip_is_forwarded_without_changing_the_chat_contract() -> None:
    expected = {
        "session_id": "selected-session",
        "answer": "selected",
        "provider": "arvan",
        "model": "fixture-model",
        "_warnings": [],
    }
    with patch("src.api.routes.copilot_service.chat", return_value=expected.copy()) as service:
        payload = chat(
            ChatRequest.model_validate({
                "session_id": "selected-session",
                "message": "What is this?",
                "ui_context": {"selected_ip": " 192.0.2.10 "},
            })
        )

    assert ChatResponse.model_validate(payload).status == "ok"
    assert service.call_args.kwargs["ui_context"] == {"selected_ip": "192.0.2.10"}


def test_stream_content_type_framing_final_and_error_events() -> None:
    answer_event = LLMStreamEvent("answer_delta", text="test")
    done_event = LLMStreamEvent(
        "done",
        data={
            "session_id": "stream-session",
            "provider": "arvan",
            "model": "fixture-model",
            "warnings": [],
        },
    )
    with patch(
        "src.api.routes.copilot_service.chat_stream",
        return_value=iter([answer_event, done_event]),
    ):
        response = chat_stream(
            ChatRequest(session_id="stream-session", message="Just say test.")
        )

    assert response.status_code == 200
    assert response.media_type == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert encode_sse_event(answer_event) + encode_sse_event(done_event) == (
        'event: answer_delta\ndata: {"type":"answer_delta","text":"test"}\n\n'
        'event: done\ndata: {"type":"done","data":{"session_id":"stream-session",'
        '"provider":"arvan","model":"fixture-model","warnings":[]}}\n\n'
    )

    error_event = LLMStreamEvent("error", message="safe failure", data={"reason": "fixture"})
    assert encode_sse_event(error_event) == (
        'event: error\ndata: {"type":"error","data":{"reason":"fixture"},'
        '"message":"safe failure"}\n\n'
    )


def test_graph_stats_openapi_uses_typed_ranked_entries() -> None:
    schema = create_app().openapi()
    components = schema["components"]["schemas"]
    stats = components["GraphStatsResponse"]["properties"]

    assert stats["top_destinations"]["items"]["$ref"].endswith("/GraphTopDestination")
    assert stats["top_sources"]["items"]["$ref"].endswith("/GraphTopSource")
