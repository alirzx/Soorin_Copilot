"""Focused tests for request-local outbound LLM usage reporting."""

from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from typing import Any, Iterator

from src.config.settings import get_settings
from src.core.copilot.service import CopilotService
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent
from src.core.memory.store import MemoryStore
from src.core.observability import llm_usage as usage_module
from src.core.observability.llm_usage import LLMUsageCall, ProductUsageReporter


def make_settings(**overrides: Any):
    values = {
        "llm_provider": "arvan",
        "llm_enabled": True,
        "planner_enabled": True,
        "router_base_url": "http://router.example.invalid",
        "router_api_key": "router-key",
        "planner_base_url": "http://planner.example.invalid",
        "planner_api_key": "planner-key",
        "synthesizer_base_url": "http://synthesizer.example.invalid",
        "synthesizer_api_key": "synthesizer-key",
        "product_api_base_url": "http://product.invalid",
        "product_api_token": "bootstrap-token",
        "product_hwid": "test-hwid",
        "llm_usage_reporting_enabled": True,
        "llm_usage_reporting_url": "https://backend.example.com/internal/llm-usage/requests",
        "product_retry_backoff_seconds": 0.0,
        "chat_store_history": False,
        "conversation_summary_enabled": False,
        "copilot_human_trace_enabled": False,
        "intent_router_retry_enabled": False,
    }
    values.update(overrides)
    return replace(get_settings(), **values)


class FakeProductClient:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[dict[str, Any]] = []

    def get_asset_detection(self, ip: str, *, request_id: str = "") -> dict[str, Any]:
        return {"ip": ip, "request_id": request_id}

    def get_asset_profile(self, ip: str, *, request_id: str = "") -> dict[str, Any]:
        return {"ip": ip, "request_id": request_id}

    def post_json(
        self,
        endpoint_or_url: str,
        payload: dict[str, Any],
        *,
        request_id: str = "",
        idempotency_key: str = "",
        operation: str = "other",
    ) -> tuple[dict[str, Any], int, float]:
        self.calls.append(
            {
                "endpoint_or_url": endpoint_or_url,
                "payload": payload,
                "request_id": request_id,
                "idempotency_key": idempotency_key,
                "operation": operation,
            }
        )
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("report failed")
        return {"ok": True}, 200, 0.01


class FakeRoutingProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, **kwargs) -> LLMProviderResult:
        purpose = kwargs.get("purpose")
        self.calls.append({"messages": messages, **kwargs})
        if purpose == "intent_router":
            return LLMProviderResult(
                text=json.dumps(
                    {
                        "intent": "general_knowledge",
                        "scope": "none",
                        "direction": "none",
                        "depth": 0,
                        "requires_graph": False,
                        "requires_detection": False,
                        "requires_asset_profile": False,
                        "requires_multiple_entities": False,
                        "is_followup": False,
                        "reason": "general",
                    }
                ),
                provider="arvan",
                model="kimi-k3",
                usage={"input_tokens": 2000, "output_tokens": 95, "total_tokens": 2095},
                latency_ms=6004,
                status_code=200,
            )
        if purpose == "planner":
            return LLMProviderResult(
                text='{"plan_id":"p1","steps":[]}',
                provider="arvan",
                model="GLM-5.2",
                usage={"input_tokens": 3000, "output_tokens": 100, "total_tokens": 3100},
                latency_ms=5100,
                status_code=200,
            )
        return LLMProviderResult(
            text="final answer",
            provider="arvan",
            model="kimi-k3",
            usage={"input_tokens": 6792, "output_tokens": 961, "total_tokens": 7753},
            latency_ms=4200,
            status_code=200,
        )

    def stream_chat(self, messages, **kwargs) -> Iterator[LLMStreamEvent]:
        self.calls.append({"messages": messages, **kwargs, "stream": True})
        yield LLMStreamEvent(type="answer_delta", text="final ")
        yield LLMStreamEvent(
            type="done",
            data={
                "provider": "arvan",
                "model": "kimi-k3",
                "deployment": "kimi",
                "usage": {"input_tokens": 6792, "output_tokens": 961, "total_tokens": 7753},
                "latency_ms": 4200,
                "status_code": 200,
                "finish_reason": "stop",
                "stream_terminated": True,
            },
        )


class FakePlannerProvider(FakeRoutingProvider):
    def chat(self, messages, **kwargs) -> LLMProviderResult:
        if kwargs.get("purpose") == "planner":
            raise LLMError("planner failed", reason="planner_failed")
        return super().chat(messages, **kwargs)


