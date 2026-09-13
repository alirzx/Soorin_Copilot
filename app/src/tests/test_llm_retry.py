"""Focused transient retry tests for the provider-neutral LLM client."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

import requests
from urllib3.exceptions import ProtocolError

from src.config.settings import get_settings
from src.core.context.entities import EntityResolver
from src.core.context.intent import SemanticIntentRouter as GLMIntentRouter
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.memory.routing_state import SessionRoutingState


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self.payload = payload or {}

    def json(self) -> dict:
        return self.payload


def success_response(text: str = "ok") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "model": "GLM-5.2",
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        },
    )


class LLMTransientRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "test-secret-that-must-not-be-logged"
        self.settings = replace(
            get_settings(),
            llm_enabled=True,
            llm_provider="arvan",
            router_base_url="https://example.invalid/v1",
            router_api_key=self.secret,
            synthesizer_base_url="https://example.invalid/v1",
            synthesizer_api_key=self.secret,
            llm_max_transient_retries=1,
            llm_retry_base_delay_seconds=0.0,
            llm_retry_max_delay_seconds=0.0,
            llm_connect_timeout_seconds=8,
            router_timeout_seconds=15,
            synthesizer_timeout_seconds=300,
        )
        self.client = LLMClient(self.settings)
        self.messages = [{"role": "user", "content": "hello"}]

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_read_timeout_then_success(self, post) -> None:
        post.side_effect = [requests.exceptions.ReadTimeout(), success_response("recovered")]

        result = self.client.chat(self.messages, request_id="retry-read")

        self.assertEqual(result.text, "recovered")
        self.assertEqual(post.call_count, 2)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_chunked_encoding_error_then_success(self, post) -> None:
        post.side_effect = [requests.exceptions.ChunkedEncodingError(), success_response("recovered")]

        result = self.client.chat(self.messages, request_id="retry-chunk")

        self.assertEqual(result.text, "recovered")
        self.assertEqual(post.call_count, 2)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_protocol_error_then_success(self, post) -> None:
        post.side_effect = [ProtocolError("connection broken"), success_response("recovered")]

        result = self.client.chat(self.messages, request_id="retry-protocol")

        self.assertEqual(result.text, "recovered")
        self.assertEqual(post.call_count, 2)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_http_503_then_success(self, post) -> None:
        post.side_effect = [FakeResponse(503), success_response("recovered")]

        result = self.client.chat(self.messages, request_id="retry-503")

        self.assertEqual(result.text, "recovered")
        self.assertEqual(post.call_count, 2)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_http_400_is_not_retried(self, post) -> None:
        post.return_value = FakeResponse(400)

        with self.assertRaises(LLMError) as raised:
            self.client.chat(self.messages, request_id="no-retry-400")

        self.assertEqual(raised.exception.details["status_code"], 400)
        self.assertFalse(raised.exception.details["retryable"])
        self.assertEqual(post.call_count, 1)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_two_transient_failures_end_in_safe_error(self, post) -> None:
        post.side_effect = [requests.exceptions.ConnectTimeout(), requests.exceptions.ConnectionError()]

        with self.assertRaisesRegex(LLMError, "Arvan request failed") as raised:
            self.client.chat(self.messages, request_id="retry-exhausted")

        self.assertTrue(raised.exception.details["retryable"])
        self.assertEqual(post.call_count, 2)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_router_falls_back_immediately_on_transport_failure(self, post) -> None:
        post.side_effect = requests.exceptions.ReadTimeout()
        router = GLMIntentRouter(self.settings, self.client)
        entities = EntityResolver().resolve("Tell me about 192.168.20.149")

        decision = router.classify(
            "Tell me about 192.168.20.149",
            entities,
            SessionRoutingState(),
            request_id="router-transport",
        )

        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.fallback_reason, "provider_transport_error")
        self.assertFalse(decision.content_present)
        self.assertIsNone(decision.finish_reason)
        self.assertEqual(post.call_count, 1)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_provider_uses_purpose_specific_connect_and_read_timeouts(self, post) -> None:
        post.side_effect = [success_response("router"), success_response("answer")]

        self.client.chat(
            self.messages,
            request_id="router-timeout",
            timeout_seconds=self.settings.router_timeout_seconds,
            purpose="intent_router",
            transient_retries=0,
        )
        self.client.chat(
            self.messages,
            request_id="chat-timeout",
            timeout_seconds=self.settings.synthesizer_timeout_seconds,
            purpose="chat",
        )

        self.assertEqual(post.call_args_list[0].kwargs["timeout"], (8, 15))
        self.assertEqual(post.call_args_list[1].kwargs["timeout"], (8, 300))

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_provider_receives_exact_requested_max_tokens(self, post) -> None:
        post.return_value = success_response("bounded")

        self.client.chat(self.messages, request_id="max-tokens", max_tokens=321, purpose="chat")

        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 321)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_retry_logs_reason_and_count_without_secret(self, post) -> None:
        post.side_effect = [requests.exceptions.ReadTimeout(), success_response("recovered")]

        with self.assertLogs("src.core.llm", level="INFO") as captured:
            self.client.chat(self.messages, request_id="retry-logs")

        logs = "\n".join(captured.output)
        self.assertIn("event=provider_retry_scheduled", logs)
        self.assertIn("error_type=ReadTimeout", logs)
        self.assertIn("retry_count=1", logs)
        self.assertIn("event=provider_retry_succeeded", logs)
        self.assertNotIn(self.secret, logs)


if __name__ == "__main__":
    unittest.main()
