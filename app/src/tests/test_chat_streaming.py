"""Focused offline tests for final-model streaming and Streamlit consumption."""

from __future__ import annotations

import codecs
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.api.routes import encode_sse_event
from src.config.llm_deployments import ArvanDeploymentConfig
from src.config.settings import get_settings
from src.core.copilot.service import CopilotService
from src.core.llm.errors import LLMError
from src.core.llm.providers.arvan import ArvanProvider
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent
from src.core.memory.store import MemoryStore
from src.web.chat_stream import collect_visible_stream, parse_sse_events


GENERAL_ROUTE = json.dumps(
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
        "classification_confidence": 0.9,
        "reason": "general",
    }
)

UNICODE_TEXT = (
    "So the question is not \u201cis this DC broken?\u201d "
    "\u2018smart single quotes\u2019 "
    "em dash \u2014 arrow \u2192 "
    "Persian text: \u0627\u06cc\u0646 \u06cc\u06a9 \u062a\u0633\u062a \u0627\u0633\u062a "
    "emoji: \u2705"
)
MOJIBAKE_MARKERS = ("\u00e2\u0080\u009c", "\u00e2\u0080\u0099", "\u00e2\u0080\u0094", "\u00e2\u0086\u0092")


def deployment() -> ArvanDeploymentConfig:
    return ArvanDeploymentConfig(
        name="glm",
        base_url="https://stream.example.invalid/v1",
        chat_path="/chat/completions",
        model="fixture-chat-model",
        api_key="fixture-key",
        auth_scheme="apikey",
        connect_timeout_seconds=2,
        maximum_completion_tokens=1024,
        router_read_timeout_seconds=5,
        router_max_tokens=128,
        router_repair_max_tokens=256,
        chat_read_timeout_seconds=20,
        chat_max_tokens=512,
        router_temperature=0.0,
        router_top_p=0.1,
        chat_temperature=0.2,
        chat_top_p=0.9,
        supports_temperature=True,
        supports_top_p=True,
        provider_type="arvan",
    )


def service_settings(**overrides: Any):
    values = {
        "llm_provider": "fake",
        "intent_router_deployment": "glm",
        "chat_deployment": "glm",
        "glm_model": "fixture-chat-model",
        "copilot_human_trace_enabled": False,
        "intent_router_enabled": True,
        "intent_router_retry_enabled": False,
        "product_api_base_url": "",
        "product_api_token": "",
        "product_hwid": "",
        "chat_store_history": True,
        "conversation_summary_enabled": False,
        "llm_expose_reasoning": True,
    }
    values.update(overrides)
    return replace(get_settings(), **values)


class FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self.lines = lines
        self.status_code = status_code
        self.closed = False

    def iter_lines(self, chunk_size: int = 512, decode_unicode: bool = False):
        del chunk_size, decode_unicode
        yield from self.lines

    def close(self) -> None:
        self.closed = True


class SplitByteStreamResponse:
    def __init__(self, chunks: list[bytes], status_code: int = 200) -> None:
        self.chunks = chunks
        self.status_code = status_code
        self.encoding: str | None = None
        self.closed = False

    def iter_lines(self, chunk_size: int = 512, decode_unicode: bool = False):
        del chunk_size
        decoder = codecs.getincrementaldecoder(self.encoding or "utf-8")()
        pending = ""
        for chunk in self.chunks:
            fragment = decoder.decode(chunk) if decode_unicode else chunk.decode("utf-8")
            pending += fragment
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                yield line
        tail = pending + (decoder.decode(b"", final=True) if decode_unicode else "")
        if tail:
            yield tail

    def close(self) -> None:
        self.closed = True


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}"


def assert_no_mojibake(test_case: unittest.TestCase, text: str) -> None:
    for marker in MOJIBAKE_MARKERS:
        test_case.assertNotIn(marker, text)


