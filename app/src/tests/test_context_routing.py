"""Focused tests for LLM-primary graph routing and bounded retrieval."""

from __future__ import annotations

import pickle
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import networkx as nx

from src.config.settings import get_settings
from src.core.context.composer import ContextComposer
from src.core.context.entities import EntityResolver
from src.core.context.intent import GLMIntentRouter, ROUTER_SYSTEM_PROMPT_FALLBACK, validate_router_payload
from src.core.context.models import CopilotContextPackage, GraphProviderResult, ProviderProvenance, ResolvedEntity
from src.core.context.providers.graph import GraphContextProvider
from src.core.context.router import DeterministicFallbackRouter, normalize_intent_route
from src.core.copilot.service import CopilotService
from src.core.copilot.trace import CopilotRequestTrace, render_human_copilot_trace
from src.core.graph.loader import get_cached_graph, load_graph, replace_active_graph, set_graph_path
from src.core.graph.refresh import GraphRefreshService
from src.core.graph.retrieval import GraphRetrievalSpec, retrieve_graph_context
from src.core.graph.service import get_subnet
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult
from src.core.memory.routing_state import SessionRoutingState, SessionRoutingStateStore
from src.core.memory.store import MemoryStore
from src.core.product_client.schemas import ProductTopologyResponse, TopologyConnectionRecord


