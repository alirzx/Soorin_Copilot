"""Focused tests for detection context provider, cache, and Copilot composition."""

from __future__ import annotations

import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from src.config.settings import get_settings
from src.core.context.composer import ContextComposer
from src.core.context.models import (
    CopilotContextPackage,
    DetectionProviderResult,
    GraphProviderResult,
    ProviderProvenance,
    ResolvedEntity,
)
from src.core.context.providers.detection import DetectionContextProvider
from src.core.copilot.service import CopilotService
from src.core.detection import adapt_asset_detection
from src.core.detection.models import RawAssetDetectionResponse
from src.core.llm.providers.base import LLMProviderResult
from src.core.memory.store import MemoryStore
from src.core.memory.routing_state import SessionRoutingState, SessionRoutingStateStore
from src.core.product_client.errors import ProductApiError


def make_settings(**overrides: Any):
    values = {
        "llm_provider": "fake",
        "arvan_model": "fake",
        "copilot_human_trace_enabled": False,
        "chat_store_history": False,
        "detection_cache_enabled": True,
        "detection_cache_ttl_seconds": 300,
        "detection_stale_on_error": True,
    }
    values.update(overrides)
    return replace(
        get_settings(),
        **values,
    )


def sample_payload(*, asset_found: bool = True) -> dict[str, Any]:
    return {
        "ip": "192.168.21.1",
        "assetFound": asset_found,
        "storedTag": "Endpoint",
        "storedSubTag": "Workstation",
        "detection": {
            "primaryRole": "Domain Joined Workstation",
            "confidence": 0.91,
            "topRoles": [{"role": "Domain Joined Workstation"}],
            "vendor": "Microsoft",
            "product": "Windows 10",
        },
        "tagging": {"tag": "Endpoint", "subTag": "Windows", "confidence": 0.88},
        "matchedRules": [
            {
                "id": "r1",
                "code": "windows_identity",
                "name": "Windows identity",
                "confidence": 0.9,
                "evidence": ["os_is_windows=true", "kerberos_server=false"],
            }
        ],
        "signals": {
            "extended": {
                "outbound_ratio_pct": 99.5,
                "kerberos_server": False,
                "is_domain_controller": False,
                "external_peer_count": 0,
            },
            "normalized": {"primary_role": "Domain Joined Workstation"},
        },
    }


def evidence(asset_found: bool = True):
    raw = RawAssetDetectionResponse.model_validate(sample_payload(asset_found=asset_found))
    return adapt_asset_detection(raw, fetched_at=datetime(2026, 7, 12, tzinfo=timezone.utc))


