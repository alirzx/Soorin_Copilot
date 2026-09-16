"""Offline provider matrix for the shared OpenAI-compatible transport."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from src.config.settings import get_settings
# Match application startup order and the established deployment tests.
import src.core.context  # noqa: F401
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError


class JsonResponse:
    status_code = 200

    def __init__(self, text: str = "ok") -> None:
        self.text = text

    def json(self) -> dict[str, object]:
        return {
            "choices": [
                {"message": {"content": self.text}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }


class StreamResponse:
    status_code = 200
    encoding = "utf-8"

    def __init__(self) -> None:
        self.closed = False

    def iter_lines(self, **_kwargs: object):
        yield "data: " + json.dumps(
            {"choices": [{"delta": {"content": "streamed"}, "finish_reason": "stop"}]}
        )
        yield "data: [DONE]"

    def close(self) -> None:
        self.closed = True


class ErrorResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class InvalidJsonResponse:
    status_code = 200

    @staticmethod
    def json() -> dict[str, object]:
        raise ValueError("invalid json")


def provider_settings(provider: str, *, api_key: str = ""):
    return replace(
        get_settings(),
        llm_enabled=True,
        llm_provider=provider,
        planner_enabled=True,
        llm_auth_scheme="Bearer",
        router_base_url="http://router.local/v1",
        router_model="router-model",
        router_api_key=api_key,
        planner_base_url="http://planner.local/v1",
        planner_model="planner-model",
        planner_api_key=api_key,
        synthesizer_base_url="http://synth.local/v1",
        synthesizer_model="synth-model",
        synthesizer_api_key=api_key,
        llm_max_transient_retries=0,
    )


@pytest.mark.parametrize("provider", ["vllm", "ollama", "openai_compatible"])
def test_supported_provider_routes_all_purposes_through_role_deployments(
    provider: str,
) -> None:
    client = LLMClient(provider_settings(provider))
    messages = [{"role": "user", "content": "hello"}]

    with patch(
        "src.core.llm.providers.openai_compatible.requests.post",
        side_effect=[JsonResponse("route"), JsonResponse("plan"), JsonResponse("answer")],
    ) as post:
        routed = client.chat(messages, purpose="intent_router")
        planned = client.chat(messages, purpose="planner")
        answered = client.chat(messages, purpose="chat")

    assert [routed.provider, planned.provider, answered.provider] == [provider] * 3
    assert [call.args[0] for call in post.call_args_list] == [
        "http://router.local/v1/chat/completions",
        "http://planner.local/v1/chat/completions",
        "http://synth.local/v1/chat/completions",
    ]
    assert [call.kwargs["json"]["model"] for call in post.call_args_list] == [
        "router-model",
        "planner-model",
        "synth-model",
    ]


@pytest.mark.parametrize(
    ("provider", "api_key", "expected_authorization"),
    [
        ("vllm", "", None),
        ("vllm", "local-key", "Bearer local-key"),
        ("ollama", "", None),
        ("openai_compatible", "gateway-key", "Bearer gateway-key"),
    ],
)
def test_optional_auth_is_centralized_and_omitted_when_empty(
    provider: str,
    api_key: str,
    expected_authorization: str | None,
) -> None:
    client = LLMClient(provider_settings(provider, api_key=api_key))

    with patch(
        "src.core.llm.providers.openai_compatible.requests.post",
        return_value=JsonResponse(),
    ) as post:
        result = client.chat([{"role": "user", "content": "hello"}], purpose="chat")

    headers = post.call_args.kwargs["headers"]
    assert result.provider == provider
    assert headers.get("Authorization") == expected_authorization
    assert headers["Content-Type"] == "application/json"


@pytest.mark.parametrize("provider", ["vllm", "ollama", "openai_compatible"])
def test_local_and_generic_providers_preserve_stream_metadata(provider: str) -> None:
    client = LLMClient(provider_settings(provider))
    response = StreamResponse()

    with patch(
        "src.core.llm.providers.openai_compatible.requests.post",
        return_value=response,
    ) as post:
        events = list(
            client.stream_chat(
                [{"role": "user", "content": "hello"}],
                purpose="chat",
            )
        )

    assert [event.text for event in events if event.type == "answer_delta"] == [
        "streamed"
    ]
    done = next(event for event in events if event.type == "done")
    assert done.data["provider"] == provider
    assert done.data["deployment"] == "synthesizer"
    assert post.call_args.kwargs["stream"] is True
    assert "Authorization" not in post.call_args.kwargs["headers"]
    assert response.closed is True


def test_unknown_provider_fails_safely_without_constructing_a_transport() -> None:
    settings = provider_settings("some_unknown_provider")
    client = LLMClient(settings)

    with pytest.raises(LLMError) as raised:
        client.chat([{"role": "user", "content": "hello"}], purpose="chat")
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        settings.validate_selected_llm_deployments()

    assert raised.value.reason == "provider_not_supported"
    assert client.health() == {
        "enabled": True,
        "supported": False,
        "ready": False,
        "provider": "some_unknown_provider",
        "model": "synth-model",
        "deployment": "synthesizer",
        "endpoint_configured": True,
        "model_configured": True,
        "auth_required": False,
        "auth_configured": False,
        "reason": "provider_not_supported",
    }


@pytest.mark.parametrize("provider", ["vllm", "ollama", "openai_compatible"])
def test_local_provider_validation_and_health_do_not_require_an_api_key(
    provider: str,
) -> None:
    settings = provider_settings(provider)
    settings.validate_selected_llm_deployments()

    health = LLMClient(settings).health()

    assert health["supported"] is True
    assert health["ready"] is True
    assert health["chat"]["auth_required"] is False
    assert health["chat"]["auth_configured"] is False


@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
def test_compatible_retryable_http_statuses_remain_classified(
    status_code: int,
) -> None:
    client = LLMClient(provider_settings("vllm"))

    with patch(
        "src.core.llm.providers.openai_compatible.requests.post",
        return_value=ErrorResponse(status_code),
    ), pytest.raises(LLMError) as raised:
        client.chat(
            [{"role": "user", "content": "hello"}],
            purpose="chat",
            transient_retries=0,
        )

    assert raised.value.reason == "provider_http_error"
    assert raised.value.details["retryable"] is True


def test_invalid_json_and_empty_content_policies_are_unchanged() -> None:
    client = LLMClient(provider_settings("ollama"))
    messages = [{"role": "user", "content": "hello"}]

    with patch(
        "src.core.llm.providers.openai_compatible.requests.post",
        return_value=InvalidJsonResponse(),
    ), pytest.raises(LLMError) as invalid:
        client.chat(messages, purpose="chat")
    assert invalid.value.reason == "provider_invalid_json"

    with patch(
        "src.core.llm.providers.openai_compatible.requests.post",
        return_value=JsonResponse(""),
    ):
        router = client.chat(messages, purpose="intent_router")
    assert router.text == ""

    with patch(
        "src.core.llm.providers.openai_compatible.requests.post",
        return_value=JsonResponse(""),
    ), pytest.raises(LLMError) as empty_chat:
        client.chat(messages, purpose="chat")
    assert empty_chat.value.reason == "provider_empty_answer"