class FakeLLMClient:
    def __init__(self, responses: list[LLMProviderResult | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, **kwargs) -> LLMProviderResult:
        self.calls.append({"messages": messages, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeProductClient:
    def __init__(self, response: ProductTopologyResponse | Exception) -> None:
        self.response = response

    def fetch_topology_unique_ip_pairs(self) -> ProductTopologyResponse:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def fake_result(text: str, *, finish_reason: str | None = "stop") -> LLMProviderResult:
    return LLMProviderResult(text=text, provider="fake", model="fake", finish_reason=finish_reason, status_code=200)


def make_settings(**overrides):
    return replace(
        get_settings(),
        llm_provider="fake",
        arvan_model="fake",
        copilot_human_trace_enabled=False,
        intent_router_enabled=True,
        intent_router_min_confidence=0.65,
        intent_router_retry_enabled=True,
        **overrides,
    )


class EntityAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = EntityResolver()

    def test_explicit_ip_overrides_ui_selected_ip(self) -> None:
        result = self.resolver.resolve(
            "Tell me about 10.10.10.10.",
            ui_context={"selected_ip": "192.168.30.115"},
            routing_state=SessionRoutingState(active_ip="192.168.0.149"),
        )
        self.assertEqual(result.primary_entity.value, "10.10.10.10")
        self.assertEqual(result.primary_entity.source, "message")

    def test_ui_selected_ip_overrides_conversation_entity(self) -> None:
        result = self.resolver.resolve(
            "What is this?",
            ui_context={"selected_ip": "192.168.30.115"},
            routing_state=SessionRoutingState(active_ip="192.168.0.149"),
        )
        self.assertEqual(result.primary_entity.value, "192.168.30.115")
        self.assertEqual(result.primary_entity.source, "ui")

    def test_conversation_entity_used_only_without_explicit_or_ui(self) -> None:
        result = self.resolver.resolve(
            "What do we know about it?",
            routing_state=SessionRoutingState(active_ip="192.168.30.115"),
        )
        self.assertEqual(result.primary_entity.source, "conversation")

    def test_possessive_single_entity_references_use_active_ip(self) -> None:
        state = SessionRoutingState(active_ip="192.168.21.1")
        phrases = [
            "Give its data.",
            "Show its connections.",
            "Show its inbound connections.",
            "Show its outbound connections.",
            "List its neighbors.",
            "List its peers.",
            "Explain its behavior.",
            "Show its topology.",
            "What is its role?",
            "Tell me more about it.",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                result = self.resolver.resolve(phrase, routing_state=state)
                self.assertEqual(result.status, "resolved")
                self.assertEqual(result.entity_mode, "single")
                self.assertEqual(result.primary_entity.value, "192.168.21.1")
                self.assertEqual(result.primary_entity.source, "conversation")

    def test_possessive_reference_does_not_override_new_explicit_ip(self) -> None:
        result = self.resolver.resolve(
            "Give its data for 192.168.21.2.",
            routing_state=SessionRoutingState(active_ip="192.168.21.1"),
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.entity_mode, "single")
        self.assertEqual(result.primary_entity.value, "192.168.21.2")
        self.assertEqual(result.primary_entity.source, "message")

    def test_two_explicit_entities_are_preserved_for_path(self) -> None:
        result = self.resolver.resolve("Find path between 192.168.30.115 and 192.168.0.149")
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.entity_mode, "multiple")
        self.assertIsNone(result.primary_entity)
        self.assertEqual([entity.value for entity in result.entities], ["192.168.30.115", "192.168.0.149"])

    def test_pair_followup_resolves_from_active_entities(self) -> None:
        result = self.resolver.resolve(
            "Now find the shortest path between them.",
            routing_state=SessionRoutingState(active_entities=("192.168.30.100", "192.168.30.101")),
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.entity_mode, "multiple")
        self.assertEqual([entity.value for entity in result.entities], ["192.168.30.100", "192.168.30.101"])

    def test_pair_reference_phrases_resolve_from_active_entities(self) -> None:
        state = SessionRoutingState(active_entities=("192.168.30.100", "192.168.30.101"))
        phrases = [
            "Compare them.",
            "Compare both.",
            "Show those assets.",
            "Show their shared peers.",
            "Which one has broader outbound reach?",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                result = self.resolver.resolve(phrase, routing_state=state)
                self.assertEqual(result.status, "resolved")
                self.assertEqual(result.entity_mode, "multiple")
                self.assertEqual([entity.value for entity in result.entities], ["192.168.30.100", "192.168.30.101"])

    def test_reference_plus_explicit_ip_uses_active_ip_as_pair(self) -> None:
        result = self.resolver.resolve(
            "Now compare it with 192.168.30.144.",
            routing_state=SessionRoutingState(active_ip="192.168.30.115"),
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.entity_mode, "multiple")
        self.assertEqual([entity.value for entity in result.entities], ["192.168.30.115", "192.168.30.144"])

    def test_invalid_ip_candidate_is_not_resolved(self) -> None:
        result = self.resolver.resolve("Tell me about 999.999.999.999")
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.entity_mode, "invalid")

    def test_topic_detachment_suppresses_context_for_turn(self) -> None:
        result = self.resolver.resolve(
            "Not about this asset; explain lateral movement generally.",
            ui_context={"selected_ip": "192.168.30.115"},
            routing_state=SessionRoutingState(active_ip="192.168.30.115"),
        )
        self.assertEqual(result.status, "none")
        self.assertTrue(result.reference_suppressed)


class RouterSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entities = EntityResolver().resolve("Tell me about 192.168.30.115")
        self.two_entities = EntityResolver().resolve("Path from 192.168.30.115 to 192.168.0.149")

    def payload(self, **overrides):
        data = {
            "intent": "graph_neighbors",
            "scope": "one_hop",
            "direction": "both",
            "depth": 1,
            "requires_graph": True,
            "requires_multiple_entities": False,
            "is_followup": False,
            "classification_confidence": 0.9,
            "reason": "ok",
        }
        data.update(overrides)
        return data

    def test_reject_unsupported_intent_scope_direction_and_inherit(self) -> None:
        with self.assertRaises(ValueError):
            validate_router_payload(self.payload(intent="bad"), self.entities, min_confidence=0.65)
        with self.assertRaises(ValueError):
            validate_router_payload(self.payload(scope="bad"), self.entities, min_confidence=0.65)
        with self.assertRaises(ValueError):
            validate_router_payload(self.payload(direction="inherit"), self.entities, min_confidence=0.65)

    def test_reject_invalid_scope_depth_combinations(self) -> None:
        cases = [
            {"scope": "full_neighbors", "depth": 2},
            {"scope": "two_hop", "depth": 1},
            {"intent": "graph_path", "scope": "one_hop", "depth": 1},
            {"intent": "graph_path", "scope": "path", "depth": 1},
            {"intent": "general_knowledge", "scope": "node_summary", "requires_graph": False, "depth": 0},
            {"depth": 3},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    validate_router_payload(self.payload(**case), self.entities, min_confidence=0.65)

    def test_requires_multiple_entities_needs_exactly_two(self) -> None:
        with self.assertRaises(ValueError):
            validate_router_payload(
                self.payload(intent="graph_relationships", scope="one_hop", requires_multiple_entities=True),
                self.entities,
                min_confidence=0.65,
            )
        decision = validate_router_payload(
            self.payload(intent="graph_path", scope="path", depth=0, requires_multiple_entities=True),
            self.two_entities,
            min_confidence=0.65,
        )
        self.assertEqual(decision.scope, "path")

    def test_multi_entity_neighbors_is_normalized_to_comparison(self) -> None:
        decision = validate_router_payload(
            self.payload(requires_multiple_entities=True),
            self.two_entities,
            min_confidence=0.65,
        )
        self.assertEqual(decision.intent, "graph_relationships")
        self.assertEqual(decision.scope, "multi_entity_comparison")
        self.assertTrue(decision.route_normalized)
        self.assertEqual(decision.route_normalization_reason, "multi_entity_neighbors_to_relationship")

    def test_asset_investigation_node_summary_requires_graph_is_normalized(self) -> None:
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="node_summary",
                direction="none",
                depth=1,
                requires_graph=False,
                requires_multiple_entities=False,
            ),
            self.entities,
            min_confidence=0.65,
        )
        route = normalize_intent_route(decision, self.entities)
        self.assertEqual(decision.intent, "asset_investigation")
        self.assertEqual(decision.scope, "node_summary")
        self.assertEqual(decision.direction, "both")
        self.assertEqual(decision.depth, 0)
        self.assertTrue(decision.requires_graph)
        self.assertFalse(decision.requires_multiple_entities)
        self.assertTrue(decision.route_normalized)
        self.assertEqual(decision.route_normalization_reason, "node_summary_requires_graph")
        self.assertTrue(route.use_graph)
        self.assertEqual(route.decision_source, "glm")

    def test_three_entities_are_rejected_for_graph_routes(self) -> None:
        three = EntityResolver().resolve("Compare 192.168.1.1 192.168.1.2 192.168.1.3")
        with self.assertRaises(ValueError):
            validate_router_payload(
                self.payload(intent="graph_relationships", scope="multi_entity_comparison", requires_multiple_entities=True),
                three,
                min_confidence=0.65,
            )

    def test_high_confidence_unclear_is_valid(self) -> None:
        decision = validate_router_payload(
            self.payload(intent="unclear", scope="none", direction="none", depth=0, requires_graph=False),
            self.entities,
            min_confidence=0.65,
        )
        self.assertEqual(decision.intent, "unclear")

    def test_low_confidence_unclear_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_router_payload(
                self.payload(
                    intent="unclear",
                    scope="none",
                    direction="none",
                    depth=0,
                    requires_graph=False,
                    classification_confidence=0.2,
                ),
                self.entities,
                min_confidence=0.65,
            )


class LLMPrimaryRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self.entities = EntityResolver().resolve("Tell me about 192.168.30.115")

    def test_valid_glm_decision_normalizes_without_fallback(self) -> None:
        router = GLMIntentRouter(
            self.settings,
            FakeLLMClient([fake_result('{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.92,"reason":"asset question"}')]),  # type: ignore[arg-type]
        )
        decision = router.classify("Tell me about 192.168.30.115", self.entities, SessionRoutingState())
        route = normalize_intent_route(decision, self.entities)
        self.assertEqual(route.decision_source, "glm")
        self.assertFalse(route.fallback_used)
        self.assertEqual(route.scope, "node_summary")

    def test_malformed_json_retries_once_then_falls_back(self) -> None:
        router = GLMIntentRouter(self.settings, FakeLLMClient([fake_result("not json"), fake_result("still bad")]))  # type: ignore[arg-type]
        decision = router.classify("192.168.30.115", self.entities, SessionRoutingState())
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.retry_count, 1)

    def test_missing_content_and_finish_reason_length_trigger_retry(self) -> None:
        router = GLMIntentRouter(
            self.settings,
            FakeLLMClient([
                fake_result("", finish_reason="length"),
                fake_result('{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"ok"}'),
            ]),  # type: ignore[arg-type]
        )
        decision = router.classify("192.168.30.115", self.entities, SessionRoutingState())
        self.assertFalse(decision.fallback_used)
        self.assertEqual(decision.retry_count, 1)

    def test_provider_error_and_low_confidence_fall_back(self) -> None:
        router = GLMIntentRouter(self.settings, FakeLLMClient([LLMError("boom", reason="timeout")]))  # type: ignore[arg-type]
        self.assertTrue(router.classify("x", self.entities, SessionRoutingState()).fallback_used)
        low = GLMIntentRouter(
            self.settings,
            FakeLLMClient([fake_result('{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.2,"reason":"low"}')]),  # type: ignore[arg-type]
        )
        self.assertTrue(low.classify("x", self.entities, SessionRoutingState()).fallback_used)

    def test_router_uses_router_specific_generation_settings_and_retry_budget(self) -> None:
        settings = make_settings(
            intent_router_temperature=0.0,
            intent_router_top_p=0.1,
            intent_router_max_tokens=77,
            intent_router_retry_max_tokens=155,
        )
        llm = FakeLLMClient([
            fake_result("", finish_reason="length"),
            fake_result('{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"ok"}'),
        ])
        router = GLMIntentRouter(settings, llm)  # type: ignore[arg-type]
        decision = router.classify("192.168.30.115", self.entities, SessionRoutingState())
        self.assertFalse(decision.fallback_used)
        self.assertEqual(llm.calls[0]["temperature"], 0.0)
        self.assertEqual(llm.calls[0]["top_p"], 0.1)
        self.assertEqual(llm.calls[0]["max_tokens"], 77)
        self.assertEqual(llm.calls[1]["max_tokens"], 155)

    def test_router_prompt_loads_from_file_and_missing_file_falls_back(self) -> None:
        router = GLMIntentRouter(self.settings, FakeLLMClient([fake_result("{}")]))  # type: ignore[arg-type]
        self.assertIn("classification_confidence", router.system_prompt)
        self.assertIn("multi_entity_comparison", router.system_prompt)
        missing = GLMIntentRouter(
            make_settings(intent_router_system_prompt_path="/tmp/soorin-missing-router-prompt.md"),
            FakeLLMClient([fake_result("{}")]),  # type: ignore[arg-type]
        )
        self.assertEqual(missing.system_prompt, ROUTER_SYSTEM_PROMPT_FALLBACK)
        self.assertLess(len(ROUTER_SYSTEM_PROMPT_FALLBACK), 400)


class GraphRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        graph_path = Path(self.temp_dir.name) / "graph.pkl"
        graph = nx.DiGraph()
        graph.add_edges_from(
            [
                ("a", "b"),
            ]
        )
        graph = nx.DiGraph()
        for node in ["192.168.30.115", "192.168.0.149", "192.168.0.150", "192.168.0.151", "10.0.0.1"]:
            graph.add_node(node)
        graph.add_edge("192.168.0.149", "192.168.30.115")
        graph.add_edge("192.168.0.150", "192.168.30.115")
        graph.add_edge("192.168.30.115", "192.168.0.149")
        graph.add_edge("192.168.30.115", "192.168.0.151")
        graph.add_edge("192.168.0.151", "10.0.0.1")
        with graph_path.open("wb") as handle:
            pickle.dump(graph, handle)
        set_graph_path(graph_path)
        load_graph(force_reload=True)
        self.settings = make_settings(graph_full_neighbors_hard_max=10, graph_two_hop_max_nodes=4, graph_max_edges=4)
        self.provider = GraphContextProvider(self.settings)
        self.entity = ResolvedEntity(type="ip", value="192.168.30.115", source="message")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def route(
        self,
        scope: str,
        direction: str,
        depth: int,
        entities: list[ResolvedEntity] | None = None,
        *,
        intent: str | None = None,
        relationship_mode: str = "none",
    ):
        from src.core.context.models import RouteDecision

        ents = entities or [self.entity]
        return RouteDecision(
            use_graph=True,
            reason="test",
            target_entity=ents[0] if len(ents) == 1 else None,
            target_entities=ents,
            intent=intent or ("graph_neighbors" if scope != "path" else "graph_path"),
            scope=scope,
            direction=direction,
            depth=depth,
            requires_multiple_entities=len(ents) == 2,
            relationship_mode=relationship_mode,
            intent_confidence=1,
        )

    def test_node_summary_counts_total_and_returned_separately(self) -> None:
        result = self.provider.provide(self.entity, route=self.route("node_summary", "both", 0))
        text = ContextComposer(self.settings).compose(CopilotContextPackage(entities=EntityResolver().resolve(self.entity.value), graph=result))
        self.assertEqual(result.context["inbound_total"], 2)
        self.assertIn("inbound_returned", result.context)
        self.assertIn("bidirectional_total", result.context)
        self.assertIn("bidirectional_returned", result.context)
        self.assertEqual(result.context["context_node_count"], 1)
        self.assertEqual(result.context["context_edge_count"], 0)
        self.assertEqual(result.context["inbound_context_included"], 0)
        self.assertEqual(result.context["outbound_context_included"], 0)
        self.assertEqual(result.context["bidirectional_context_included"], 0)
        self.assertIn("model_context_included=0", text)

    def test_subnet_formatter_returns_canonical_cidr(self) -> None:
        self.assertEqual(get_subnet("192.168.0.149"), "192.168.0.0/24")
        self.assertEqual(get_subnet("192.168.21.1"), "192.168.21.0/24")
        self.assertEqual(get_subnet("not-an-ip"), "other")

    def test_full_inbound_and_outbound_return_direct_neighbors(self) -> None:
        inbound = self.provider.provide(self.entity, route=self.route("full_neighbors", "inbound", 1))
        outbound = self.provider.provide(self.entity, route=self.route("full_neighbors", "outbound", 1))
        self.assertEqual(inbound.context["inbound_returned"], 2)
        self.assertEqual(outbound.context["outbound_returned"], 2)

    def test_two_hop_enforces_limits_and_marks_truncation(self) -> None:
        result = self.provider.provide(self.entity, route=self.route("two_hop", "both", 2))
        self.assertEqual(result.context["scope"], "two_hop")
        self.assertLessEqual(result.context["returned_node_count"], self.settings.graph_two_hop_max_nodes)
        self.assertIn("truncated", result.context)

    def test_path_requires_two_entities_and_unknown_node_not_found(self) -> None:
        second = ResolvedEntity(type="ip", value="10.0.0.1", source="message")
        result = self.provider.provide(self.entity, route=self.route("path", "both", 0, [self.entity, second]))
        self.assertTrue(result.context["path_exists"])
        unknown = ResolvedEntity(type="ip", value="192.168.99.99", source="ui")
        missing = self.provider.provide(unknown, route=self.route("node_summary", "both", 0, [unknown]))
        self.assertEqual(missing.status, "not_found")

    def test_full_neighbors_with_33_inbound_peers_are_all_included_in_model_context(self) -> None:
        graph = nx.DiGraph()
        target = "192.168.30.115"
        for index in range(33):
            graph.add_edge(f"192.168.0.{index + 1}", target)
        replace_active_graph(graph, {"active_graph_source": "test"})
        settings = make_settings(graph_full_neighbors_hard_max=100, graph_full_enumeration_max_peers=100)
        entity = ResolvedEntity(type="ip", value=target, source="message")
        result = GraphContextProvider(settings).provide(entity, route=self.route("full_neighbors", "inbound", 1, [entity]))
        package = CopilotContextPackage(
            entities=EntityResolver().resolve(f"List inbound peers for {target}"),
            graph=result,
            provenance=[],
            limitations=[],
        )
        text = ContextComposer(settings).compose(package)
        self.assertEqual(result.context["inbound_retrieved"], 33)
        self.assertEqual(result.context["inbound_context_included"], 33)
        self.assertFalse(result.context["context_truncated"])
        self.assertIn("All 33 requested inbound peers were retrieved and explicitly included.", text)

    def test_directional_context_counts_use_actual_included_directions(self) -> None:
        graph = nx.DiGraph()
        target = "192.168.30.115"
        bidirectional = ["192.168.10.1", "192.168.10.2"]
        inbound_only = [f"192.168.11.{i}" for i in range(1, 26)]
        outbound_only = []
        for peer in [*bidirectional, *inbound_only]:
            graph.add_edge(peer, target)
        for peer in bidirectional:
            graph.add_edge(target, peer)
        replace_active_graph(graph, {"active_graph_source": "test"})
        settings = make_settings(graph_full_neighbors_hard_max=100, graph_full_enumeration_max_peers=100)
        entity = ResolvedEntity(type="ip", value=target, source="message")
        result = GraphContextProvider(settings).provide(entity, route=self.route("full_neighbors", "both", 1, [entity]))
        ContextComposer(settings).compose(CopilotContextPackage(entities=EntityResolver().resolve(target), graph=result))
        self.assertEqual(result.context["inbound_retrieved"], 27)
        self.assertEqual(result.context["outbound_retrieved"], 2)
        self.assertEqual(result.context["bidirectional_retrieved"], 2)
        self.assertEqual(result.context["inbound_context_included"], 27)
        self.assertEqual(result.context["outbound_context_included"], 2)
        self.assertEqual(result.context["bidirectional_context_included"], 2)

    def test_directional_context_counts_under_truncation(self) -> None:
        graph = nx.DiGraph()
        target = "192.168.30.115"
        graph.add_edge("192.168.20.1", target)
        graph.add_edge(target, "192.168.20.1")
        for index in range(2, 5):
            graph.add_edge(f"192.168.20.{index}", target)
        for index in range(2, 63):
            graph.add_edge(target, f"192.168.21.{index}")
        replace_active_graph(graph, {"active_graph_source": "test"})
        settings = make_settings(
            graph_full_neighbors_hard_max=100,
            graph_context_max_enumerated_nodes=20,
            graph_context_max_enumerated_edges=20,
            graph_full_enumeration_max_peers=10,
        )
        entity = ResolvedEntity(type="ip", value=target, source="message")
        result = GraphContextProvider(settings).provide(entity, route=self.route("full_neighbors", "both", 1, [entity]))
        ContextComposer(settings).compose(CopilotContextPackage(entities=EntityResolver().resolve(target), graph=result))
        self.assertEqual(result.context["inbound_retrieved"], 4)
        self.assertEqual(result.context["outbound_retrieved"], 62)
        self.assertEqual(result.context["bidirectional_retrieved"], 1)
        self.assertLessEqual(result.context["inbound_context_included"], 4)
        self.assertLess(result.context["outbound_context_included"], 62)
        self.assertTrue(result.context["context_truncated"])

    def test_large_full_neighbors_separates_retrieved_from_context_included(self) -> None:
        graph = nx.DiGraph()
        target = "192.168.30.115"
        for index in range(120):
            graph.add_edge(target, f"192.168.1.{index + 1}")
        replace_active_graph(graph, {"active_graph_source": "test"})
        settings = make_settings(
            graph_full_neighbors_hard_max=200,
            graph_full_enumeration_max_peers=50,
            graph_context_max_enumerated_nodes=20,
            graph_context_max_enumerated_edges=20,
        )
        entity = ResolvedEntity(type="ip", value=target, source="message")
        result = GraphContextProvider(settings).provide(entity, route=self.route("full_neighbors", "outbound", 1, [entity]))
        package = CopilotContextPackage(entities=EntityResolver().resolve(target), graph=result, provenance=[], limitations=[])
        text = ContextComposer(settings).compose(package)
        self.assertEqual(result.context["outbound_retrieved"], 120)
        self.assertLess(result.context["outbound_context_included"], result.context["outbound_retrieved"])
        self.assertFalse(result.context["retrieval_truncated"])
        self.assertTrue(result.context["context_truncated"])
        self.assertIn("graph_retrieved=120", text)
        self.assertIn("model_context_included=", text)

    def test_two_hop_edge_only_truncation_reports_edge_limit(self) -> None:
        graph = nx.DiGraph()
        target = "192.168.30.115"
        for index in range(5):
            first = f"10.0.0.{index + 1}"
            second = f"10.0.1.{index + 1}"
            graph.add_edge(target, first)
            graph.add_edge(first, second)
        replace_active_graph(graph, {"active_graph_source": "test"})
        settings = make_settings(graph_two_hop_max_nodes=50, graph_max_edges=4)
        entity = ResolvedEntity(type="ip", value=target, source="message")
        context = retrieve_graph_context(
            GraphRetrievalSpec(scope="two_hop", direction="outbound", depth=2, entities=[entity]),
            settings,
        )
        self.assertTrue(context["retrieval_truncated"])
        self.assertEqual(context["retrieval_truncation_reason"], "edge_limit:4")

    def test_two_hop_node_only_and_combined_truncation_reasons(self) -> None:
        graph = nx.DiGraph()
        target = "192.168.30.115"
        for index in range(8):
            graph.add_edge(target, f"10.0.0.{index + 1}")
        replace_active_graph(graph, {"active_graph_source": "test"})
        entity = ResolvedEntity(type="ip", value=target, source="message")

        node_only = retrieve_graph_context(
            GraphRetrievalSpec(scope="two_hop", direction="outbound", depth=2, entities=[entity]),
            make_settings(graph_two_hop_max_nodes=4, graph_max_edges=50),
        )
        self.assertEqual(node_only["retrieval_truncation_reason"], "node_limit:4")

        combined = retrieve_graph_context(
            GraphRetrievalSpec(scope="two_hop", direction="outbound", depth=2, entities=[entity]),
            make_settings(graph_two_hop_max_nodes=4, graph_max_edges=3),
        )
        reason_types = [reason["type"] for reason in combined["retrieval_truncation_reasons"]]
        self.assertEqual(reason_types, ["node_limit", "edge_limit"])

    def test_no_truncation_below_limits(self) -> None:
        graph = nx.DiGraph()
        target = "192.168.30.115"
        graph.add_edge(target, "10.0.0.1")
        replace_active_graph(graph, {"active_graph_source": "test"})
        entity = ResolvedEntity(type="ip", value=target, source="message")
        context = retrieve_graph_context(
            GraphRetrievalSpec(scope="two_hop", direction="outbound", depth=2, entities=[entity]),
            make_settings(graph_two_hop_max_nodes=10, graph_max_edges=10),
        )
        self.assertFalse(context["retrieval_truncated"])
        self.assertIsNone(context["retrieval_truncation_reason"])

    def test_direct_relationship_uses_exact_edge_lookup(self) -> None:
        graph = nx.DiGraph()
        a = "192.168.30.100"
        b = "192.168.30.101"
        graph.add_edge(a, b)
        for index in range(50):
            graph.add_edge(a, f"10.10.0.{index}")
            graph.add_edge(f"10.20.0.{index}", a)
        replace_active_graph(graph, {"active_graph_source": "test"})
        entity_a = ResolvedEntity(type="ip", value=a, source="message")
        entity_b = ResolvedEntity(type="ip", value=b, source="message")
        result = GraphContextProvider(make_settings()).provide(
            None,
            route=self.route("one_hop", "both", 1, [entity_a, entity_b], intent="graph_relationships", relationship_mode="direct"),
        )
        self.assertTrue(result.context["source_present"])
        self.assertTrue(result.context["target_present"])
        self.assertTrue(result.context["forward_edge"])
        self.assertFalse(result.context["reverse_edge"])
        self.assertEqual(result.context["relationship_status"], "forward_direct_relationship")
        self.assertEqual(result.context["target_ips"], [a, b])
        self.assertEqual(result.context["retrieved_edge_count"], 1)
        self.assertLessEqual(result.context["candidate_edge_count"], 2)

    def test_relationship_variants_and_missing_entities(self) -> None:
        a = ResolvedEntity(type="ip", value="192.168.30.100", source="message")
        b = ResolvedEntity(type="ip", value="192.168.30.101", source="message")
        missing = ResolvedEntity(type="ip", value="192.168.30.250", source="message")
        graph = nx.DiGraph()
        graph.add_edge(b.value, a.value)
        replace_active_graph(graph, {"active_graph_source": "test"})
        reverse = GraphContextProvider(make_settings()).provide(
            None,
            route=self.route("one_hop", "both", 1, [a, b], intent="graph_relationships", relationship_mode="direct"),
        )
        self.assertFalse(reverse.context["forward_edge"])
        self.assertTrue(reverse.context["reverse_edge"])
        self.assertEqual(reverse.context["relationship_status"], "reverse_direct_relationship")
        graph.add_edge(a.value, b.value)
        replace_active_graph(graph, {"active_graph_source": "test"})
        both = GraphContextProvider(make_settings()).provide(
            None,
            route=self.route("one_hop", "both", 1, [a, b], intent="graph_relationships", relationship_mode="direct"),
        )
        self.assertTrue(both.context["bidirectional"])
        self.assertEqual(both.context["relationship_status"], "bidirectional_direct_relationship")
        graph.remove_edge(a.value, b.value)
        graph.remove_edge(b.value, a.value)
        replace_active_graph(graph, {"active_graph_source": "test"})
        no_edge = GraphContextProvider(make_settings()).provide(
            None,
            route=self.route("one_hop", "both", 1, [a, b], intent="graph_relationships", relationship_mode="direct"),
        )
        self.assertEqual(no_edge.context["relationship_status"], "no_direct_relationship")
        absent = GraphContextProvider(make_settings()).provide(
            None,
            route=self.route("one_hop", "both", 1, [a, missing], intent="graph_relationships", relationship_mode="direct"),
        )
        self.assertFalse(absent.context["target_present"])
        self.assertEqual(absent.context["relationship_status"], "entity_missing_from_active_graph")
        both_missing = GraphContextProvider(make_settings()).provide(
            None,
            route=self.route("one_hop", "both", 1, [missing, ResolvedEntity(type="ip", value="192.168.30.251", source="message")], intent="graph_relationships", relationship_mode="direct"),
        )
        self.assertFalse(both_missing.context["source_present"])
        self.assertFalse(both_missing.context["target_present"])
        self.assertEqual(both_missing.context["relationship_status"], "entity_missing_from_active_graph")

    def test_comparison_returns_both_summaries_shared_peers_and_limits(self) -> None:
        graph = nx.DiGraph()
        a = "192.168.30.100"
        b = "192.168.30.101"
        for index in range(5):
            peer = f"10.0.0.{index}"
            graph.add_edge(a, peer)
            graph.add_edge(b, peer)
        for index in range(20):
            graph.add_edge(a, f"10.1.0.{index}")
        graph.add_edge(a, b)
        replace_active_graph(graph, {"active_graph_source": "test"})
        entity_a = ResolvedEntity(type="ip", value=a, source="message")
        entity_b = ResolvedEntity(type="ip", value=b, source="message")
        result = GraphContextProvider(make_settings(graph_comparison_max_peers_per_entity=10, graph_comparison_max_shared_peers=3)).provide(
            None,
            route=self.route("multi_entity_comparison", "both", 1, [entity_a, entity_b], intent="graph_relationships", relationship_mode="compare"),
        )
        self.assertEqual(result.context["scope"], "multi_entity_comparison")
        self.assertEqual(result.context["entity_a"]["ip"], a)
        self.assertEqual(result.context["entity_b"]["ip"], b)
        self.assertEqual(result.context["shared_peer_total"], 5)
        self.assertEqual(result.context["shared_peers_retrieved_count"], 3)
        self.assertTrue(result.context["direct_relationship"]["a_to_b"])
        self.assertEqual(result.context["direct_relationship"]["relationship_status"], "forward_direct_relationship")
        self.assertEqual(result.context["target_ips"], [a, b])
        self.assertEqual(result.context["degree_comparison"]["broader_outbound_entity"], a)
        self.assertIn("10.0.0.0/24", result.context["subnet_comparison"]["shared_subnets"])


class GraphRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = make_settings(
            graph_raw_path=str(root / "raw" / "topology_raw.json"),
            graph_pickle_path=str(root / "processed" / "topology_graph.pkl"),
            graph_stats_path=str(root / "processed" / "topology_stats.json"),
            graph_graphml_path=str(root / "processed" / "topology_graph.graphml"),
            graph_gexf_path=str(root / "processed" / "topology_graph.gexf"),
            graph_auto_refresh_enabled=False,
            graph_refresh_keep_raw_snapshots=1,
            graph_refresh_keep_processed_snapshots=1,
            graph_refresh_lock_timeout_seconds=1,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def topology_response(self) -> ProductTopologyResponse:
        records = [
            TopologyConnectionRecord("192.168.0.1", "192.168.0.2"),
            TopologyConnectionRecord("192.168.0.2", "192.168.0.3"),
        ]
        return ProductTopologyResponse(
            raw_payload=[{"src_ip": record.src_ip, "dst_ip": record.dst_ip} for record in records],
            records=records,
            endpoint_path="/topology",
            status_code=200,
            elapsed_seconds=0.01,
        )

    def test_successful_refresh_persists_and_activates_new_graph(self) -> None:
        service = GraphRefreshService(self.settings, FakeProductClient(self.topology_response()))  # type: ignore[arg-type]
        result = service.refresh_once()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.activated)
        self.assertEqual(get_cached_graph().number_of_nodes(), 3)
        self.assertTrue(Path(self.settings.graph_pickle_path).exists())
        self.assertTrue(Path(self.settings.graph_stats_path).exists())
        self.assertEqual(service.status()["consecutive_failures"], 0)

    def test_failed_fetch_preserves_active_graph_and_records_failure(self) -> None:
        initial = nx.DiGraph()
        initial.add_edge("a", "b")
        replace_active_graph(initial, {"active_graph_source": "test"})
        service = GraphRefreshService(self.settings, FakeProductClient(RuntimeError("network down")))  # type: ignore[arg-type]
        result = service.refresh_once()
        self.assertEqual(result.status, "error")
        self.assertFalse(result.activated)
        self.assertEqual(get_cached_graph().number_of_nodes(), 2)
        self.assertEqual(service.status()["consecutive_failures"], 1)

    def test_suspicious_node_drop_rejects_activation(self) -> None:
        initial = nx.DiGraph()
        for index in range(100):
            initial.add_edge(f"10.0.0.{index}", f"10.0.1.{index}")
        replace_active_graph(initial, {"active_graph_source": "test"})
        tiny = ProductTopologyResponse(
            raw_payload=[{"src_ip": "a", "dst_ip": "b"}],
            records=[TopologyConnectionRecord("a", "b")],
            endpoint_path="/topology",
            status_code=200,
            elapsed_seconds=0.01,
        )
        service = GraphRefreshService(self.settings, FakeProductClient(tiny))  # type: ignore[arg-type]
        result = service.refresh_once()
        self.assertEqual(result.status, "error")
        self.assertFalse(result.activated)
        self.assertEqual(get_cached_graph().number_of_nodes(), initial.number_of_nodes())

    def test_failed_required_persistence_preserves_active_graph(self) -> None:
        initial = nx.DiGraph()
        initial.add_edge("a", "b")
        replace_active_graph(initial, {"active_graph_source": "test"})
        service = GraphRefreshService(self.settings, FakeProductClient(self.topology_response()))  # type: ignore[arg-type]
        with patch("src.core.graph.refresh.pickle.dump", side_effect=OSError("disk full")):
            result = service.refresh_once(force=True)
        self.assertEqual(result.status, "error")
        self.assertFalse(result.activated)
        self.assertEqual(get_cached_graph().number_of_edges(), 1)

    def test_overlapping_refresh_is_skipped(self) -> None:
        service = GraphRefreshService(self.settings, FakeProductClient(self.topology_response()))  # type: ignore[arg-type]
        self.assertTrue(service._lock.acquire(timeout=1))  # noqa: SLF001 - intentional concurrency boundary test.
        try:
            result = service.refresh_once()
        finally:
            service._lock.release()  # noqa: SLF001
        self.assertEqual(result.status, "skipped")


class ServiceAndTraceTests(unittest.TestCase):
    def test_state_updates_previous_route_and_general_question_preserves_active_entity(self) -> None:
        settings = make_settings()
        router_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"asset"}'
        llm = FakeLLMClient([fake_result(router_json), fake_result("answer")])
        state_store = SessionRoutingStateStore()
        service = CopilotService(settings, llm, MemoryStore(10), state_store)
        service.graph_provider.provide = lambda *args, **kwargs: GraphProviderResult(  # type: ignore[method-assign]
            provider="graph",
            status="not_found",
            target_entity=ResolvedEntity(type="ip", value="192.168.30.115", source="message"),
            context={"target_ip": "192.168.30.115", "node_found": False, "scope": "node_summary", "direction": "both", "depth": 0},
            provenance=ProviderProvenance(source="observed_communication_graph", status="not_found"),
        )
        service.chat("Tell me about 192.168.30.115", "s1", request_id="r1")
        state = state_store.get("s1")
        self.assertEqual(state.active_ip, "192.168.30.115")
        self.assertEqual(state.previous_scope, "node_summary")

    def test_single_asset_summary_and_possessive_followups_use_graph_and_state(self) -> None:
        settings = make_settings()
        asset_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"asset"}'
        malformed_asset_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"none","depth":1,"requires_graph":false,"requires_multiple_entities":false,"is_followup":true,"classification_confidence":0.9,"reason":"asset followup"}'
        connections_json = '{"intent":"graph_neighbors","scope":"one_hop","direction":"both","depth":1,"requires_graph":true,"requires_multiple_entities":false,"is_followup":true,"classification_confidence":0.9,"reason":"connections"}'
        llm = FakeLLMClient([
            fake_result(asset_json),
            fake_result("asset answer"),
            fake_result(malformed_asset_json),
            fake_result("data answer"),
            fake_result(connections_json),
            fake_result("connections answer"),
            fake_result(asset_json),
            fake_result("details answer"),
        ])
        state_store = SessionRoutingStateStore()
        service = CopilotService(settings, llm, MemoryStore(20), state_store)
        captured_routes = []
        captured_entities = []

        def fake_graph_provider(entity, **kwargs):
            captured_routes.append(kwargs["route"])
            captured_entities.append(entity)
            return GraphProviderResult(
                provider="graph",
                status="available",
                target_entity=entity,
                context={
                    "target_ip": kwargs["route"].target_entity.value,
                    "node_found": True,
                    "scope": kwargs["route"].scope,
                    "direction": kwargs["route"].direction,
                    "depth": kwargs["route"].depth,
                    "nodes": [],
                    "edges": [],
                },
                provenance=ProviderProvenance(source="observed_communication_graph", status="available"),
            )

        service.graph_provider.provide = fake_graph_provider  # type: ignore[method-assign]
        service.chat("Tell me about 192.168.21.1.", "single", request_id="r-single-1")
        service.chat("Give its data.", "single", request_id="r-single-2")
        service.chat("Show its connections.", "single", request_id="r-single-3")
        service.chat("Tell me more about it.", "single", request_id="r-single-4")

        self.assertEqual(len(captured_routes), 4)
        self.assertEqual(captured_routes[0].intent, "asset_investigation")
        self.assertEqual(captured_routes[0].scope, "node_summary")
        self.assertTrue(captured_routes[0].use_graph)
        self.assertEqual(captured_routes[0].decision_source, "glm")
        self.assertFalse(captured_routes[0].fallback_used)
        self.assertEqual(captured_entities[0].source, "message")

        self.assertEqual(captured_routes[1].intent, "asset_investigation")
        self.assertEqual(captured_routes[1].scope, "node_summary")
        self.assertEqual(captured_routes[1].direction, "both")
        self.assertEqual(captured_routes[1].depth, 0)
        self.assertTrue(captured_routes[1].use_graph)
        self.assertTrue(captured_routes[1].route_normalized)
        self.assertEqual(captured_routes[1].route_normalization_reason, "node_summary_requires_graph")
        self.assertEqual(captured_routes[1].decision_source, "glm")
        self.assertEqual(captured_entities[1].source, "conversation")

        self.assertEqual(captured_routes[2].intent, "graph_neighbors")
        self.assertEqual(captured_routes[2].scope, "one_hop")
        self.assertTrue(captured_routes[2].use_graph)
        self.assertEqual(captured_entities[2].source, "conversation")
        self.assertEqual(captured_entities[3].source, "conversation")

        state = state_store.get("single")
        self.assertEqual(state.active_ip, "192.168.21.1")
        self.assertEqual(state.active_entities, ())
        self.assertEqual(state.last_provider, "graph")
        self.assertEqual(state.previous_intent, "asset_investigation")
        self.assertEqual(state.previous_scope, "node_summary")
        self.assertEqual(state.previous_direction, "both")
        self.assertEqual(state.previous_depth, 0)

    def test_two_entity_state_is_preserved_for_followup_path(self) -> None:
        settings = make_settings()
        relationship_json = '{"intent":"graph_relationships","scope":"one_hop","direction":"both","depth":1,"requires_graph":true,"requires_multiple_entities":true,"is_followup":false,"classification_confidence":0.9,"reason":"direct"}'
        path_json = '{"intent":"graph_path","scope":"path","direction":"both","depth":0,"requires_graph":true,"requires_multiple_entities":true,"is_followup":true,"classification_confidence":0.9,"reason":"path"}'
        llm = FakeLLMClient([fake_result(relationship_json), fake_result("answer one"), fake_result(path_json), fake_result("answer two")])
        state_store = SessionRoutingStateStore()
        service = CopilotService(settings, llm, MemoryStore(20), state_store)
        service.graph_provider.provide = lambda entity, **kwargs: GraphProviderResult(  # type: ignore[method-assign]
            provider="graph",
            status="available",
            target_entity=entity,
            context={
                "target_ip": "192.168.30.100",
                "node_found": True,
                "scope": kwargs["route"].scope,
                "direction": "both",
                "depth": kwargs["route"].depth,
                "relationship_mode": kwargs["route"].relationship_mode,
                "source_present": True,
                "target_present": True,
                "forward_edge": True,
                "reverse_edge": False,
            },
            provenance=ProviderProvenance(source="observed_communication_graph", status="available"),
        )
        service.chat("Are 192.168.30.100 and 192.168.30.101 directly connected?", "pair", request_id="r-pair-1")
        state = state_store.get("pair")
        self.assertEqual(state.active_entities, ("192.168.30.100", "192.168.30.101"))
        self.assertIsNone(state.active_ip)
        service.chat("Now find the shortest path between them.", "pair", request_id="r-pair-2")
        self.assertEqual(state_store.get("pair").active_entities, ("192.168.30.100", "192.168.30.101"))

    def test_single_entity_replaces_pair_state(self) -> None:
        settings = make_settings()
        router_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"asset"}'
        llm = FakeLLMClient([fake_result(router_json), fake_result("answer")])
        state_store = SessionRoutingStateStore()
        state_store.set("s", SessionRoutingState(active_entities=("192.168.30.100", "192.168.30.101")))
        service = CopilotService(settings, llm, MemoryStore(20), state_store)
        service.graph_provider.provide = lambda *args, **kwargs: GraphProviderResult(  # type: ignore[method-assign]
            provider="graph",
            status="available",
            context={"target_ip": "192.168.30.115", "node_found": True, "scope": "node_summary", "direction": "both", "depth": 0},
            provenance=ProviderProvenance(source="observed_communication_graph", status="available"),
        )
        service.chat("Tell me about 192.168.30.115", "s", request_id="r-single")
        state = state_store.get("s")
        self.assertEqual(state.active_ip, "192.168.30.115")
        self.assertEqual(state.active_entities, ())

    def test_general_and_unclear_turns_preserve_active_pair_state(self) -> None:
        settings = make_settings()
        general_json = '{"intent":"general_knowledge","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"general"}'
        unclear_json = '{"intent":"unclear","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"unclear"}'
        llm = FakeLLMClient([fake_result(general_json), fake_result("general answer"), fake_result(unclear_json), fake_result("unclear answer")])
        state_store = SessionRoutingStateStore()
        pair = ("192.168.30.100", "192.168.30.101")
        state_store.set("s", SessionRoutingState(active_entities=pair, previous_entity_count=2, previous_entity_mode="multiple"))
        service = CopilotService(settings, llm, MemoryStore(20), state_store)

        service.chat("Explain phishing for 192.168.30.250 in general.", "s", request_id="r-general")
        state = state_store.get("s")
        self.assertEqual(state.active_entities, pair)
        self.assertIsNone(state.active_ip)
        self.assertEqual(state.previous_entity_count, 2)

        service.chat("Maybe compare 192.168.30.200 and 192.168.30.201 somehow?", "s", request_id="r-unclear")
        state = state_store.get("s")
        self.assertEqual(state.active_entities, pair)
        self.assertIsNone(state.active_ip)
        self.assertEqual(state.previous_entity_count, 2)

    def test_general_and_unclear_turns_preserve_active_single_state_and_previous_route(self) -> None:
        settings = make_settings()
        general_json = '{"intent":"general_knowledge","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"general"}'
        unclear_json = '{"intent":"unclear","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"unclear"}'
        llm = FakeLLMClient([fake_result(general_json), fake_result("general answer"), fake_result(unclear_json), fake_result("unclear answer")])
        state_store = SessionRoutingStateStore()
        state_store.set(
            "single",
            SessionRoutingState(
                active_ip="192.168.21.1",
                previous_entity_count=1,
                previous_entity_mode="single",
                last_provider="graph",
                previous_intent="asset_investigation",
                previous_scope="node_summary",
                previous_direction="both",
                previous_depth=0,
            ),
        )
        service = CopilotService(settings, llm, MemoryStore(20), state_store)

        service.chat("Explain phishing in general.", "single", request_id="r-single-general")
        state = state_store.get("single")
        self.assertEqual(state.active_ip, "192.168.21.1")
        self.assertEqual(state.last_provider, "graph")
        self.assertEqual(state.previous_intent, "asset_investigation")
        self.assertEqual(state.previous_scope, "node_summary")

        service.chat("Maybe something unclear?", "single", request_id="r-single-unclear")
        state = state_store.get("single")
        self.assertEqual(state.active_ip, "192.168.21.1")
        self.assertEqual(state.last_provider, "graph")
        self.assertEqual(state.previous_intent, "asset_investigation")
        self.assertEqual(state.previous_scope, "node_summary")

    def test_explicit_new_ip_replaces_previous_active_single_ip(self) -> None:
        settings = make_settings()
        router_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"asset"}'
        llm = FakeLLMClient([fake_result(router_json), fake_result("answer")])
        state_store = SessionRoutingStateStore()
        state_store.set("single", SessionRoutingState(active_ip="192.168.21.1"))
        service = CopilotService(settings, llm, MemoryStore(20), state_store)
        service.graph_provider.provide = lambda entity, **kwargs: GraphProviderResult(  # type: ignore[method-assign]
            provider="graph",
            status="available",
            target_entity=entity,
            context={"target_ip": entity.value, "node_found": True, "scope": "node_summary", "direction": "both", "depth": 0},
            provenance=ProviderProvenance(source="observed_communication_graph", status="available"),
        )
        service.chat("Tell me about 192.168.21.2.", "single", request_id="r-new-single")
        state = state_store.get("single")
        self.assertEqual(state.active_ip, "192.168.21.2")
        self.assertEqual(state.active_entities, ())

    def test_explicit_broad_comparison_executes_comparison_route_and_stores_pair(self) -> None:
        settings = make_settings()
        comparison_json = '{"intent":"graph_relationships","scope":"multi_entity_comparison","direction":"both","depth":1,"requires_graph":true,"requires_multiple_entities":true,"is_followup":false,"classification_confidence":0.9,"reason":"compare"}'
        llm = FakeLLMClient([fake_result(comparison_json), fake_result("comparison answer")])
        state_store = SessionRoutingStateStore()
        service = CopilotService(settings, llm, MemoryStore(20), state_store)
        captured_routes = []

        def fake_graph_provider(entity, **kwargs):
            captured_routes.append(kwargs["route"])
            return GraphProviderResult(
                provider="graph",
                status="available",
                target_entity=entity,
                context={
                    "target_ips": ["192.168.30.100", "192.168.30.101"],
                    "target_ip": "192.168.30.100",
                    "node_found": True,
                    "scope": "multi_entity_comparison",
                    "direction": "both",
                    "depth": 1,
                    "relationship_mode": "compare",
                    "entities": ["192.168.30.100", "192.168.30.101"],
                    "entity_a": {"ip": "192.168.30.100", "present": True, "inbound_total": 1, "outbound_total": 3, "bidirectional_total": 0},
                    "entity_b": {"ip": "192.168.30.101", "present": True, "inbound_total": 2, "outbound_total": 1, "bidirectional_total": 0},
                    "direct_relationship": {"a_to_b": False, "b_to_a": False, "relationship_status": "no_direct_relationship"},
                    "degree_comparison": {"entity_a_total_peer_count": 4, "entity_b_total_peer_count": 3, "broader_outbound_entity": "192.168.30.100"},
                    "subnet_comparison": {"shared_subnets": [], "entity_a_unique_subnets": [], "entity_b_unique_subnets": []},
                    "shared_peer_total": 0,
                    "shared_peers_retrieved": [],
                    "shared_peers_retrieved_count": 0,
                    "entity_a_unique_peer_total": 4,
                    "entity_b_unique_peer_total": 3,
                    "nodes": [],
                    "edges": [],
                },
                provenance=ProviderProvenance(source="observed_communication_graph", status="available"),
            )

        service.graph_provider.provide = fake_graph_provider  # type: ignore[method-assign]
        service.chat("Compare 192.168.30.100 and 192.168.30.101.", "compare", request_id="r-compare")

        self.assertEqual(captured_routes[0].scope, "multi_entity_comparison")
        self.assertEqual(captured_routes[0].relationship_mode, "compare")
        self.assertEqual([entity.value for entity in captured_routes[0].target_entities], ["192.168.30.100", "192.168.30.101"])
        self.assertEqual(state_store.get("compare").active_entities, ("192.168.30.100", "192.168.30.101"))

    def test_compact_memory_removes_old_full_assistant_messages_from_model_input(self) -> None:
        settings = make_settings(
            conversation_summary_trigger_tokens=20,
            conversation_recent_raw_messages=2,
            conversation_summary_max_tokens=80,
        )
        router_json = '{"intent":"general_knowledge","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"general"}'
        long_old_answer = "OLD_ASSISTANT_REPORT " * 80
        memory = MemoryStore(50)
        memory.append("m", "user", "older question")
        memory.append("m", "assistant", long_old_answer)
        memory.append("m", "user", "recent question")
        memory.append("m", "assistant", "recent short answer")
        llm = FakeLLMClient([fake_result(router_json), fake_result("new answer")])
        service = CopilotService(settings, llm, memory, SessionRoutingStateStore())
        service.chat("What is phishing?", "m", request_id="r-memory")
        answer_call_messages = llm.calls[1]["messages"]
        serialized = "\n".join(message["content"] for message in answer_call_messages)
        self.assertIn("[SOORIN CONVERSATION SUMMARY]", serialized)
        self.assertNotIn(long_old_answer, serialized)

    def test_compaction_preserves_pair_and_them_followup_uses_pair(self) -> None:
        settings = make_settings(
            conversation_summary_trigger_tokens=20,
            conversation_recent_raw_messages=2,
            conversation_summary_max_tokens=80,
        )
        comparison_json = '{"intent":"graph_relationships","scope":"multi_entity_comparison","direction":"both","depth":1,"requires_graph":true,"requires_multiple_entities":true,"is_followup":true,"classification_confidence":0.9,"reason":"compare"}'
        long_old_answer = "OLD_PAIR_ASSISTANT_REPORT " * 80
        memory = MemoryStore(50)
        memory.append("pair", "user", "older pair question")
        memory.append("pair", "assistant", long_old_answer)
        memory.append("pair", "user", "recent pair question")
        memory.append("pair", "assistant", "recent pair answer")
        llm = FakeLLMClient([fake_result(comparison_json), fake_result("comparison answer")])
        state_store = SessionRoutingStateStore()
        pair = ("192.168.30.100", "192.168.30.101")
        state_store.set("pair", SessionRoutingState(active_entities=pair, previous_intent="graph_relationships", previous_scope="one_hop", last_provider="graph"))
        service = CopilotService(settings, llm, memory, state_store)
        captured_routes = []

        def fake_graph_provider(entity, **kwargs):
            captured_routes.append(kwargs["route"])
            return GraphProviderResult(
                provider="graph",
                status="available",
                target_entity=entity,
                context={
                    "target_ips": list(pair),
                    "target_ip": pair[0],
                    "node_found": True,
                    "scope": "multi_entity_comparison",
                    "direction": "both",
                    "depth": 1,
                    "relationship_mode": "compare",
                    "entities": list(pair),
                    "entity_a": {"ip": pair[0], "present": True, "inbound_total": 1, "outbound_total": 2, "bidirectional_total": 0},
                    "entity_b": {"ip": pair[1], "present": True, "inbound_total": 2, "outbound_total": 1, "bidirectional_total": 0},
                    "direct_relationship": {"a_to_b": False, "b_to_a": False, "relationship_status": "no_direct_relationship"},
                    "degree_comparison": {"entity_a_total_peer_count": 3, "entity_b_total_peer_count": 3, "broader_outbound_entity": pair[0]},
                    "subnet_comparison": {"shared_subnets": [], "entity_a_unique_subnets": [], "entity_b_unique_subnets": []},
                    "shared_peer_total": 0,
                    "shared_peers_retrieved": [],
                    "shared_peers_retrieved_count": 0,
                    "entity_a_unique_peer_total": 3,
                    "entity_b_unique_peer_total": 3,
                    "nodes": [],
                    "edges": [],
                },
                provenance=ProviderProvenance(source="observed_communication_graph", status="available"),
            )

        service.graph_provider.provide = fake_graph_provider  # type: ignore[method-assign]
        service.chat("Compare them.", "pair", request_id="r-them")

        answer_call_messages = llm.calls[1]["messages"]
        serialized = "\n".join(message["content"] for message in answer_call_messages)
        self.assertIn("[SOORIN CONVERSATION SUMMARY]", serialized)
        self.assertNotIn(long_old_answer, serialized)
        self.assertEqual([entity.value for entity in captured_routes[0].target_entities], list(pair))
        self.assertEqual(state_store.get("pair").active_entities, pair)

    def test_memory_summary_not_regenerated_below_trigger_after_compaction(self) -> None:
        settings = make_settings(conversation_summary_trigger_tokens=20, conversation_recent_raw_messages=2)
        memory = MemoryStore(20)
        memory.append("s", "user", "old " * 100)
        memory.append("s", "assistant", "answer " * 100)
        first = memory.prepare_for_model("s", settings, SessionRoutingState(active_entities=("a", "b")))
        second = memory.prepare_for_model("s", settings, SessionRoutingState(active_entities=("a", "b")))
        self.assertTrue(first.summary_updated)
        self.assertFalse(second.summary_updated)
        self.assertTrue(second.summary_present)

    def test_trace_renders_router_and_retrieval_sections(self) -> None:
        trace = CopilotRequestTrace("r", "s", "m")
        trace.put("ROUTER INPUT", resolved_entity_count=1)
        trace.put("GRAPH RETRIEVAL", inbound_total=2, inbound_returned=1)
        with self.assertLogs("src.core.copilot.trace", level="INFO") as logs:
            render_human_copilot_trace(trace)
        text = "\n".join(logs.output)
        self.assertIn("== ROUTER INPUT ==", text)
        self.assertIn("== GRAPH RETRIEVAL ==", text)


if __name__ == "__main__":
    unittest.main()