class FakeProductClient:
    def __init__(self, responses: list[RawAssetDetectionResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def get_asset_detection(self, ip: str, *, request_id: str = "") -> RawAssetDetectionResponse:
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeLLMClient:
    def __init__(self, responses: list[LLMProviderResult]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, **kwargs) -> LLMProviderResult:
        self.calls.append({"messages": messages, **kwargs})
        return self.responses.pop(0)


def fake_result(text: str) -> LLMProviderResult:
    return LLMProviderResult(text=text, provider="fake", model="fake", finish_reason="stop", status_code=200)


class FakeGraphProvider:
    def __init__(self, result: GraphProviderResult | Exception) -> None:
        self.result = result
        self.calls = 0
        self.last_entity = None
        self.last_route = None

    def provide(self, *_args: Any, **_kwargs: Any) -> GraphProviderResult:
        self.calls += 1
        self.last_entity = _args[0] if _args else None
        self.last_route = _kwargs.get("route")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeDetectionProvider:
    def __init__(self, result: DetectionProviderResult | Exception) -> None:
        self.result = result
        self.calls = 0
        self.last_ip = ""
        self.last_detail = ""

    def fetch(self, *_args: Any, **_kwargs: Any) -> DetectionProviderResult:
        self.calls += 1
        self.last_ip = str(_args[0]) if _args else ""
        self.last_detail = str(_args[1]) if len(_args) > 1 else ""
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def detection_result(status: str = "available", *, detail: str = "summary") -> DetectionProviderResult:
    item = evidence()
    return DetectionProviderResult(
        provider="detection",
        status=status,  # type: ignore[arg-type]
        detail=detail,  # type: ignore[arg-type]
        ip=item.ip,
        evidence=item if status in {"available", "not_found"} else None,
        rendered_context="[SOORIN ASSET DETECTION EVIDENCE]\nPrimary role: Domain Joined Workstation"
        if status in {"available", "not_found"}
        else "",
        provenance=ProviderProvenance(source="product_asset_detection", status=status),  # type: ignore[arg-type]
        cache_hit=True,
        cache_age_seconds=12,
        stale=False,
        latency_ms=3,
    )


def graph_result(status: str = "available") -> GraphProviderResult:
    entity = ResolvedEntity(type="ip", value="192.168.21.1", source="message")
    return GraphProviderResult(
        provider="graph",
        status=status,  # type: ignore[arg-type]
        target_entity=entity,
        context={
            "target_ip": entity.value,
            "target_ips": [entity.value],
            "node_found": status == "available",
            "scope": "node_summary",
            "direction": "both",
            "depth": 0,
            "inbound_total": 1,
            "outbound_total": 4,
            "bidirectional_total": 0,
            "inbound_retrieved": 1,
            "outbound_retrieved": 4,
            "bidirectional_retrieved": 0,
            "candidate_node_count": 5,
            "retrieved_node_count": 5,
            "candidate_edge_count": 5,
            "retrieved_edge_count": 5,
            "nodes": [{"id": entity.value, "hop": 0}],
            "edges": [],
        },
        provenance=ProviderProvenance(source="observed_communication_graph", status=status),  # type: ignore[arg-type]
        latency_ms=2,
    )


class DetectionProviderCacheTests(unittest.TestCase):
    def raw(self, *, asset_found: bool = True) -> RawAssetDetectionResponse:
        return RawAssetDetectionResponse.model_validate(sample_payload(asset_found=asset_found))

    def test_cache_miss_performs_one_product_call_and_renders_summary(self) -> None:
        client = FakeProductClient([self.raw()])
        provider = DetectionContextProvider(make_settings(), client)  # type: ignore[arg-type]
        result = provider.fetch("192.168.21.1", "summary", "req-1")
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.status, "available")
        self.assertFalse(result.cache_hit)
        self.assertIn("[SOORIN ASSET DETECTION EVIDENCE]", result.rendered_context)

    def test_fresh_cache_hit_performs_no_new_product_call(self) -> None:
        client = FakeProductClient([self.raw()])
        provider = DetectionContextProvider(make_settings(), client)  # type: ignore[arg-type]
        provider.fetch("192.168.21.1", "summary", "req-1")
        result = provider.fetch("192.168.21.1", "summary", "req-2")
        self.assertEqual(client.calls, 1)
        self.assertTrue(result.cache_hit)

    def test_expired_cache_refreshes(self) -> None:
        client = FakeProductClient([self.raw(), self.raw()])
        provider = DetectionContextProvider(make_settings(detection_cache_ttl_seconds=1), client)  # type: ignore[arg-type]
        provider.fetch("192.168.21.1", "summary", "req-1")
        provider._cache["192.168.21.1"] = replace(provider._cache["192.168.21.1"], stored_at=time.time() - 10)
        result = provider.fetch("192.168.21.1", "summary", "req-2")
        self.assertEqual(client.calls, 2)
        self.assertFalse(result.stale)

    def test_stale_cache_used_on_fetch_failure(self) -> None:
        client = FakeProductClient([self.raw(), ProductApiError("temporary unavailable")])
        provider = DetectionContextProvider(make_settings(detection_cache_ttl_seconds=1), client)  # type: ignore[arg-type]
        provider.fetch("192.168.21.1", "summary", "req-1")
        provider._cache["192.168.21.1"] = replace(provider._cache["192.168.21.1"], stored_at=time.time() - 10)
        result = provider.fetch("192.168.21.1", "summary", "req-2")
        self.assertEqual(result.status, "available")
        self.assertTrue(result.stale)
        self.assertEqual(result.error_type, "ProductApiError")

    def test_no_stale_cache_returns_unavailable(self) -> None:
        client = FakeProductClient([ProductApiError("temporary unavailable")])
        provider = DetectionContextProvider(make_settings(), client)  # type: ignore[arg-type]
        result = provider.fetch("192.168.21.1", "summary", "req-1")
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.error_type, "ProductApiError")

    def test_asset_found_false_returns_not_found(self) -> None:
        client = FakeProductClient([self.raw(asset_found=False)])
        provider = DetectionContextProvider(make_settings(), client)  # type: ignore[arg-type]
        result = provider.fetch("192.168.21.1", "summary", "req-1")
        self.assertEqual(result.status, "not_found")

    def test_compact_full_mode_renders_detailed_evidence(self) -> None:
        client = FakeProductClient([self.raw()])
        provider = DetectionContextProvider(make_settings(), client)  # type: ignore[arg-type]
        result = provider.fetch("192.168.21.1", "compact_full", "req-1")
        self.assertIn("Matched rules:", result.rendered_context)
        self.assertIn("kerberos_server=false", result.rendered_context)