class UsageReportingTests(unittest.TestCase):
    @staticmethod
    def assert_payload_contract(payload: dict[str, Any]) -> None:
        assert set(payload) == {"inputTokens", "outputTokens", "models"}
        for model in payload["models"]:
            assert set(model) == {
                "purpose",
                "model",
                "inputTokens",
                "outputTokens",
            }
        assert payload["inputTokens"] == sum(
            model["inputTokens"] for model in payload["models"]
        )
        assert payload["outputTokens"] == sum(
            model["outputTokens"] for model in payload["models"]
        )

    def build_client(self, reporter: ProductUsageReporter, provider: Any, **overrides: Any) -> LLMClient:
        client = LLMClient(make_settings(**overrides), usage_recorder=reporter)
        client.providers["router"] = provider  # type: ignore[assignment]
        client.providers["planner"] = provider  # type: ignore[assignment]
        client.providers["synthesizer"] = provider  # type: ignore[assignment]
        client.provider = provider  # type: ignore[assignment]
        return client

    def test_router_planner_and_chat_aggregate_into_one_payload(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        provider = FakeRoutingProvider()
        client = self.build_client(reporter, provider)
        scope = reporter.start_request("req-1", "trace-1", "session-1")
        try:
            client.chat([{"role": "user", "content": "route"}], request_id="req-1", trace_id="trace-1", purpose="intent_router")
            client.chat([{"role": "user", "content": "plan"}], request_id="req-1", trace_id="trace-1", purpose="planner")
            client.chat([{"role": "user", "content": "answer"}], request_id="req-1", trace_id="trace-1", purpose="chat")
        finally:
            reporter.finish_request(scope, request_success=True)

        self.assertEqual(len(product_client.calls), 1)
        payload = product_client.calls[0]["payload"]
        self.assert_payload_contract(payload)
        self.assertEqual(
            payload,
            {
                "inputTokens": 11792,
                "outputTokens": 1156,
                "models": [
                    {
                        "purpose": "router",
                        "model": "kimi-k3",
                        "inputTokens": 2000,
                        "outputTokens": 95,
                    },
                    {
                        "purpose": "planner",
                        "model": "GLM-5.2",
                        "inputTokens": 3000,
                        "outputTokens": 100,
                    },
                    {
                        "purpose": "chat",
                        "model": "kimi-k3",
                        "inputTokens": 6792,
                        "outputTokens": 961,
                    },
                ],
            },
        )
        self.assertEqual(product_client.calls[0]["request_id"], "req-1")
        self.assertEqual(product_client.calls[0]["idempotency_key"], "req-1")

    def test_reporting_disabled_sends_nothing(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(llm_usage_reporting_enabled=False), product_client)  # type: ignore[arg-type]
        scope = reporter.start_request("req-disabled", "trace-disabled")
        reporter.record(
            LLMUsageCall.from_usage(
                request_id="req-disabled",
                call_id="c1",
                trace_id="trace-disabled",
                provider="arvan",
                model="GLM-5.2",
                purpose="chat",
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )
        )
        reporter.finish_request(scope, request_success=True)
        self.assertEqual(product_client.calls, [])

    def test_streaming_chat_completion_is_included_once(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        client = self.build_client(reporter, FakeRoutingProvider())
        scope = reporter.start_request("req-stream", "trace-stream", "session-stream")
        try:
            events = list(
                client.stream_chat(
                    [{"role": "user", "content": "answer"}],
                    request_id="req-stream",
                    trace_id="trace-stream",
                    purpose="chat",
                )
            )
        finally:
            reporter.finish_request(scope, request_success=True)

        self.assertEqual(events[-1].type, "done")
        payload = product_client.calls[0]["payload"]
        self.assert_payload_contract(payload)
        self.assertEqual(
            payload,
            {
                "inputTokens": 6792,
                "outputTokens": 961,
                "models": [
                    {
                        "purpose": "chat",
                        "model": "kimi-k3",
                        "inputTokens": 6792,
                        "outputTokens": 961,
                    }
                ],
            },
        )

    def test_partial_execution_reports_available_usage_without_status(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        provider = FakePlannerProvider()
        client = self.build_client(reporter, provider)
        scope = reporter.start_request("req-partial", "trace-partial")
        try:
            client.chat([{"role": "user", "content": "route"}], request_id="req-partial", trace_id="trace-partial", purpose="intent_router")
            with self.assertRaises(LLMError):
                client.chat([{"role": "user", "content": "plan"}], request_id="req-partial", trace_id="trace-partial", purpose="planner")
        finally:
            reporter.finish_request(scope, request_success=False)

        payload = product_client.calls[0]["payload"]
        self.assert_payload_contract(payload)
        self.assertNotIn("status", payload)
        self.assertEqual(payload["inputTokens"], 2000)
        self.assertEqual(payload["outputTokens"], 95)
        failed_planner = next(
            model for model in payload["models"] if model["purpose"] == "planner"
        )
        self.assertEqual(failed_planner["inputTokens"], 0)
        self.assertEqual(failed_planner["outputTokens"], 0)

    def test_reporting_failure_makes_one_nonfatal_product_call(self) -> None:
        product_client = FakeProductClient(failures=1)
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        scope = reporter.start_request("req-retry", "trace-retry")
        reporter.record(
            LLMUsageCall.from_usage(
                request_id="req-retry",
                call_id="c1",
                trace_id="trace-retry",
                provider="arvan",
                model="GLM-5.2",
                purpose="chat",
                usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            )
        )
        reporter.finish_request(scope, request_success=True)
        self.assertEqual(len(product_client.calls), 1)

    def test_duplicate_call_id_and_duplicate_finish_are_suppressed(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        scope = reporter.start_request("req-once", "trace-once", "session-once")
        call = LLMUsageCall.from_usage(
            request_id="req-once",
            call_id="req-once:chat:kimi:stream",
            trace_id="trace-once",
            provider="arvan",
            deployment="kimi",
            model="kimi-k3",
            purpose="chat",
            usage={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            http_status=200,
            finish_reason="stop",
        )
        reporter.record(call)
        reporter.record(call)
        reporter.finish_request(scope, request_success=True)
        reporter.finish_request(scope, request_success=True)

        self.assertEqual(len(product_client.calls), 1)
        payload = product_client.calls[0]["payload"]
        self.assert_payload_contract(payload)
        self.assertEqual(
            payload,
            {
                "inputTokens": 4,
                "outputTokens": 2,
                "models": [
                    {
                        "purpose": "chat",
                        "model": "kimi-k3",
                        "inputTokens": 4,
                        "outputTokens": 2,
                    }
                ],
            },
        )

    def test_repeated_calls_aggregate_by_purpose_and_model(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        scope = reporter.start_request("req-aggregate", "trace-aggregate")
        for call_id, input_tokens, output_tokens in (
            ("router-1", 10, 2),
            ("router-2", 15, 3),
        ):
            reporter.record(
                LLMUsageCall.from_usage(
                    request_id="req-aggregate",
                    call_id=call_id,
                    trace_id="trace-aggregate",
                    provider="arvan",
                    deployment="kimi",
                    model="kimi-k3",
                    purpose="intent_router",
                    usage={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                )
            )
        reporter.finish_request(scope, request_success=True)

        payload = product_client.calls[0]["payload"]
        self.assert_payload_contract(payload)
        self.assertEqual(
            payload,
            {
                "inputTokens": 25,
                "outputTokens": 5,
                "models": [
                    {
                        "purpose": "router",
                        "model": "kimi-k3",
                        "inputTokens": 25,
                        "outputTokens": 5,
                    }
                ],
            },
        )

    def test_repair_calls_count_as_actual_router_and_planner_consumption(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        scope = reporter.start_request("req-repair", "trace-repair")
        for purpose, call_id, input_tokens, output_tokens in (
            ("intent_router", "router", 10, 2),
            ("intent_router_repair", "router-repair", 4, 1),
            ("planner_repair", "planner-repair", 6, 3),
        ):
            reporter.record(
                LLMUsageCall.from_usage(
                    request_id="req-repair",
                    call_id=call_id,
                    model="test-model",
                    purpose=purpose,
                    usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
                )
            )
        reporter.finish_request(scope, request_success=True)

        payload = product_client.calls[0]["payload"]
        self.assertEqual(payload["inputTokens"], 20)
        self.assertEqual(payload["outputTokens"], 6)
        self.assertEqual(
            payload["models"],
            [
                {
                    "purpose": "router",
                    "model": "test-model",
                    "inputTokens": 14,
                    "outputTokens": 3,
                },
                {
                    "purpose": "planner",
                    "model": "test-model",
                    "inputTokens": 6,
                    "outputTokens": 3,
                },
            ],
        )
        self.assertEqual(product_client.calls[0]["operation"], "usage_report")

    def test_planner_is_absent_when_it_did_not_run(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        scope = reporter.start_request("req-no-planner", "trace-no-planner")
        reporter.record(
            LLMUsageCall.from_usage(
                request_id="req-no-planner",
                call_id="router",
                model="kimi-k3",
                purpose="intent_router",
                usage={"input_tokens": 7, "output_tokens": 1},
            )
        )
        reporter.record(
            LLMUsageCall.from_usage(
                request_id="req-no-planner",
                call_id="chat",
                model="kimi-k3",
                purpose="chat",
                usage={"input_tokens": 11, "output_tokens": 4},
            )
        )
        reporter.finish_request(scope, request_success=True)

        payload = product_client.calls[0]["payload"]
        self.assert_payload_contract(payload)
        self.assertEqual(
            [model["purpose"] for model in payload["models"]],
            ["router", "chat"],
        )

    def test_requests_remain_isolated_under_concurrency(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]

        def worker(request_id: str, tokens: int) -> None:
            scope = reporter.start_request(request_id, f"trace-{request_id}")
            try:
                reporter.record(
                    LLMUsageCall.from_usage(
                        request_id=request_id,
                        call_id=f"{request_id}:chat",
                        trace_id=f"trace-{request_id}",
                        provider="arvan",
                        model="GLM-5.2",
                        purpose="chat",
                        usage={"input_tokens": tokens, "output_tokens": 1, "total_tokens": tokens + 1},
                    )
                )
            finally:
                reporter.finish_request(scope, request_success=True)

        first = threading.Thread(target=worker, args=("req-a", 10))
        second = threading.Thread(target=worker, args=("req-b", 20))
        first.start()
        second.start()
        first.join()
        second.join()

        self.assertEqual(len(product_client.calls), 2)
        payloads = {item["request_id"]: item["payload"] for item in product_client.calls}
        self.assert_payload_contract(payloads["req-a"])
        self.assert_payload_contract(payloads["req-b"])
        self.assertEqual(payloads["req-a"]["inputTokens"], 10)
        self.assertEqual(payloads["req-b"]["inputTokens"], 20)
        self.assertEqual(
            {item["idempotency_key"] for item in product_client.calls},
            {"req-a", "req-b"},
        )

    def test_payload_omits_prompts_and_responses_and_state_is_cleared(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        scope = reporter.start_request("req-clean", "trace-clean")
        reporter.record(
            LLMUsageCall.from_usage(
                request_id="req-clean",
                call_id="req-clean:chat",
                trace_id="trace-clean",
                provider="arvan",
                model="GLM-5.2",
                purpose="chat",
                usage={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            )
        )
        reporter.finish_request(scope, request_success=True)

        payload_text = json.dumps(product_client.calls[0]["payload"], ensure_ascii=False)
        self.assertNotIn("super secret prompt", payload_text)
        self.assert_payload_contract(product_client.calls[0]["payload"])
        self.assertIsNone(usage_module._CURRENT_COLLECTOR.get())

    def test_failed_call_without_usage_does_not_fabricate_tokens(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        scope = reporter.start_request("req-failed-zero", "trace-failed-zero")
        reporter.record(
            LLMUsageCall.from_usage(
                request_id="req-failed-zero",
                call_id="failed-chat",
                model="kimi-k3",
                purpose="chat",
                usage=None,
                status="failed",
            )
        )
        reporter.finish_request(scope, request_success=False)

        payload = product_client.calls[0]["payload"]
        self.assert_payload_contract(payload)
        self.assertEqual(
            payload,
            {
                "inputTokens": 0,
                "outputTokens": 0,
                "models": [
                    {
                        "purpose": "chat",
                        "model": "kimi-k3",
                        "inputTokens": 0,
                        "outputTokens": 0,
                    }
                ],
            },
        )

    def test_request_without_llm_calls_reports_empty_exact_body(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        scope = reporter.start_request("req-empty", "trace-empty")

        reporter.finish_request(scope, request_success=True)

        self.assertEqual(len(product_client.calls), 1)
        self.assertEqual(
            product_client.calls[0]["payload"],
            {
                "inputTokens": 0,
                "outputTokens": 0,
                "models": [],
            },
        )

    def test_final_reporting_failure_does_not_fail_user_request(self) -> None:
        settings = make_settings()
        product_client = FakeProductClient(failures=2)
        reporter = ProductUsageReporter(settings, product_client)  # type: ignore[arg-type]
        provider = FakeRoutingProvider()
        client = self.build_client(reporter, provider)
        service = CopilotService(
            settings,
            client,
            MemoryStore(10),
            product_client=product_client,  # type: ignore[arg-type]
            usage_reporter=reporter,
        )

        result = service.chat("What is Kerberos?", "s1", request_id="req-service")

        self.assertEqual(result["answer"], "final answer")
        self.assertEqual(result["provider"], "arvan")
        self.assertEqual(len(product_client.calls), 1)


if __name__ == "__main__":
    unittest.main()
