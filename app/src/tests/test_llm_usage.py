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
        "intent_router_deployment": "glm",
        "chat_deployment": "glm",
        "planner_enabled": True,
        "planner_deployment": "gpt55",
        "glm_base_url": "http://glm.example.invalid",
        "glm_api_key": "glm-key",
        "gpt55_base_url": "http://gpt.example.invalid",
        "gpt55_api_key": "gpt-key",
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
    ) -> tuple[dict[str, Any], int, float]:
        self.calls.append(
            {
                "endpoint_or_url": endpoint_or_url,
                "payload": payload,
                "request_id": request_id,
                "idempotency_key": idempotency_key,
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
                        "classification_confidence": 0.95,
                        "reason": "general",
                    }
                ),
                provider="arvan",
                model="GPT-5.5",
                usage={"input_tokens": 2000, "output_tokens": 95, "total_tokens": 2095},
                latency_ms=6004,
                status_code=200,
            )
        if purpose == "planner":
            return LLMProviderResult(
                text='{"plan_id":"p1","steps":[]}',
                provider="arvan",
                model="GPT-5.5",
                usage={"input_tokens": 3000, "output_tokens": 100, "total_tokens": 3100},
                latency_ms=5100,
                status_code=200,
            )
        return LLMProviderResult(
            text="final answer",
            provider="arvan",
            model="GLM-5.2",
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
                "model": "GLM-5.2",
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
    def build_client(self, reporter: ProductUsageReporter, provider: Any, **overrides: Any) -> LLMClient:
        client = LLMClient(make_settings(**overrides), usage_recorder=reporter)
        client.providers["glm"] = provider  # type: ignore[assignment]
        client.providers["gpt55"] = provider  # type: ignore[assignment]
        client.provider = provider  # type: ignore[assignment]
        return client

    def test_router_planner_and_chat_aggregate_into_one_payload(self) -> None:
        product_client = FakeProductClient()
        reporter = ProductUsageReporter(make_settings(), product_client)  # type: ignore[arg-type]
        provider = FakeRoutingProvider()
        client = self.build_client(reporter, provider)
        scope = reporter.start_request("req-1", "trace-1")
        try:
            client.chat([{"role": "user", "content": "route"}], request_id="req-1", trace_id="trace-1", purpose="intent_router")
            client.chat([{"role": "user", "content": "plan"}], request_id="req-1", trace_id="trace-1", purpose="planner")
            client.chat([{"role": "user", "content": "answer"}], request_id="req-1", trace_id="trace-1", purpose="chat")
        finally:
            reporter.finish_request(scope, request_success=True)

        self.assertEqual(len(product_client.calls), 1)
        payload = product_client.calls[0]["payload"]
        self.assertEqual(payload["request_id"], "req-1")
        self.assertEqual(payload["trace_id"], "trace-1")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["call_count"], 3)
        self.assertEqual(payload["input_tokens"], 11792)
        self.assertEqual(payload["output_tokens"], 1156)
        self.assertEqual(payload["total_tokens"], 12948)
        self.assertEqual([call["purpose"] for call in payload["calls"]], ["intent_router", "planner", "chat"])

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

    def test_partial_execution_reports_partial(self) -> None:
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

        self.assertEqual(product_client.calls[0]["payload"]["status"], "partial")
        self.assertEqual(product_client.calls[0]["payload"]["call_count"], 2)

    def test_one_retry_occurs_after_reporting_failure(self) -> None:
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
        self.assertEqual(len(product_client.calls), 2)

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
        payloads = {item["payload"]["request_id"]: item["payload"] for item in product_client.calls}
        self.assertEqual(payloads["req-a"]["input_tokens"], 10)
        self.assertEqual(payloads["req-b"]["input_tokens"], 20)

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
        self.assertIsNone(usage_module._CURRENT_COLLECTOR.get())

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
        self.assertEqual(len(product_client.calls), 2)


if __name__ == "__main__":
    unittest.main()