class CopilotDetectionIntegrationTests(unittest.TestCase):
    def service(self, router_json: str, graph: GraphProviderResult | Exception, detection: DetectionProviderResult | Exception) -> CopilotService:
        llm = FakeLLMClient([fake_result(router_json), fake_result("grounded answer")])
        service = CopilotService(make_settings(), llm, MemoryStore(max_messages=4))
        service.graph_provider = FakeGraphProvider(graph)  # type: ignore[assignment]
        service.detection_provider = FakeDetectionProvider(detection)  # type: ignore[assignment]
        return service

    def asset_route(
        self,
        *,
        requires_graph: bool = True,
        requires_detection: bool = True,
        detail: str = "summary",
        entity_binding: str = "explicit",
        is_followup: bool = False,
    ) -> str:
        return (
            '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,'
            f'"requires_graph":{str(requires_graph).lower()},"requires_detection":{str(requires_detection).lower()},'
            f'"detection_detail":"{detail}","entity_binding":"{entity_binding}",'
            f'"requires_multiple_entities":false,"is_followup":{str(is_followup).lower()},'
            '"classification_confidence":0.95,"reason":"asset"}'
        )

    def test_graph_and_detection_both_available_with_separate_contexts_and_fusion(self) -> None:
        service = self.service(self.asset_route(), graph_result(), detection_result())
        response = service.chat("Tell me about 192.168.21.1.", request_id="req-1")
        context_message = service.llm_client.calls[1]["messages"][1]["content"]  # type: ignore[index]
        self.assertEqual(response["answer"], "grounded answer")
        self.assertIn("[SOORIN ASSET DETECTION EVIDENCE]", context_message)
        self.assertIn("[SOORIN GRAPH EVIDENCE]", context_message)
        self.assertIn("[SOORIN EVIDENCE ALIGNMENT]", context_message)

    def test_graph_available_detection_unavailable_still_answers(self) -> None:
        service = self.service(self.asset_route(), graph_result(), detection_result("unavailable"))
        response = service.chat("Tell me about 192.168.21.1.", request_id="req-2")
        self.assertEqual(response["answer"], "grounded answer")

    def test_detection_available_graph_unavailable_still_answers(self) -> None:
        service = self.service(self.asset_route(), graph_result("unavailable"), detection_result())
        response = service.chat("Tell me about 192.168.21.1.", request_id="req-3")
        context_message = service.llm_client.calls[1]["messages"][1]["content"]  # type: ignore[index]
        self.assertEqual(response["answer"], "grounded answer")
        self.assertIn("[SOORIN ASSET DETECTION EVIDENCE]", context_message)

    def test_both_skipped_for_general_question(self) -> None:
        router = (
            '{"intent":"general_knowledge","scope":"none","direction":"none","depth":0,'
            '"requires_graph":false,"requires_detection":false,"detection_detail":"summary",'
            '"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.95,"reason":"general"}'
        )
        service = self.service(router, graph_result(), detection_result())
        response = service.chat("What is a domain-joined workstation?", request_id="req-4")
        self.assertEqual(response["answer"], "grounded answer")
        self.assertEqual(service.graph_provider.calls, 0)  # type: ignore[attr-defined]
        self.assertEqual(service.detection_provider.calls, 0)  # type: ignore[attr-defined]

    def test_provider_failure_does_not_crash_request(self) -> None:
        service = self.service(self.asset_route(), graph_result(), RuntimeError("boom"))
        response = service.chat("Tell me about 192.168.21.1.", request_id="req-5")
        self.assertEqual(response["answer"], "grounded answer")

    def test_trace_renders_detection_fields_and_no_secrets(self) -> None:
        service = self.service(self.asset_route(), graph_result(), detection_result())
        service.settings = make_settings(copilot_human_trace_enabled=True)
        with self.assertLogs("src.core.copilot.trace", level="INFO") as logs:
            service.chat("Tell me about 192.168.21.1.", request_id="req-6")
        text = "\n".join(logs.output)
        self.assertIn("== ASSET DETECTION ==", text)
        self.assertIn("cache_hit", text)
        self.assertIn("graph_dynamic_tokens_approx", text)
        self.assertIn("detection_dynamic_tokens_approx", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("secret-hwid", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("mac_address", text)

    def test_composer_alignment_is_deterministic(self) -> None:
        text = ContextComposer(make_settings()).compose(
            CopilotContextPackage(
                entities=None,  # type: ignore[arg-type]
                graph=graph_result(),
                detection=detection_result(),
            ),
            request_id="req-7",
        )
        self.assertIn("[SOORIN EVIDENCE ALIGNMENT]", text)
        self.assertIn("Detected workstation role and outbound-heavy graph behavior may align.", text)

    def test_active_single_detection_followup_succeeds_without_pre_resolved_entity(self) -> None:
        router_json = (
            '{"intent":"asset_investigation","scope":"none","direction":"none","depth":0,'
            '"requires_graph":false,"requires_detection":true,"detection_detail":"compact_full",'
            '"entity_binding":"active_single","requires_multiple_entities":false,"is_followup":true,'
            '"classification_confidence":0.95,"reason":"detection follow-up"}'
        )
        llm = FakeLLMClient([fake_result(router_json), fake_result("detection answer")])
        state_store = SessionRoutingStateStore()
        state_store.set("s1", SessionRoutingState(active_ip="192.168.30.111"))
        service = CopilotService(make_settings(), llm, MemoryStore(max_messages=4), state_store)
        service.graph_provider = FakeGraphProvider(graph_result())  # type: ignore[assignment]
        service.detection_provider = FakeDetectionProvider(detection_result(detail="compact_full"))  # type: ignore[assignment]

        response = service.chat("Show me more detection evidence.", "s1", request_id="bind-1")

        self.assertEqual(response["answer"], "detection answer")
        self.assertEqual(service.graph_provider.calls, 0)  # type: ignore[attr-defined]
        self.assertEqual(service.detection_provider.calls, 1)  # type: ignore[attr-defined]
        self.assertEqual(state_store.get("s1").active_ip, "192.168.30.111")
        self.assertEqual(state_store.get("s1").last_provider, "detection")

    def test_active_pair_graph_followup_succeeds_without_pre_resolved_pair(self) -> None:
        router_json = (
            '{"intent":"graph_path","scope":"path","direction":"both","depth":0,'
            '"requires_graph":true,"requires_detection":false,"detection_detail":"summary",'
            '"entity_binding":"active_pair","requires_multiple_entities":true,"is_followup":true,'
            '"classification_confidence":0.95,"reason":"pair follow-up"}'
        )
        llm = FakeLLMClient([fake_result(router_json), fake_result("path answer")])
        state_store = SessionRoutingStateStore()
        state_store.set("s2", SessionRoutingState(active_entities=("192.168.30.111", "192.168.30.112")))
        service = CopilotService(make_settings(), llm, MemoryStore(max_messages=4), state_store)
        service.graph_provider = FakeGraphProvider(graph_result())  # type: ignore[assignment]
        service.detection_provider = FakeDetectionProvider(detection_result())  # type: ignore[assignment]

        response = service.chat("Find the path now.", "s2", request_id="bind-2")

        self.assertEqual(response["answer"], "path answer")
        self.assertEqual(service.graph_provider.calls, 1)  # type: ignore[attr-defined]
        self.assertEqual(service.detection_provider.calls, 0)  # type: ignore[attr-defined]
        self.assertEqual(state_store.get("s2").active_entities, ("192.168.30.111", "192.168.30.112"))

    def test_trace_contains_binding_fields(self) -> None:
        llm = FakeLLMClient([fake_result(self.asset_route()), fake_result("answer")])
        service = CopilotService(make_settings(copilot_human_trace_enabled=True), llm, MemoryStore(max_messages=4))
        service.graph_provider = FakeGraphProvider(graph_result())  # type: ignore[assignment]
        service.detection_provider = FakeDetectionProvider(detection_result())  # type: ignore[assignment]

        with self.assertLogs("src.core.copilot.trace", level="INFO") as logs:
            service.chat("Tell me about 192.168.21.1.", request_id="bind-trace")

        text = "\n".join(logs.output)
        self.assertIn("== ENTITY BINDING ==", text)
        self.assertIn("requested_entity_binding", text)
        self.assertIn("resolved_entity_binding", text)
        self.assertIn("materialized_entities", text)

    def test_ui_selected_ip_reaches_graph_and_detection_and_updates_state(self) -> None:
        router_json = (
            '{"intent":"general_knowledge","scope":"none","direction":"none","depth":0,'
            '"requires_graph":false,"requires_detection":false,"detection_detail":"summary",'
            '"entity_binding":"none","requires_multiple_entities":false,"is_followup":false,'
            '"classification_confidence":0.95,"reason":"glm missed UI subject"}'
        )
        llm = FakeLLMClient([fake_result(router_json), fake_result("ui answer")])
        state_store = SessionRoutingStateStore()
        state_store.set("ui", SessionRoutingState(active_ip="192.168.30.111"))
        service = CopilotService(make_settings(), llm, MemoryStore(max_messages=4), state_store)
        graph_provider = FakeGraphProvider(graph_result())
        detection_provider = FakeDetectionProvider(detection_result())
        service.graph_provider = graph_provider  # type: ignore[assignment]
        service.detection_provider = detection_provider  # type: ignore[assignment]

        response = service.chat(
            "What is selected?",
            "ui",
            ui_context={"selected_ip": "192.168.30.222"},
            request_id="ui-bind-1",
        )

        self.assertEqual(response["answer"], "ui answer")
        self.assertEqual(graph_provider.last_entity.value, "192.168.30.222")
        self.assertEqual(graph_provider.last_entity.source, "ui")
        self.assertEqual(detection_provider.last_ip, "192.168.30.222")
        self.assertEqual(state_store.get("ui").active_ip, "192.168.30.222")

    def test_explicit_message_ip_overrides_conflicting_ui_for_providers_and_state(self) -> None:
        router_json = (
            '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,'
            '"requires_graph":true,"requires_detection":true,"detection_detail":"summary",'
            '"entity_binding":"explicit","requires_multiple_entities":false,"is_followup":false,'
            '"classification_confidence":0.95,"reason":"explicit asset"}'
        )
        llm = FakeLLMClient([fake_result(router_json), fake_result("explicit answer")])
        state_store = SessionRoutingStateStore()
        state_store.set("explicit-ui", SessionRoutingState(active_ip="192.168.3.99"))
        service = CopilotService(
            make_settings(copilot_human_trace_enabled=True),
            llm,
            MemoryStore(max_messages=4),
            state_store,
        )
        graph_provider = FakeGraphProvider(graph_result())
        detection_provider = FakeDetectionProvider(detection_result())
        service.graph_provider = graph_provider  # type: ignore[assignment]
        service.detection_provider = detection_provider  # type: ignore[assignment]

        with self.assertLogs(level="INFO") as logs:
            response = service.chat(
                "Tell me about 192.168.3.137.",
                "explicit-ui",
                ui_context={"selected_ip": "192.168.3.103"},
                request_id="explicit-ui-bind",
            )

        self.assertEqual(response["answer"], "explicit answer")
        self.assertEqual(graph_provider.last_entity.value, "192.168.3.137")
        self.assertEqual(graph_provider.last_entity.source, "message")
        self.assertEqual(detection_provider.last_ip, "192.168.3.137")
        self.assertEqual(state_store.get("explicit-ui").active_ip, "192.168.3.137")
        text = "\n".join(logs.output)
        self.assertIn("requested_entity_binding=explicit", text)
        self.assertIn("resolved_entity_binding=explicit", text)
        self.assertIn("binding_source=message", text)
        self.assertIn("materialized_entities=192.168.3.137", text)
        self.assertNotIn("ui_entity_takes_authority", text)

    def test_trace_records_normalized_ui_authority(self) -> None:
        router_json = (
            '{"intent":"general_knowledge","scope":"none","direction":"none","depth":0,'
            '"requires_graph":false,"requires_detection":false,"detection_detail":"summary",'
            '"entity_binding":"none","requires_multiple_entities":false,"is_followup":false,'
            '"classification_confidence":0.95,"reason":"glm missed UI subject"}'
        )
        llm = FakeLLMClient([fake_result(router_json), fake_result("ui answer")])
        service = CopilotService(make_settings(copilot_human_trace_enabled=True), llm, MemoryStore(max_messages=4))
        service.graph_provider = FakeGraphProvider(graph_result())  # type: ignore[assignment]
        service.detection_provider = FakeDetectionProvider(detection_result())  # type: ignore[assignment]
        with self.assertLogs("src.core.copilot.trace", level="INFO") as logs:
            service.chat(
                "What is selected?",
                "ui-trace",
                ui_context={"selected_ip": "192.168.30.222"},
                request_id="ui-bind-trace",
            )
        text = "\n".join(logs.output)
        self.assertIn("resolved_entity_binding", text)
        self.assertIn("ui_entity_takes_authority", text)
        self.assertIn("ui_subject_requires_asset_route", text)
        self.assertIn("192.168.30.222", text)

    def test_pure_graph_route_skips_detection(self) -> None:
        router_json = (
            '{"intent":"graph_neighbors","scope":"one_hop","direction":"both","depth":1,'
            '"requires_graph":true,"requires_detection":true,"detection_detail":"summary",'
            '"entity_binding":"explicit","requires_multiple_entities":false,"is_followup":false,'
            '"classification_confidence":0.95,"reason":"graph only"}'
        )
        service = self.service(router_json, graph_result(), detection_result())
        service.chat("Show connections for 192.168.21.1.", request_id="graph-only")
        self.assertEqual(service.graph_provider.calls, 1)  # type: ignore[attr-defined]
        self.assertEqual(service.detection_provider.calls, 0)  # type: ignore[attr-defined]

    def test_multi_entity_route_skips_detection(self) -> None:
        router_json = (
            '{"intent":"graph_path","scope":"path","direction":"both","depth":0,'
            '"requires_graph":true,"requires_detection":true,"detection_detail":"compact_full",'
            '"entity_binding":"explicit","requires_multiple_entities":true,"is_followup":false,'
            '"classification_confidence":0.95,"reason":"path"}'
        )
        service = self.service(router_json, graph_result(), detection_result())
        service.chat("Find path between 192.168.21.1 and 192.168.21.2.", request_id="multi-no-detection")
        self.assertEqual(service.graph_provider.calls, 1)  # type: ignore[attr-defined]
        self.assertEqual(service.detection_provider.calls, 0)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