class ArvanProviderStreamingTests(unittest.TestCase):
    def test_answer_deltas_stream_in_order_and_payload_requests_streaming(self) -> None:
        response = FakeStreamResponse(
            [
                sse({"choices": [{"delta": {"content": "first "}, "finish_reason": None}]}),
                sse({"choices": [{"delta": {"content": "second"}, "finish_reason": "stop"}]}),
                "data: [DONE]",
            ]
        )
        provider = ArvanProvider(deployment())
        with patch("src.core.llm.providers.arvan.requests.post", return_value=response) as post:
            events = list(
                provider.stream_chat(
                    [{"role": "user", "content": "hello"}],
                    max_tokens=128,
                    temperature=0.2,
                    top_p=0.9,
                    timeout_seconds=20,
                )
            )

        self.assertEqual([event.text for event in events if event.type == "answer_delta"], ["first ", "second"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertTrue(response.closed)

    def test_reasoning_answer_usage_and_finish_reason_remain_separate(self) -> None:
        usage = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
        response = FakeStreamResponse(
            [
                sse({"choices": [{"delta": {"reasoning_content": "explicit thought"}, "finish_reason": None}]}),
                sse({"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}),
                sse({"choices": [], "usage": usage}),
                "data: [DONE]",
            ]
        )
        with patch("src.core.llm.providers.arvan.requests.post", return_value=response):
            events = list(
                ArvanProvider(deployment()).stream_chat(
                    [{"role": "user", "content": "hello"}],
                    max_tokens=128,
                    temperature=0.2,
                    top_p=0.9,
                    timeout_seconds=20,
                )
            )

        self.assertEqual([event.text for event in events if event.type == "reasoning_delta"], ["explicit thought"])
        self.assertEqual([event.text for event in events if event.type == "answer_delta"], ["answer"])
        self.assertEqual(next(event.data for event in events if event.type == "usage"), usage)
        done = next(event for event in events if event.type == "done")
        self.assertEqual(done.data["finish_reason"], "stop")
        self.assertEqual(done.data["usage"], usage)

    def test_router_purpose_cannot_use_streaming_transport(self) -> None:
        provider = ArvanProvider(deployment())
        with patch("src.core.llm.providers.arvan.requests.post") as post:
            with self.assertRaisesRegex(LLMError, "only for final chat"):
                list(
                    provider.stream_chat(
                        [{"role": "user", "content": "route"}],
                        max_tokens=128,
                        temperature=0.0,
                        top_p=0.1,
                        timeout_seconds=5,
                        purpose="intent_router",
                    )
                )
        post.assert_not_called()

    def test_unicode_answer_survives_split_utf8_transport_chunks(self) -> None:
        wire_text = "\n".join(
            [
                sse({"choices": [{"delta": {"content": UNICODE_TEXT}, "finish_reason": "stop"}]}),
                "data: [DONE]",
                "",
            ]
        )
        response = SplitByteStreamResponse([bytes([value]) for value in wire_text.encode("utf-8")])

        with patch("src.core.llm.providers.arvan.requests.post", return_value=response):
            events = list(
                ArvanProvider(deployment()).stream_chat(
                    [{"role": "user", "content": "unicode"}],
                    max_tokens=128,
                    temperature=0.2,
                    top_p=0.9,
                    timeout_seconds=20,
                )
            )

        answer = "".join(event.text for event in events if event.type == "answer_delta")
        self.assertEqual(answer, UNICODE_TEXT)
        assert_no_mojibake(self, answer)
        self.assertTrue(response.closed)


class FakeStreamingLLM:
    def __init__(self, events: list[LLMStreamEvent | Exception], *, fallback_text: str = "fallback") -> None:
        self.events = list(events)
        self.fallback_text = fallback_text
        self.chat_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMProviderResult:
        self.chat_calls.append({"messages": messages, **kwargs})
        text = GENERAL_ROUTE if kwargs.get("purpose") == "intent_router" else self.fallback_text
        return LLMProviderResult(
            text=text,
            provider="fake",
            model="fixture-chat-model",
            deployment="glm",
            finish_reason="stop",
            usage={"completion_tokens": 2},
            status_code=200,
        )

    def stream_chat(self, messages: list[dict[str, str]], **kwargs: Any):
        self.stream_calls.append({"messages": messages, **kwargs})
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def provider_done(*, usage: dict[str, Any] | None = None) -> LLMStreamEvent:
    return LLMStreamEvent(
        "done",
        data={
            "provider": "fake",
            "model": "fixture-chat-model",
            "deployment": "glm",
            "finish_reason": "stop",
            "usage": usage or {},
            "latency_ms": 5,
            "status_code": 200,
            "stream_terminated": True,
        },
    )


class CopilotServiceStreamingTests(unittest.TestCase):
    def test_exact_greeting_bypasses_router_planner_providers_and_final_model(self) -> None:
        llm = FakeStreamingLLM([])
        memory = MemoryStore(10)
        service = CopilotService(service_settings(), llm, memory)  # type: ignore[arg-type]

        result = service.chat("hey", "fast-session", request_id="fast-request")

        self.assertEqual(result["provider"], "deterministic")
        self.assertEqual(llm.chat_calls, [])
        self.assertEqual(llm.stream_calls, [])
        self.assertEqual(memory.get("fast-session")[0]["content"], "hey")
        self.assertIsNone(service.routing_state_store.get("fast-session").active_ip)

    def test_streamed_thanks_preserves_contract_without_llm(self) -> None:
        llm = FakeStreamingLLM([])
        service = CopilotService(service_settings(), llm, MemoryStore(10))  # type: ignore[arg-type]

        events = list(service.chat_stream("thank you", "thanks-session", request_id="thanks-request"))

        self.assertEqual([event.type for event in events], ["answer_delta", "done"])
        self.assertEqual(events[-1].data["provider"], "deterministic")
        self.assertEqual(llm.chat_calls, [])
        self.assertEqual(llm.stream_calls, [])

    def test_greeting_prefixed_substantive_request_uses_normal_workflow(self) -> None:
        llm = FakeStreamingLLM([], fallback_text="normal")
        service = CopilotService(service_settings(), llm, MemoryStore(10))  # type: ignore[arg-type]

        result = service.chat("hey, explain phishing", "normal-session", request_id="normal-request")

        self.assertEqual(result["answer"], "normal")
        self.assertEqual([call["purpose"] for call in llm.chat_calls], ["intent_router", "chat"])

    def test_router_is_non_streaming_and_final_answer_is_stored_once(self) -> None:
        llm = FakeStreamingLLM(
            [
                LLMStreamEvent("answer_delta", text="streamed "),
                LLMStreamEvent("answer_delta", text="answer"),
                provider_done(usage={"completion_tokens": 2}),
            ]
        )
        memory = MemoryStore(10)
        service = CopilotService(service_settings(), llm, memory)  # type: ignore[arg-type]

        events = list(service.chat_stream("What is phishing?", "stream-session", request_id="stream-request"))

        self.assertEqual([call["purpose"] for call in llm.chat_calls], ["intent_router"])
        self.assertEqual([call["purpose"] for call in llm.stream_calls], ["chat"])
        self.assertEqual(
            memory.get("stream-session"),
            [
                {"role": "user", "content": "What is phishing?"},
                {"role": "assistant", "content": "streamed answer"},
            ],
        )
        self.assertEqual(sum(event.type == "done" for event in events), 1)

    def test_provider_without_streaming_falls_back_before_answer(self) -> None:
        class NonStreamingLLM:
            def __init__(self) -> None:
                self.calls = 0

            def chat(self, messages, **kwargs):
                del messages, kwargs
                self.calls += 1
                return LLMProviderResult("fallback", "fake", "fixture", deployment="glm")

        llm = NonStreamingLLM()
        service = CopilotService(service_settings(), llm, MemoryStore(0))  # type: ignore[arg-type]
        emitted: list[LLMStreamEvent] = []
        metrics = self._metrics()
        result = service._stream_final_model(
            [{"role": "user", "content": "hello"}],
            request_id="fallback",
            max_tokens=100,
            temperature=0.2,
            top_p=0.9,
            timeout_seconds=10,
            sink=emitted.append,
            metrics=metrics,
        )

        self.assertEqual(result.text, "fallback")
        self.assertEqual(llm.calls, 1)
        self.assertEqual([event.text for event in emitted], ["fallback"])
        self.assertFalse(metrics["streaming_used"])

    def test_partial_stream_failure_does_not_call_non_streaming_model(self) -> None:
        llm = FakeStreamingLLM(
            [
                LLMStreamEvent("answer_delta", text="partial"),
                LLMError("broken", reason="provider_stream_transport_error"),
            ]
        )
        service = CopilotService(service_settings(), llm, MemoryStore(0))  # type: ignore[arg-type]
        with self.assertRaisesRegex(LLMError, "interrupted"):
            service._stream_final_model(
                [{"role": "user", "content": "hello"}],
                request_id="partial",
                max_tokens=100,
                temperature=0.2,
                top_p=0.9,
                timeout_seconds=10,
                sink=lambda event: None,
                metrics=self._metrics(),
            )
        self.assertEqual(llm.chat_calls, [])

    def test_usage_finish_reason_and_explicit_reasoning_are_preserved(self) -> None:
        usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        llm = FakeStreamingLLM(
            [
                LLMStreamEvent("reasoning_delta", text="explicit"),
                LLMStreamEvent("answer_delta", text="answer"),
                LLMStreamEvent("usage", data=usage),
                provider_done(usage=usage),
            ]
        )
        service = CopilotService(service_settings(llm_expose_reasoning=True), llm, MemoryStore(0))  # type: ignore[arg-type]
        emitted: list[LLMStreamEvent] = []
        result = service._stream_final_model(
            [{"role": "user", "content": "hello"}],
            request_id="metadata",
            max_tokens=100,
            temperature=0.2,
            top_p=0.9,
            timeout_seconds=10,
            sink=emitted.append,
            metrics=self._metrics(),
        )

        self.assertEqual(result.usage, usage)
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual([event.type for event in emitted], ["reasoning_delta", "answer_delta", "usage"])

    def test_existing_non_streaming_chat_path_remains_non_streaming(self) -> None:
        llm = FakeStreamingLLM([], fallback_text="non-stream answer")
        service = CopilotService(service_settings(), llm, MemoryStore(10))  # type: ignore[arg-type]

        result = service.chat("What is phishing?", "non-stream", request_id="non-stream-request")

        self.assertEqual(result["answer"], "non-stream answer")
        self.assertEqual([call["purpose"] for call in llm.chat_calls], ["intent_router", "chat"])
        self.assertEqual(llm.stream_calls, [])

    def test_existing_non_streaming_chat_path_preserves_unicode(self) -> None:
        llm = FakeStreamingLLM([], fallback_text=UNICODE_TEXT)
        service = CopilotService(service_settings(), llm, MemoryStore(10))  # type: ignore[arg-type]

        result = service.chat("Unicode please", "non-stream-unicode", request_id="non-stream-unicode")

        self.assertEqual(result["answer"], UNICODE_TEXT)
        assert_no_mojibake(self, result["answer"])
        self.assertEqual(llm.stream_calls, [])

    @staticmethod
    def _metrics() -> dict[str, Any]:
        return {
            "streaming_requested": True,
            "streaming_used": False,
            "first_reasoning_chunk_latency_ms": None,
            "first_answer_chunk_latency_ms": None,
            "stream_chunk_count": 0,
            "reasoning_chunk_count": 0,
            "answer_chunk_count": 0,
            "stream_completed": False,
            "stream_error_type": "",
        }


class SSEAndStreamlitContractTests(unittest.TestCase):
    def test_sse_round_trip_keeps_reasoning_and_answer_separate(self) -> None:
        records = [
            encode_sse_event(LLMStreamEvent("reasoning_delta", text="why")),
            encode_sse_event(LLMStreamEvent("answer_delta", text="what")),
            encode_sse_event(LLMStreamEvent("done")),
        ]
        lines = "".join(records).splitlines()
        parsed = list(parse_sse_events(lines))

        self.assertEqual(collect_visible_stream(parsed), ("why", "what", True, ""))

    def test_sse_unicode_round_trip_is_valid_and_survives_split_bytes(self) -> None:
        records = [
            encode_sse_event(LLMStreamEvent("answer_delta", text=UNICODE_TEXT)),
            encode_sse_event(LLMStreamEvent("done")),
        ]
        wire_text = "".join(records)
        byte_chunks = [bytes([value]) for value in wire_text.encode("utf-8")]

        parsed = list(parse_sse_events(byte_chunks))
        _, answer, completed, error = collect_visible_stream(parsed)

        self.assertEqual(answer, UNICODE_TEXT)
        self.assertTrue(completed)
        self.assertEqual(error, "")
        assert_no_mojibake(self, wire_text)
        assert_no_mojibake(self, answer)
        for event in parsed:
            json.dumps(event, ensure_ascii=False)

    def test_streaming_response_declares_utf8_sse_media_type(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn('media_type="text/event-stream; charset=utf-8"', source)

    def test_missing_reasoning_still_completes_answer_normally(self) -> None:
        events = [
            {"type": "answer_delta", "text": "normal answer"},
            {"type": "done"},
        ]
        self.assertEqual(collect_visible_stream(events), ("", "normal answer", True, ""))

    def test_previous_sidebar_and_bottom_input_fixes_remain_present(self) -> None:
        source = (Path(__file__).resolve().parents[2] / "app_st.py").read_text(encoding="utf-8")
        chat_ui = (Path(__file__).resolve().parents[1] / "web" / "chat_ui.py").read_text(encoding="utf-8")
        history_loop = chat_ui.index("for item in messages:")
        input_call = chat_ui.index("prompt = st.chat_input(")

        self.assertLess(history_loop, input_call)
        self.assertIn("render_conversation_chat(", source)
        self.assertIn("LLM: {get_active_llm_label()}", source)
        self.assertNotIn("RAG: planned", source)
        self.assertNotIn('st.session_state.messages.append({"role": "user"', source)
        self.assertNotIn('st.session_state.messages.append({"role": "assistant"', source)


if __name__ == "__main__":
    unittest.main()
