"""Focused tests for LLM-primary graph routing and bounded retrieval."""

from __future__ import annotations

import pickle
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import networkx as nx

from src.config.settings import get_settings
from src.core.agent.contracts import EvidenceFact, ToolResult
from src.core.agent.task_mapping import task_spec_from_route
from src.core.context.composer import ContextComposer
from src.core.context.entities import EntityResolver
from src.core.context.intent import (
    SemanticIntentRouter as GLMIntentRouter,
    ROUTER_SYSTEM_PROMPT_FALLBACK,
    _extract_first_json_object,
    _json_from_text,
    validate_router_payload,
)
from src.core.context.models import (
    AssetProfileProviderResult,
    CopilotContextPackage,
    DetectionProviderResult,
    GraphProviderResult,
    ProviderProvenance,
    ResolvedEntity,
)
from src.core.context.providers.graph import GraphContextProvider
from src.core.context.router import DeterministicFallbackRouter, normalize_intent_route
from src.core.copilot.service import CopilotService
from src.core.copilot.trace import CopilotRequestTrace, render_human_copilot_trace
from src.core.graph.loader import get_cached_graph, load_graph, replace_active_graph, set_graph_path
from src.core.graph.refresh import GraphRefreshService
from src.core.graph.retrieval import GraphRetrievalSpec, retrieve_graph_context
from src.core.graph.service import get_subnet
from src.core.graph.visualization import _filter_graph_by_subnet, _inject_node_click_bridge, generate_pyvis_graph
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent
from src.core.memory.routing_state import SessionRoutingState, SessionRoutingStateStore
from src.core.memory.episodes import MemoryContextKey
from src.core.memory.store import MemoryStore
from src.core.product_client.schemas import ProductTopologyResponse, TopologyConnectionRecord
from src.web.copilot_help import choose_help_ui_pattern, get_copilot_help_content
from src.web.pages.topology import build_copilot_ui_context, _resolve_graph_selection_event


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
        self.calls = 0

    def fetch_topology_unique_ip_pairs(self) -> ProductTopologyResponse:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def fake_result(
    text: str,
    *,
    finish_reason: str | None = "stop",
    usage: dict[str, int] | None = None,
) -> LLMProviderResult:
    return LLMProviderResult(
        text=text,
        provider="fake",
        model="fake",
        finish_reason=finish_reason,
        usage=usage or {},
        status_code=200,
    )


def make_settings(**overrides):
    values = {
        "llm_provider": "fake",
        "planner_enabled": False,
        "router_model": "fake",
        "synthesizer_model": "fake",
        "planner_model": "fake",
        "copilot_human_trace_enabled": False,
        "intent_router_enabled": True,
        "intent_router_min_confidence": 0.65,
        "intent_router_retry_enabled": True,
        "product_api_base_url": "",
        "product_api_token": "",
        "product_hwid": "",
    }
    values.update(overrides)
    return replace(
        get_settings(),
        **values,
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

    def test_comparison_followup_materializes_explicit_then_active_entity(self) -> None:
        state = SessionRoutingState(active_ip="192.168.21.142")
        prompts = (
            "How is 192.168.0.125 different from it?",
            "How is 192.168.0.125 different from previous asset?",
        )

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                result = self.resolver.resolve(prompt, routing_state=state)
                self.assertEqual(
                    [entity.value for entity in result.entities],
                    ["192.168.0.125", "192.168.21.142"],
                )
                self.assertEqual(
                    [entity.source for entity in result.entities],
                    ["message", "conversation"],
                )
                self.assertEqual(result.reference_type, "compare_with_reference")

    def test_cidr_only_never_materializes_network_address_as_host(self) -> None:
        for cidr in ("192.168.21.0/24", "10.0.0.0/8", "172.16.0.0/16", "2001:db8::/32"):
            with self.subTest(cidr=cidr):
                result = self.resolver.resolve(f"Identify peers in {cidr}.")
                self.assertEqual(result.entities, [])
                self.assertIsNone(result.primary_entity)
                self.assertIn(cidr, result.subnet_constraints)
                self.assertIn(cidr, result.unsupported_constraints)

    def test_explicit_host_and_cidr_are_kept_separate(self) -> None:
        result = self.resolver.resolve(
            "For asset 192.168.0.55, identify bidirectional peers inside 192.168.21.7/24."
        )
        self.assertEqual([entity.value for entity in result.entities], ["192.168.0.55"])
        self.assertEqual(result.subnet_constraints, ("192.168.21.0/24",))
        self.assertNotIn("192.168.21.0", [entity.value for entity in result.entities])

    def test_bounded_active_single_reference_phrases_resolve_consistently(self) -> None:
        state = SessionRoutingState(active_ip="192.168.21.104")
        phrases = [
            "Show the connections of it.",
            "Analyze evidence from it.",
            "Use all of its evidence.",
            "What do we know about it?",
            "Assess behavior observed from it.",
            "Show connections we have from it.",
            "Analyze this host.",
            "Continue with the same asset.",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                result = self.resolver.resolve(phrase, routing_state=state)
                self.assertEqual(result.status, "resolved")
                self.assertEqual(result.primary_entity.value, "192.168.21.104")
                self.assertEqual(result.primary_entity.source, "conversation")
                self.assertTrue(result.reference_detected)

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
        self.assertEqual([entity.value for entity in result.entities], ["192.168.30.144", "192.168.30.115"])

    def test_comparison_reference_can_use_recent_turn_entity(self) -> None:
        result = self.resolver.resolve(
            "How does 192.168.0.125 diverge from the asset we just analyzed?",
            recent_messages=[
                {"role": "user", "content": "Tell me about 192.168.21.142."},
                {"role": "assistant", "content": "192.168.21.142 was analyzed."},
            ],
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.entity_mode, "multiple")
        self.assertEqual(
            [entity.value for entity in result.entities],
            ["192.168.0.125", "192.168.21.142"],
        )
        self.assertEqual(
            [entity.source for entity in result.entities],
            ["message", "conversation"],
        )

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
            "requires_detection": False,
            "requires_asset_profile": False,
            "entity_binding": "explicit",
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

    def test_explicit_comparison_binding_keeps_conversation_second_entity(self) -> None:
        message = "How is 192.168.0.125 different from previous asset?"
        state = SessionRoutingState(active_ip="192.168.21.142")
        entities = EntityResolver().resolve(message, routing_state=state)
        decision = validate_router_payload(
            self.payload(
                intent="graph_relationships",
                scope="multi_entity_comparison",
                direction="both",
                depth=1,
                requires_multiple_entities=True,
                entity_binding="explicit",
                is_followup=True,
            ),
            entities,
            min_confidence=0.65,
            message=message,
            routing_state=state,
        )

        self.assertEqual(
            decision.materialized_entities,
            ("192.168.0.125", "192.168.21.142"),
        )
        self.assertEqual(decision.scope, "multi_entity_comparison")
        self.assertTrue(decision.requires_multiple_entities)

    def test_comparison_task_spec_rejects_one_entity(self) -> None:
        route = SimpleNamespace(
            materialized_entities=("192.168.0.125",),
            scope="multi_entity_comparison",
        )

        with self.assertRaisesRegex(ValueError, "comparison_requires_two_distinct_entities"):
            task_spec_from_route(route, "Compare this asset with the previous asset.")

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
        self.assertFalse(route.use_detection)
        self.assertEqual(route.decision_source, "semantic_router")

    def test_asset_investigation_honors_selected_providers(self) -> None:
        decision = validate_router_payload(
            self.payload(intent="asset_investigation", scope="node_summary", direction="both", depth=0),
            self.entities,
            min_confidence=0.65,
            message="Tell me about 192.168.30.115.",
        )
        route = normalize_intent_route(decision, self.entities)
        self.assertTrue(route.use_graph)
        self.assertFalse(route.use_detection)
        self.assertFalse(route.use_asset_profile)

    def test_classification_explanation_can_request_detection_only(self) -> None:
        decision = validate_router_payload(
            self.payload(intent="asset_investigation", scope="none", direction="none", depth=0, requires_graph=False, requires_detection=True),
            self.entities,
            min_confidence=0.65,
            message="Why is 192.168.30.115 classified as a Windows workstation?",
        )
        route = normalize_intent_route(decision, self.entities)
        self.assertFalse(route.use_graph)
        self.assertTrue(route.use_detection)
        self.assertEqual(route.scope, "none")

    def test_graph_route_preserves_explicit_provider_selection(self) -> None:
        decision = validate_router_payload(
            self.payload(intent="graph_neighbors", scope="one_hop", direction="outbound", depth=1, requires_detection=True),
            self.entities,
            min_confidence=0.65,
            message="Find its outbound peers.",
        )
        route = normalize_intent_route(decision, self.entities)
        self.assertTrue(route.use_graph)
        self.assertTrue(route.use_detection)

    def test_combined_role_topology_requests_both(self) -> None:
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="node_summary",
                direction="both",
                depth=0,
                requires_graph=True,
                requires_detection=True,
            ),
            self.entities,
            min_confidence=0.65,
            message="Does its detected role agree with its topology?",
        )
        route = normalize_intent_route(decision, self.entities)
        self.assertTrue(route.use_graph)
        self.assertTrue(route.use_detection)

    def test_combined_asset_scopes_preserve_graph_and_detection(self) -> None:
        cases = [("node_summary", 0), ("full_neighbors", 1), ("two_hop", 2)]
        for scope, depth in cases:
            with self.subTest(scope=scope):
                decision = validate_router_payload(
                    self.payload(
                        intent="asset_investigation",
                        scope=scope,
                        direction="both",
                        depth=depth,
                        requires_graph=True,
                        requires_detection=True,
                        requires_asset_profile=True,
                    ),
                    self.entities,
                    min_confidence=0.65,
                    message="Analyze this asset using all evidence.",
                )
                route = normalize_intent_route(decision, self.entities)
                self.assertEqual(route.scope, scope)
                self.assertEqual(route.depth, depth)
                self.assertEqual(route.direction, "both")
                self.assertTrue(route.use_graph)
                self.assertTrue(route.use_detection)
                self.assertTrue(route.use_asset_profile)

    def test_obsolete_detection_mode_field_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected"):
            validate_router_payload(
                {**self.payload(requires_detection=True), "detection_detail": "summary"},
                self.entities,
                min_confidence=0.65,
            )

    def test_detection_selection_is_preserved_with_graph(self) -> None:
        cases = [("full_neighbors", 1), ("two_hop", 2)]
        for scope, depth in cases:
            with self.subTest(scope=scope):
                decision = validate_router_payload(
                    self.payload(
                        intent="asset_investigation",
                        scope=scope,
                        direction="both",
                        depth=depth,
                        requires_graph=True,
                        requires_detection=True,
                    ),
                    self.entities,
                    min_confidence=0.65,
                    message="Include every matched detection rule, all supporting signals, and all conflicts.",
                )
                route = normalize_intent_route(decision, self.entities)
                self.assertTrue(route.use_graph)
                self.assertTrue(route.use_detection)
                self.assertEqual(route.scope, scope)
                self.assertEqual(route.depth, depth)

    def test_anomaly_route_uses_current_graph_and_detection_evidence(self) -> None:
        decision = validate_router_payload(
            self.payload(
                intent="graph_neighbors",
                scope="one_hop",
                direction="both",
                depth=1,
                requires_graph=True,
                requires_detection=False,
            ),
            self.entities,
            min_confidence=0.65,
            message="Does this asset show unusual behavior?",
        )
        route = normalize_intent_route(decision, self.entities)
        self.assertEqual(route.intent, "graph_neighbors")
        self.assertEqual(route.scope, "one_hop")
        self.assertEqual(route.depth, 1)
        self.assertTrue(route.use_graph)
        self.assertFalse(route.use_detection)

    def test_deep_two_hop_route_preserves_all_selected_providers(self) -> None:
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="two_hop",
                direction="both",
                depth=2,
                requires_graph=True,
                requires_detection=True,
                requires_asset_profile=True,
            ),
            self.entities,
            min_confidence=0.65,
            message="Perform a deep anomaly assessment using all detection rules, conflicts, and two-hop communication patterns.",
        )
        self.assertEqual(decision.scope, "two_hop")
        self.assertEqual(decision.depth, 2)
        self.assertTrue(decision.requires_graph)
        self.assertTrue(decision.requires_detection)
        self.assertTrue(decision.requires_asset_profile)

    def test_general_knowledge_requests_no_providers(self) -> None:
        decision = validate_router_payload(
            self.payload(
                intent="general_knowledge",
                scope="none",
                direction="none",
                depth=0,
                requires_graph=False,
                requires_detection=True,
            ),
            self.entities,
            min_confidence=0.65,
            message="What is a domain-joined workstation?",
        )
        route = normalize_intent_route(decision, self.entities)
        self.assertFalse(route.use_graph)
        self.assertFalse(route.use_detection)

    def test_multiple_entities_can_use_detection(self) -> None:
        decision = validate_router_payload(
            self.payload(
                intent="graph_path",
                scope="path",
                direction="both",
                depth=0,
                requires_graph=True,
                requires_detection=True,
                requires_multiple_entities=True,
            ),
            self.two_entities,
            min_confidence=0.65,
            message="Find path from 192.168.30.115 to 192.168.0.149",
        )
        route = normalize_intent_route(decision, self.two_entities)
        self.assertTrue(route.use_graph)
        self.assertTrue(route.use_detection)

    def test_active_single_binding_allows_detection_followup_without_pre_resolved_entity(self) -> None:
        no_entities = EntityResolver().resolve("Show me more detection evidence.")
        self.assertEqual(no_entities.status, "none")
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="none",
                direction="none",
                depth=0,
                requires_graph=False,
                requires_detection=True,
                entity_binding="active_single",
                is_followup=True,
            ),
            no_entities,
            min_confidence=0.65,
            message="Show me more detection evidence.",
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        route = normalize_intent_route(decision, EntityResolver().resolve("unused", routing_state=SessionRoutingState(active_ip="192.168.30.111")))
        self.assertEqual(decision.entity_binding, "active_single")
        self.assertEqual(decision.materialized_entities, ("192.168.30.111",))
        self.assertTrue(decision.requires_detection)
        self.assertEqual(route.decision_source, "semantic_router")

    def test_active_reference_normalizes_is_followup_true(self) -> None:
        state = SessionRoutingState(active_ip="192.168.30.115")
        entities = EntityResolver().resolve("Use all of its evidence.", routing_state=state)
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="node_summary",
                direction="both",
                depth=0,
                requires_graph=True,
                requires_detection=True,
                entity_binding="active_single",
                is_followup=False,
            ),
            entities,
            min_confidence=0.65,
            message="Use all of its evidence.",
            routing_state=state,
        )
        self.assertTrue(decision.is_followup)
        self.assertTrue(decision.route_normalized)

    def test_general_knowledge_binds_none(self) -> None:
        decision = validate_router_payload(
            self.payload(
                intent="general_knowledge",
                scope="none",
                direction="none",
                depth=0,
                requires_graph=False,
                requires_detection=False,
                entity_binding="active_single",
            ),
            EntityResolver().resolve("What is Kerberos?"),
            min_confidence=0.65,
            message="What is Kerberos?",
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        self.assertEqual(decision.entity_binding, "none")
        self.assertEqual(decision.materialized_entities, ())
        self.assertTrue(decision.binding_normalized)
        self.assertEqual(decision.binding_normalization_reason, "general_requires_no_entity_binding")

    def test_topic_detachment_forbids_active_binding(self) -> None:
        detached = EntityResolver().resolve(
            "Not about this asset; explain Kerberos.",
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        decision = validate_router_payload(
            self.payload(
                intent="general_knowledge",
                scope="none",
                direction="none",
                depth=0,
                requires_graph=False,
                requires_detection=False,
                entity_binding="active_single",
            ),
            detached,
            min_confidence=0.65,
            message="Not about this asset; explain Kerberos.",
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        self.assertEqual(decision.entity_binding, "none")

    def test_explicit_entity_overrides_active_binding(self) -> None:
        explicit = EntityResolver().resolve(
            "Show detection evidence for 192.168.30.112.",
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="none",
                direction="none",
                depth=0,
                requires_graph=False,
                requires_detection=True,
                entity_binding="active_single",
            ),
            explicit,
            min_confidence=0.65,
            message="Show detection evidence for 192.168.30.112.",
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        self.assertEqual(decision.entity_binding, "explicit")
        self.assertEqual(decision.materialized_entities, ("192.168.30.112",))
        self.assertEqual(decision.binding_normalization_reason, "explicit_entity_takes_authority")

    def test_ui_binding_overrides_active_when_no_explicit_entity(self) -> None:
        ui_entities = EntityResolver().resolve(
            "Tell me about this asset.",
            ui_context={"selected_ip": "192.168.30.113"},
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="node_summary",
                direction="both",
                depth=0,
                requires_graph=True,
                requires_detection=True,
                entity_binding="ui",
            ),
            ui_entities,
            min_confidence=0.65,
            message="Tell me about this asset.",
            ui_context={"selected_ip": "192.168.30.113"},
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        self.assertEqual(decision.entity_binding, "ui")
        self.assertEqual(decision.materialized_entities, ("192.168.30.113",))

    def test_ui_selected_ip_overrides_active_single_even_when_glm_binds_none_general(self) -> None:
        ui_entities = EntityResolver().resolve(
            "What is selected?",
            ui_context={"selected_ip": "192.168.30.113"},
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        decision = validate_router_payload(
            self.payload(
                intent="general_knowledge",
                scope="none",
                direction="none",
                depth=0,
                requires_graph=False,
                requires_detection=False,
                entity_binding="none",
            ),
            ui_entities,
            min_confidence=0.65,
            message="What is selected?",
            ui_context={"selected_ip": "192.168.30.113"},
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        self.assertEqual(decision.intent, "general_knowledge")
        self.assertEqual(decision.entity_binding, "ui")
        self.assertEqual(decision.materialized_entities, ("192.168.30.113",))
        self.assertEqual(decision.binding_normalization_reason, "ui_entity_takes_authority")
        self.assertFalse(decision.requires_graph)
        self.assertFalse(decision.requires_detection)

    def test_explicit_ip_overrides_ui_and_active_state(self) -> None:
        explicit = EntityResolver().resolve(
            "Show detection evidence for 192.168.30.112.",
            ui_context={"selected_ip": "192.168.30.113"},
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="none",
                direction="none",
                depth=0,
                requires_graph=False,
                requires_detection=True,
                entity_binding="ui",
            ),
            explicit,
            min_confidence=0.65,
            message="Show detection evidence for 192.168.30.112.",
            ui_context={"selected_ip": "192.168.30.113"},
            routing_state=SessionRoutingState(active_ip="192.168.30.111"),
        )
        self.assertEqual(decision.entity_binding, "explicit")
        self.assertEqual(decision.materialized_entities, ("192.168.30.112",))
        self.assertEqual(decision.binding_normalization_reason, "explicit_entity_takes_authority")

    def test_requested_explicit_single_ip_wins_over_conflicting_ui_ip(self) -> None:
        explicit = EntityResolver().resolve(
            "Tell me about 192.168.3.137.",
            ui_context={"selected_ip": "192.168.3.103"},
        )
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="node_summary",
                direction="both",
                depth=0,
                requires_graph=True,
                requires_detection=True,
                entity_binding="explicit",
            ),
            explicit,
            min_confidence=0.65,
            message="Tell me about 192.168.3.137.",
            ui_context={"selected_ip": "192.168.3.103"},
        )

        self.assertEqual(decision.requested_entity_binding, "explicit")
        self.assertEqual(decision.entity_binding, "explicit")
        self.assertEqual(decision.binding_source, "message")
        self.assertEqual(decision.materialized_entities, ("192.168.3.137",))

    def test_requested_explicit_pair_wins_over_conflicting_ui_ip(self) -> None:
        explicit_pair = EntityResolver().resolve(
            "Find path between 192.168.3.137 and 192.168.3.138.",
            ui_context={"selected_ip": "192.168.3.103"},
        )
        decision = validate_router_payload(
            self.payload(
                intent="graph_path",
                scope="path",
                direction="both",
                depth=0,
                requires_graph=True,
                requires_detection=False,
                entity_binding="explicit",
                requires_multiple_entities=True,
            ),
            explicit_pair,
            min_confidence=0.65,
            message="Find path between 192.168.3.137 and 192.168.3.138.",
            ui_context={"selected_ip": "192.168.3.103"},
        )

        self.assertEqual(decision.requested_entity_binding, "explicit")
        self.assertEqual(decision.entity_binding, "explicit")
        self.assertEqual(decision.binding_source, "message")
        self.assertEqual(decision.materialized_entities, ("192.168.3.137", "192.168.3.138"))
        self.assertTrue(decision.requires_multiple_entities)

    def test_no_explicit_ip_ui_reference_still_uses_ui_entity(self) -> None:
        ui_entities = EntityResolver().resolve(
            "What is this?",
            ui_context={"selected_ip": "192.168.3.103"},
            routing_state=SessionRoutingState(active_ip="192.168.3.99"),
        )
        decision = validate_router_payload(
            self.payload(
                intent="general_knowledge",
                scope="none",
                direction="none",
                depth=0,
                requires_graph=False,
                requires_detection=False,
                entity_binding="none",
            ),
            ui_entities,
            min_confidence=0.65,
            message="What is this?",
            ui_context={"selected_ip": "192.168.3.103"},
            routing_state=SessionRoutingState(active_ip="192.168.3.99"),
        )

        self.assertEqual(decision.entity_binding, "ui")
        self.assertEqual(decision.binding_source, "ui")
        self.assertEqual(decision.materialized_entities, ("192.168.3.103",))

    def test_explicit_ip_wins_over_active_previous_ip(self) -> None:
        explicit = EntityResolver().resolve(
            "Tell me about 192.168.3.137.",
            routing_state=SessionRoutingState(active_ip="192.168.3.99"),
        )
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="node_summary",
                direction="both",
                depth=0,
                requires_graph=True,
                requires_detection=True,
                entity_binding="active_single",
            ),
            explicit,
            min_confidence=0.65,
            message="Tell me about 192.168.3.137.",
            routing_state=SessionRoutingState(active_ip="192.168.3.99"),
        )

        self.assertEqual(decision.entity_binding, "explicit")
        self.assertEqual(decision.binding_source, "message")
        self.assertEqual(decision.materialized_entities, ("192.168.3.137",))
        self.assertEqual(decision.binding_normalization_reason, "explicit_entity_takes_authority")

    def test_missing_binding_infers_active_pair_for_pair_route(self) -> None:
        no_entities = EntityResolver().resolve("Find their path.")
        decision = validate_router_payload(
            self.payload(
                intent="graph_path",
                scope="path",
                direction="both",
                depth=0,
                requires_graph=True,
                requires_detection=False,
                entity_binding="",
                requires_multiple_entities=True,
            ),
            no_entities,
            min_confidence=0.65,
            message="Find their path.",
            routing_state=SessionRoutingState(
                active_ip="192.168.30.111",
                active_entities=("192.168.30.112", "192.168.30.113"),
            ),
        )
        self.assertEqual(decision.entity_binding, "active_pair")
        self.assertEqual(decision.materialized_entities, ("192.168.30.112", "192.168.30.113"))

    def test_missing_binding_infers_active_single_for_single_route(self) -> None:
        no_entities = EntityResolver().resolve("Show more detail.")
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="node_summary",
                direction="both",
                depth=0,
                requires_graph=True,
                requires_detection=True,
                entity_binding="",
            ),
            no_entities,
            min_confidence=0.65,
            message="Show more detail.",
            routing_state=SessionRoutingState(
                active_ip="192.168.30.111",
                active_entities=("192.168.30.112", "192.168.30.113"),
            ),
        )
        self.assertEqual(decision.entity_binding, "active_single")
        self.assertEqual(decision.materialized_entities, ("192.168.30.111",))

    def test_missing_active_single_binding_raises_entity_requirement_failed(self) -> None:
        with self.assertRaisesRegex(ValueError, "entity_requirement_failed"):
            validate_router_payload(
                self.payload(
                    intent="asset_investigation",
                    scope="none",
                    direction="none",
                    depth=0,
                    requires_graph=False,
                    requires_detection=True,
                    entity_binding="active_single",
                ),
                EntityResolver().resolve("Show detection evidence."),
                min_confidence=0.65,
                message="Show detection evidence.",
                routing_state=SessionRoutingState(),
            )

    def test_invalid_entity_binding_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported_enum:entity_binding"):
            validate_router_payload(
                self.payload(entity_binding="memory_magic"),
                self.entities,
                min_confidence=0.65,
            )

    def test_unknown_router_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected"):
            validate_router_payload(
                {**self.payload(), "legacy_mode": "verbose"},
                self.entities,
                min_confidence=0.65,
            )

    def test_followup_show_more_evidence_uses_active_ip(self) -> None:
        entities = EntityResolver().resolve(
            "show more evidence",
            routing_state=SessionRoutingState(active_ip="192.168.30.115"),
        )
        self.assertEqual(entities.status, "none")
        decision = validate_router_payload(
            self.payload(
                intent="asset_investigation",
                scope="none",
                direction="none",
                depth=0,
                requires_graph=False,
                requires_detection=True,
                entity_binding="active_single",
                is_followup=True,
            ),
            entities,
            min_confidence=0.65,
            message="show more evidence",
            routing_state=SessionRoutingState(active_ip="192.168.30.115", last_provider="detection"),
        )
        self.assertEqual(decision.entity_binding, "active_single")
        self.assertEqual(decision.binding_source, "conversation")
        self.assertEqual(decision.materialized_entities, ("192.168.30.115",))
        self.assertTrue(decision.requires_detection)

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


class RouterJSONExtractionTests(unittest.TestCase):
    def test_extracts_pure_fenced_and_surrounded_json(self) -> None:
        expected = {"intent": "general_knowledge", "reason": "ok"}
        cases = [
            '{"intent":"general_knowledge","reason":"ok"}',
            '```json\n{"intent":"general_knowledge","reason":"ok"}\n```',
            'Here is the route: {"intent":"general_knowledge","reason":"ok"}',
            '{"intent":"general_knowledge","reason":"ok"} trailing prose',
        ]
        for content in cases:
            with self.subTest(content=content):
                self.assertEqual(_json_from_text(content), expected)

    def test_balancing_ignores_braces_and_escaped_quotes_inside_strings(self) -> None:
        content = 'prefix {"reason":"literal { brace } and \\"quoted\\" text","intent":"unclear"} suffix'
        payload = _json_from_text(content)
        self.assertEqual(payload["intent"], "unclear")
        self.assertIn("{ brace }", payload["reason"])
        self.assertIn('"quoted"', payload["reason"])

    def test_unterminated_object_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unbalanced_object"):
            _extract_first_json_object('reason {"intent":"unclear"')

    def test_only_first_of_two_objects_is_used(self) -> None:
        payload = _json_from_text('{"intent":"general_knowledge"} {"intent":"graph_path"}')
        self.assertEqual(payload["intent"], "general_knowledge")


class LLMPrimaryRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self.entities = EntityResolver().resolve("Tell me about 192.168.30.115")

    def test_valid_glm_decision_normalizes_without_fallback(self) -> None:
        router = GLMIntentRouter(
            self.settings,
            FakeLLMClient([fake_result('{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_detection":true,"requires_asset_profile":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.92,"reason":"asset question"}')]),  # type: ignore[arg-type]
        )
        decision = router.classify("Tell me about 192.168.30.115", self.entities, SessionRoutingState())
        route = normalize_intent_route(decision, self.entities)
        self.assertEqual(route.decision_source, "semantic_router")
        self.assertFalse(route.fallback_used)
        self.assertEqual(route.scope, "node_summary")
        self.assertTrue(route.use_detection)

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
                fake_result('{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"ok"}'),
            ]),  # type: ignore[arg-type]
        )
        decision = router.classify("192.168.30.115", self.entities, SessionRoutingState())
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.retry_count, 0)
        self.assertEqual(len(router.llm_client.calls), 1)

    def test_provider_error_and_low_confidence_fall_back(self) -> None:
        llm = FakeLLMClient([LLMError("boom", reason="timeout")])
        router = GLMIntentRouter(self.settings, llm)  # type: ignore[arg-type]
        self.assertTrue(router.classify("x", self.entities, SessionRoutingState()).fallback_used)
        self.assertEqual(len(llm.calls), 1)
        low = GLMIntentRouter(
            make_settings(intent_router_retry_enabled=False),
            FakeLLMClient([fake_result('{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.2,"reason":"low"}')]),  # type: ignore[arg-type]
        )
        self.assertTrue(low.classify("x", self.entities, SessionRoutingState()).fallback_used)

    def test_router_uses_router_specific_generation_settings_and_retry_budget(self) -> None:
        settings = make_settings(
            router_temperature=0.0,
            router_top_p=0.1,
            router_max_tokens=77,
            router_retry_max_tokens=155,
            router_timeout_seconds=13,
            router_supports_temperature=True,
            router_supports_top_p=True,
        )
        llm = FakeLLMClient([
            fake_result("", finish_reason="length"),
            fake_result('{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"ok"}'),
        ])
        router = GLMIntentRouter(settings, llm)  # type: ignore[arg-type]
        decision = router.classify("192.168.30.115", self.entities, SessionRoutingState())
        self.assertTrue(decision.fallback_used)
        self.assertEqual(llm.calls[0]["temperature"], 0.0)
        self.assertEqual(llm.calls[0]["top_p"], 0.1)
        self.assertEqual(llm.calls[0]["max_tokens"], 77)
        self.assertEqual(llm.calls[0]["timeout_seconds"], 13)
        self.assertEqual(llm.calls[0]["transient_retries"], 0)
        self.assertEqual(llm.calls[0]["purpose"], "intent_router")
        self.assertEqual(len(llm.calls), 1)

    def test_invalid_enum_and_missing_fields_use_one_content_repair(self) -> None:
        valid = (
            '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,'
            '"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,'
            '"classification_confidence":0.9,"reason":"repaired"}'
        )
        invalid_outputs = [
            valid.replace('"asset_investigation"', '"invalid_intent"'),
            '{"intent":"asset_investigation"}',
        ]
        for invalid in invalid_outputs:
            with self.subTest(invalid=invalid):
                llm = FakeLLMClient([fake_result(invalid), fake_result(valid)])
                router = GLMIntentRouter(self.settings, llm)  # type: ignore[arg-type]
                decision = router.classify("Tell me about 192.168.30.115", self.entities, SessionRoutingState())
                self.assertFalse(decision.fallback_used)
                self.assertEqual(decision.retry_count, 1)
                self.assertEqual(len(llm.calls), 2)

    def test_repair_failure_uses_deterministic_fallback(self) -> None:
        llm = FakeLLMClient([fake_result('{"intent":"invalid"}'), fake_result('{"scope":"still-invalid"}')])
        router = GLMIntentRouter(self.settings, llm)  # type: ignore[arg-type]
        decision = router.classify("Tell me about 192.168.30.115", self.entities, SessionRoutingState())
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.retry_count, 1)
        self.assertEqual(len(llm.calls), 2)

    def test_router_logs_actual_completion_tokens(self) -> None:
        valid = (
            '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,'
            '"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,'
            '"classification_confidence":0.9,"reason":"ok"}'
        )
        router = GLMIntentRouter(
            self.settings,
            FakeLLMClient([fake_result(valid, usage={"completion_tokens": 37})]),  # type: ignore[arg-type]
        )
        with self.assertLogs("src.core.context.intent", level="INFO") as logs:
            router.classify("Tell me about 192.168.30.115", self.entities, SessionRoutingState())
        self.assertIn("completion_tokens=37", "\n".join(logs.output))

    def test_exact_prompt_a_accepts_combined_node_summary_from_glm(self) -> None:
        prompt = (
            "this is one of our asset 192.168.0.125 , i want you analyze this deeply and give me analytical "
            "report based on all evidence."
        )
        entities = EntityResolver().resolve(prompt)
        payload = (
            '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,'
            '"requires_graph":true,"requires_detection":true,"requires_asset_profile":true,'
            '"entity_binding":"explicit","requires_multiple_entities":false,"is_followup":false,'
            '"classification_confidence":0.97,"reason":"combined analysis"}'
        )
        router = GLMIntentRouter(self.settings, FakeLLMClient([fake_result(payload)]))  # type: ignore[arg-type]

        decision = router.classify(prompt, entities, SessionRoutingState())

        self.assertEqual(decision.decision_source, "semantic_router")
        self.assertEqual(decision.scope, "node_summary")
        self.assertTrue(decision.requires_graph)
        self.assertTrue(decision.requires_detection)
        self.assertTrue(decision.requires_asset_profile)
        self.assertEqual(decision.entity_binding, "explicit")

    def test_exact_prompt_b_repair_preserves_combined_full_neighbors(self) -> None:
        prompt = (
            "now give me all of its connections ,its impacts on another assets ,and make complete your analysis , "
            "at the end give me comprehensive analytical report based on all evidence."
        )
        state = SessionRoutingState(
            active_ip="192.168.0.125",
            last_provider="detection",
            previous_intent="asset_investigation",
            previous_scope="none",
            previous_direction="none",
            previous_depth=0,
            previous_requires_detection=True,
            previous_requires_asset_profile=True,
        )
        entities = EntityResolver().resolve(prompt, routing_state=state)
        repaired = (
            '{"intent":"asset_investigation","scope":"full_neighbors","direction":"both","depth":1,'
            '"requires_graph":true,"requires_detection":true,"requires_asset_profile":true,'
            '"entity_binding":"active_single","requires_multiple_entities":false,"is_followup":true,'
            '"classification_confidence":0.97,"reason":"combined follow-up"}'
        )
        llm = FakeLLMClient([fake_result('{"intent":"asset_investigation"', finish_reason="length"), fake_result(repaired)])
        router = GLMIntentRouter(self.settings, llm)  # type: ignore[arg-type]

        decision = router.classify(prompt, entities, state)

        self.assertEqual(decision.decision_source, "semantic_router_repair")
        self.assertEqual(decision.retry_count, 1)
        self.assertEqual(decision.scope, "full_neighbors")
        self.assertEqual(decision.depth, 1)
        self.assertTrue(decision.requires_graph)
        self.assertTrue(decision.requires_detection)
        self.assertTrue(decision.requires_asset_profile)
        self.assertEqual(decision.entity_binding, "active_single")

    def test_router_prompt_loads_from_file_and_missing_file_falls_back(self) -> None:
        router = GLMIntentRouter(self.settings, FakeLLMClient([fake_result("{}")]))  # type: ignore[arg-type]
        self.assertIn("classification_confidence", router.system_prompt)
        self.assertIn("multi_entity_comparison", router.system_prompt)
        missing = GLMIntentRouter(
            make_settings(intent_router_system_prompt_path="/tmp/soorin-missing-router-prompt.md"),
            FakeLLMClient([fake_result("{}")]),  # type: ignore[arg-type]
        )
        self.assertEqual(missing.system_prompt, ROUTER_SYSTEM_PROMPT_FALLBACK)
        self.assertLess(len(ROUTER_SYSTEM_PROMPT_FALLBACK), 600)


class DeterministicFallbackPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = DeterministicFallbackRouter()
        self.resolver = EntityResolver()

    def test_explicit_combined_provider_request_uses_graph_and_detection(self) -> None:
        message = "Analyze 192.168.20.149 using both detection and graph evidence."
        route = self.router.route(message, self.resolver.resolve(message), fallback_reason="provider_error")

        self.assertTrue(route.use_graph)
        self.assertTrue(route.use_detection)
        self.assertEqual(route.intent, "asset_investigation")
        self.assertIn(route.matched_signals[0], {"graph_topology", "asset_evidence"})

    def test_referential_fallback_materializes_active_single_and_marks_followup(self) -> None:
        state = SessionRoutingState(
            active_ip="192.168.21.104",
            last_provider="combined",
            last_providers=("graph", "detection"),
            previous_intent="asset_investigation",
            previous_scope="node_summary",
            previous_direction="both",
            previous_depth=0,
            previous_requires_detection=True,
            previous_requires_asset_profile=False,
        )
        message = "What do we know about it?"
        route = self.router.route(
            message,
            self.resolver.resolve(message, routing_state=state),
            state,
            fallback_reason="provider_error",
        )
        self.assertEqual(route.entity_binding, "active_single")
        self.assertEqual(route.materialized_entities, ("192.168.21.104",))
        self.assertTrue(route.followup_detected)

    def test_exact_prompt_a_fallback_uses_combined_node_summary(self) -> None:
        message = (
            "this is one of our asset 192.168.0.125 , i want you analyze this deeply and give me analytical "
            "report based on all evidence."
        )
        route = self.router.route(message, self.resolver.resolve(message), fallback_reason="provider_error")
        self.assertEqual(route.scope, "node_summary")
        self.assertEqual(route.direction, "both")
        self.assertEqual(route.depth, 0)
        self.assertTrue(route.use_graph)
        self.assertTrue(route.use_detection)
        self.assertTrue(route.use_asset_profile)
        self.assertEqual(route.entity_binding, "explicit")

    def test_exact_prompt_b_fallback_uses_combined_full_neighbors(self) -> None:
        message = (
            "now give me all of its connections ,its impacts on another assets ,and make complete your analysis , "
            "at the end give me comprehensive analytical report based on all evidence."
        )
        state = SessionRoutingState(
            active_ip="192.168.0.125",
            last_provider="detection",
            previous_intent="asset_investigation",
            previous_scope="none",
            previous_direction="none",
            previous_depth=0,
            previous_requires_detection=True,
            previous_requires_asset_profile=True,
        )
        route = self.router.route(
            message,
            self.resolver.resolve(message, routing_state=state),
            state,
            fallback_reason="provider_error",
        )
        self.assertEqual(route.scope, "full_neighbors")
        self.assertEqual(route.direction, "both")
        self.assertEqual(route.depth, 1)
        self.assertTrue(route.use_graph)
        self.assertTrue(route.use_detection)
        self.assertTrue(route.use_asset_profile)
        self.assertEqual(route.entity_binding, "active_single")

    def test_explicit_graph_detail_phrases_map_to_full_and_two_hop(self) -> None:
        cases = [
            ("Show all direct connections for 192.168.0.125.", "full_neighbors", 1),
            ("Show second-degree connections for 192.168.0.125.", "two_hop", 2),
        ]
        for message, scope, depth in cases:
            with self.subTest(message=message):
                entities = self.resolver.resolve(message)
                route = self.router.route(message, entities, fallback_reason="provider_error")
                self.assertEqual(route.scope, scope)
                self.assertEqual(route.depth, depth)
                self.assertTrue(route.use_graph)
                self.assertFalse(route.use_detection)

    def test_topology_summary_uses_node_summary_not_heavy_retrieval(self) -> None:
        message = "Summarize the topology around 192.168.21.104."
        route = self.router.route(message, self.resolver.resolve(message), fallback_reason="provider_error")
        self.assertEqual(route.scope, "node_summary")
        self.assertEqual(route.depth, 0)
        self.assertTrue(route.use_graph)

    def test_comprehensive_report_always_uses_complete_provider_payloads(self) -> None:
        summary_message = "Give me a comprehensive report for 192.168.21.104."
        full_message = (
            "Give me a comprehensive report including every detection rule and all supporting signals "
            "for 192.168.21.104."
        )
        summary_route = self.router.route(
            summary_message,
            self.resolver.resolve(summary_message),
            fallback_reason="provider_error",
        )
        full_route = self.router.route(
            full_message,
            self.resolver.resolve(full_message),
            fallback_reason="provider_error",
        )
        self.assertTrue(summary_route.use_detection)
        self.assertTrue(full_route.use_detection)
        self.assertTrue(summary_route.use_asset_profile)
        self.assertTrue(full_route.use_asset_profile)

    def test_combined_full_detection_fallback_preserves_heavy_graph_scope(self) -> None:
        cases = [
            (
                "Show all direct connections for 192.168.0.55 and include every matched detection rule, conflict, and supporting classification signal.",
                "full_neighbors",
                1,
                False,
            ),
            (
                "Perform a two-hop investigation of 192.168.21.104 using all detection rules, supporting signals, conflicts, and complete profile evidence.",
                "two_hop",
                2,
                True,
            ),
        ]
        for message, scope, depth, requires_profile in cases:
            with self.subTest(scope=scope):
                route = self.router.route(message, self.resolver.resolve(message), fallback_reason="provider_error")
                self.assertEqual(route.intent, "asset_investigation")
                self.assertEqual(route.scope, scope)
                self.assertEqual(route.depth, depth)
                self.assertTrue(route.use_graph)
                self.assertTrue(route.use_detection)
                self.assertEqual(route.use_asset_profile, requires_profile)

    def test_dependency_and_destination_phrases_set_direction(self) -> None:
        cases = [
            ("Which systems depend on 192.168.0.125?", "inbound"),
            ("What destinations does 192.168.0.125 reach?", "outbound"),
            ("Show connections for 192.168.0.125.", "both"),
        ]
        for message, direction in cases:
            with self.subTest(message=message):
                route = self.router.route(message, self.resolver.resolve(message), fallback_reason="provider_error")
                self.assertTrue(route.use_graph)
                self.assertEqual(route.direction, direction)

    def test_entity_bound_anomaly_network_request_is_graph_aware(self) -> None:
        state = SessionRoutingState(
            active_ip="192.168.20.149",
            last_provider="graph",
            previous_intent="asset_investigation",
            previous_scope="node_summary",
            previous_direction="both",
            previous_depth=0,
        )
        message = "get anomaly network for this asset"
        entities = self.resolver.resolve(message, {"selected_ip": "192.168.20.149"}, state)
        route = self.router.route(message, entities, state, fallback_reason="provider_error")

        self.assertNotEqual(route.intent, "general_knowledge")
        self.assertTrue(route.use_graph)
        self.assertTrue(route.use_detection)
        self.assertTrue(route.use_asset_profile)
        self.assertIn(route.matched_signals[0], {"graph_topology", "security_or_anomaly", "all_product_and_graph_evidence"})
        self.assertIn("security_or_anomaly", route.matched_signals)

    def test_graph_only_inbound_request_uses_graph(self) -> None:
        state = SessionRoutingState(active_ip="192.168.20.149")
        message = "show inbound connections for this asset"
        entities = self.resolver.resolve(message, None, state)
        route = self.router.route(message, entities, state, fallback_reason="provider_error")

        self.assertTrue(route.use_graph)
        self.assertFalse(route.use_detection)
        self.assertEqual(route.direction, "inbound")

    def test_general_knowledge_without_entity_uses_no_live_provider(self) -> None:
        message = "What is defense in depth?"
        route = self.router.route(message, self.resolver.resolve(message), fallback_reason="provider_error")

        self.assertEqual(route.intent, "general_knowledge")
        self.assertFalse(route.use_graph)
        self.assertFalse(route.use_detection)

    def test_entity_followup_inherits_previous_operational_route(self) -> None:
        state = SessionRoutingState(
            active_ip="192.168.20.149",
            last_provider="graph",
            previous_intent="graph_neighbors",
            previous_scope="one_hop",
            previous_direction="outbound",
            previous_depth=1,
        )
        message = "What else about it?"
        entities = self.resolver.resolve(message, None, state)
        route = self.router.route(message, entities, state, fallback_reason="provider_error")

        self.assertTrue(route.use_graph)
        self.assertEqual(route.scope, "one_hop")
        self.assertEqual(route.matched_signals, ["previous_operational_route"])

    def test_explicit_topic_detachment_does_not_inherit_asset_route(self) -> None:
        state = SessionRoutingState(
            active_ip="192.168.20.149",
            last_provider="graph",
            previous_intent="asset_investigation",
            previous_scope="node_summary",
        )
        message = "Explain phishing in general."
        entities = self.resolver.resolve(message, None, state)
        route = self.router.route(message, entities, state, fallback_reason="provider_error")

        self.assertFalse(route.use_graph)
        self.assertFalse(route.use_detection)
        self.assertEqual(route.reason, "fallback_topic_detachment")


class TopologyGraphSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = nx.DiGraph()
        self.graph.add_edge("192.168.0.125", "192.168.0.126")
        self.graph.add_node("192.168.30.115")

    def test_graph_node_click_selects_ip(self) -> None:
        action, selected_ip, event_id = _resolve_graph_selection_event(
            {"action": "select", "node": "192.168.0.125", "event_id": "evt-1"},
            self.graph,
        )

        self.assertEqual(action, "select")
        self.assertEqual(selected_ip, "192.168.0.125")
        self.assertEqual(event_id, "evt-1")

    def test_graph_background_click_clears_selection(self) -> None:
        action, selected_ip, event_id = _resolve_graph_selection_event(
            {"action": "clear", "node": None, "event_id": "evt-2"},
            self.graph,
        )

        self.assertEqual(action, "clear")
        self.assertIsNone(selected_ip)
        self.assertEqual(event_id, "evt-2")

    def test_pyvis_bridge_emits_background_clear_event(self) -> None:
        html = _inject_node_click_bridge("<html><body></body></html>")

        self.assertIn('action: "clear"', html)
        self.assertIn("sendClearSelection", html)
        self.assertIn("unselectAll", html)

    def test_copilot_request_context_omits_selected_ip_after_clear(self) -> None:
        action, selected_ip, _ = _resolve_graph_selection_event(
            {"action": "clear", "node": None, "event_id": "evt-3"},
            self.graph,
        )

        self.assertEqual(action, "clear")
        self.assertIsNone(build_copilot_ui_context(selected_ip))

    def test_graph_node_click_after_clear_selects_new_ip(self) -> None:
        clear_action, cleared_ip, _ = _resolve_graph_selection_event(
            {"action": "clear", "node": None, "event_id": "evt-4"},
            self.graph,
        )
        select_action, selected_ip, event_id = _resolve_graph_selection_event(
            {"action": "select", "node": "192.168.30.115", "event_id": "evt-5"},
            self.graph,
        )

        self.assertEqual(clear_action, "clear")
        self.assertIsNone(cleared_ip)
        self.assertEqual(select_action, "select")
        self.assertEqual(selected_ip, "192.168.30.115")
        self.assertEqual(event_id, "evt-5")


class CopilotHelpContentTests(unittest.TestCase):
    def test_help_prefers_dialog_when_streamlit_supports_it(self) -> None:
        self.assertEqual(choose_help_ui_pattern(SimpleNamespace(dialog=object())), "dialog")
        self.assertEqual(choose_help_ui_pattern(SimpleNamespace()), "popover")

    def test_help_explains_authority_and_evidence_sources(self) -> None:
        content = get_copilot_help_content()
        authority = " ".join(content.authority)
        evidence = " ".join(content.evidence)
        tips = " ".join(content.tips)

        self.assertIn("IP written in your prompt", authority)
        self.assertIn("selected topology asset", authority)
        self.assertIn("previous active asset", authority)
        self.assertIn("Click empty graph space", tips)
        self.assertIn("Detection provides", evidence)
        self.assertIn("Graph provides", evidence)
        self.assertIn("Knowledge retrieval", evidence)

    def test_help_examples_cover_current_route_shapes(self) -> None:
        content = get_copilot_help_content()
        groups = {group.title: group for group in content.examples}

        for title in (
            "Summarize an asset",
            "Check identity and role",
            "Review detections and risk",
            "Explore network connections",
            "Run a combined investigation",
            "Compare two assets",
            "Find an observed path",
            "Ask cybersecurity questions",
            "Use follow-up questions",
        ):
            self.assertIn(title, groups)

        examples = "\n".join(example for group in content.examples for example in group.examples)
        self.assertIn("Show all inbound peers", examples)
        self.assertIn("Show all outbound peers", examples)
        self.assertIn("two-hop neighborhood", examples)
        self.assertIn("communicate directly", examples)
        self.assertIn("shortest graph path", examples)
        self.assertIn("Tell me more about it", examples)
        self.assertIn("Compare them", examples)

    def test_help_content_does_not_change_copilot_selected_ip_payload(self) -> None:
        before = build_copilot_ui_context("192.168.21.1")
        get_copilot_help_content()
        choose_help_ui_pattern(SimpleNamespace(dialog=object()))
        after = build_copilot_ui_context("192.168.21.1")

        self.assertEqual(before, {"selected_ip": "192.168.21.1"})
        self.assertEqual(after, before)
        self.assertIsNone(build_copilot_ui_context(None))


class ExhaustiveConnectionRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = EntityResolver()
        self.fallback = DeterministicFallbackRouter()

    def test_explicit_and_active_single_exhaustive_wording_uses_full_neighbors(self) -> None:
        cases = (
            "Show all connections for 192.168.0.125",
            "List every inbound, outbound, and bidirectional peer for 192.168.0.125",
            "Show the complete neighborhood of 192.168.0.125",
        )
        for message in cases:
            with self.subTest(message=message):
                route = self.fallback.route(message, self.resolver.resolve(message), fallback_reason="test")
                self.assertEqual((route.scope, route.depth), ("full_neighbors", 1))
                self.assertTrue(route.use_graph)
                self.assertTrue(route.exhaustive_connections_requested)

        state = SessionRoutingState(active_ip="192.168.0.125")
        for message in ("Show all of its connections.", "Now list every peer.", "Based on all of its connections, analyze it."):
            with self.subTest(message=message):
                route = self.fallback.route(message, self.resolver.resolve(message, routing_state=state), state, fallback_reason="test")
                self.assertEqual(route.scope, "full_neighbors")
                self.assertEqual(route.binding_source, "conversation")

    def test_summary_wording_does_not_expand_to_full_neighbors(self) -> None:
        for message in (
            "Summarize the connections of 192.168.0.125",
            "Analyze the network behavior of 192.168.0.125",
            "Give me a graph overview for 192.168.0.125",
        ):
            with self.subTest(message=message):
                route = self.fallback.route(message, self.resolver.resolve(message), fallback_reason="test")
                self.assertNotEqual(route.scope, "full_neighbors")
                self.assertFalse(route.exhaustive_connections_requested)

    def test_active_pair_complete_neighborhood_followup_keeps_both_entities(self) -> None:
        state = SessionRoutingState(active_entities=("192.168.0.125", "192.168.0.126"))
        message = "Compare their complete neighborhoods."
        route = self.fallback.route(message, self.resolver.resolve(message, routing_state=state), state, fallback_reason="test")
        self.assertEqual(route.scope, "multi_entity_comparison")
        self.assertEqual(route.materialized_entities, state.active_entities)
        self.assertTrue(route.requires_multiple_entities)
        self.assertTrue(route.exhaustive_connections_requested)

    def test_semantic_payload_is_normalized_to_exhaustive_single_and_pair_scopes(self) -> None:
        base = {
            "intent": "asset_investigation",
            "scope": "node_summary",
            "direction": "both",
            "depth": 0,
            "requires_graph": True,
            "requires_detection": False,
            "requires_asset_profile": False,
            "requires_multiple_entities": False,
            "is_followup": False,
            "classification_confidence": 0.95,
            "reason": "fixture",
        }
        single = self.resolver.resolve("Show all connections for 192.168.0.125")
        decision = validate_router_payload(base, single, min_confidence=0.65, message="Show all connections for 192.168.0.125")
        self.assertEqual((decision.intent, decision.scope, decision.depth), ("graph_neighbors", "full_neighbors", 1))
        self.assertEqual(decision.route_normalization_reason, "exhaustive_connections_require_full_neighbors")
        self.assertEqual(decision.decision_source, "semantic_router")

        pair_message = "Compare the complete neighborhoods of 192.168.0.125 and 192.168.0.126"
        pair = self.resolver.resolve(pair_message)
        pair_decision = validate_router_payload(base, pair, min_confidence=0.65, message=pair_message)
        self.assertEqual((pair_decision.intent, pair_decision.scope), ("graph_relationships", "multi_entity_comparison"))
        self.assertTrue(pair_decision.requires_multiple_entities)


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
        self.assertEqual(result.context["included_node_count"], 1)
        self.assertEqual(result.context["included_edge_count"], 0)
        self.assertEqual(result.context["context_mode"], "aggregate_only")
        self.assertTrue(result.context["aggregate_only_context"])
        self.assertFalse(result.context["context_truncated"])
        self.assertIsNone(result.context["context_truncation_reason"])
        self.assertEqual(result.context["inbound_context_included"], 0)
        self.assertEqual(result.context["outbound_context_included"], 0)
        self.assertEqual(result.context["bidirectional_context_included"], 0)
        self.assertIn('"representation":"node_summary"', text)
        self.assertNotIn('"top_peers"', text)

    def test_single_node_zero_edge_context_preserves_counter_invariants(self) -> None:
        context = {
            "target_ip": "10.0.0.1",
            "node_found": True,
            "scope": "node_summary",
            "direction": "both",
            "candidate_node_count": 1,
            "retrieved_node_count": 1,
            "candidate_edge_count": 0,
            "retrieved_edge_count": 0,
            "nodes": [{"id": "10.0.0.1", "hop": 0}],
            "edges": [],
        }
        graph_result = GraphProviderResult(provider="graph", status="available", context=context)

        ContextComposer(make_settings()).compose(
            CopilotContextPackage(entities=EntityResolver().resolve("10.0.0.1"), graph=graph_result)
        )

        self.assertEqual(context["included_node_count"], 1)
        self.assertEqual(context["included_edge_count"], 0)
        self.assertFalse(context["context_truncated"])

    def test_security_fallback_graph_context_discloses_missing_anomaly_provider(self) -> None:
        route = replace(
            self.route("node_summary", "both", 0),
            matched_signals=["security_or_anomaly"],
        )
        result = self.provider.provide(self.entity, route=route)

        text = ContextComposer(self.settings).compose(
            CopilotContextPackage(entities=EntityResolver().resolve(self.entity.value), graph=result)
        )

        self.assertFalse(result.context["formal_anomaly_evidence_available"])
        self.assertTrue(result.context["graph_structural_analysis_available"])
        self.assertIn("No dedicated anomaly provider evidence is available", text)

    def test_subnet_formatter_returns_canonical_cidr(self) -> None:
        self.assertEqual(get_subnet("192.168.0.149"), "192.168.0.0/24")
        self.assertEqual(get_subnet("192.168.21.1"), "192.168.21.0/24")
        self.assertEqual(get_subnet("not-an-ip"), "other")

    def test_cidr_subnet_filter_includes_matching_node(self) -> None:
        graph = nx.DiGraph()
        graph.add_edge("192.168.0.125", "192.168.0.126")
        graph.add_edge("192.168.0.125", "192.168.30.115")

        filtered = _filter_graph_by_subnet(graph, "192.168.0.0/24")

        self.assertIn("192.168.0.125", filtered.nodes)
        self.assertIn(("192.168.0.125", "192.168.0.126"), filtered.edges)

    def test_cidr_subnet_filter_excludes_outside_node(self) -> None:
        graph = nx.DiGraph()
        graph.add_edge("192.168.0.125", "192.168.30.115")

        filtered = _filter_graph_by_subnet(graph, "192.168.0.0/24")

        self.assertNotIn("192.168.30.115", filtered.nodes)
        self.assertNotIn(("192.168.0.125", "192.168.30.115"), filtered.edges)

    def test_cidr_subnet_filter_normalizes_whitespace(self) -> None:
        graph = nx.DiGraph()
        graph.add_node("192.168.0.125")

        filtered = _filter_graph_by_subnet(graph, " 192.168.0.0/24 ")

        self.assertEqual(list(filtered.nodes), ["192.168.0.125"])

    def test_cidr_subnet_filter_skips_non_ip_nodes_safely(self) -> None:
        graph = nx.DiGraph()
        graph.add_edge("not-an-ip", "192.168.0.125")
        graph.add_edge("192.168.0.125", "192.168.0.126")

        filtered = _filter_graph_by_subnet(graph, "192.168.0.0/24")

        self.assertNotIn("not-an-ip", filtered.nodes)
        self.assertEqual(set(filtered.nodes), {"192.168.0.125", "192.168.0.126"})

    def test_cidr_filtered_visualization_is_non_empty_when_nodes_match(self) -> None:
        graph = nx.DiGraph()
        graph.add_edge("192.168.0.125", "192.168.0.126")
        graph.add_edge("192.168.0.125", "192.168.30.115")
        replace_active_graph(graph, {"active_graph_source": "test"})

        html = generate_pyvis_graph(max_nodes=20, min_degree=0, subnet_filter="192.168.0.0/24", cdn_resources="in_line")

        self.assertIn("192.168.0.125", html)
        self.assertIn("192.168.0.126", html)
        self.assertNotIn("192.168.30.115", html)

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
        self.assertIn('"complete_for_user_request":true', text)
        self.assertIn('"inbound":33', text)

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
        self.assertIn('"retrieved_peer_count":120', text)
        self.assertIn('"omitted_peer_count":101', text)
        self.assertIn('"serialized_context_truncated":true', text)

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
        self.assertEqual(reason_types, ["node_limit"])

    def test_five_nodes_and_five_edges_below_context_limits_are_not_truncated(self) -> None:
        nodes = [{"id": f"10.0.0.{index}", "hop": index} for index in range(1, 6)]
        edges = [
            {"source": f"10.0.0.{index}", "target": f"10.0.0.{index + 1}"}
            for index in range(1, 5)
        ]
        edges.append({"source": "10.0.0.5", "target": "10.0.0.1"})
        context = {
            "target_ip": "10.0.0.1",
            "node_found": True,
            "scope": "one_hop",
            "direction": "both",
            "candidate_node_count": 5,
            "retrieved_node_count": 5,
            "candidate_edge_count": 5,
            "retrieved_edge_count": 5,
            "nodes": nodes,
            "edges": edges,
        }
        graph_result = GraphProviderResult(
            provider="graph",
            status="available",
            target_entity=ResolvedEntity(type="ip", value="10.0.0.1", source="message"),
            context=context,
        )

        ContextComposer(make_settings(graph_context_max_enumerated_nodes=10, graph_context_max_enumerated_edges=10)).compose(
            CopilotContextPackage(entities=EntityResolver().resolve("10.0.0.1"), graph=graph_result)
        )

        self.assertEqual(context["included_node_count"], 5)
        self.assertEqual(context["included_edge_count"], 0)
        self.assertFalse(context["context_truncated"])
        self.assertIsNone(context["context_truncation_reason"])

    def test_context_limits_remove_records_and_preserve_counter_invariants(self) -> None:
        nodes = [{"id": f"10.0.0.{index}", "hop": index} for index in range(1, 7)]
        edges = [
            {"source": "10.0.0.1", "target": f"10.0.0.{index}"}
            for index in range(2, 7)
        ]
        context = {
            "target_ip": "10.0.0.1",
            "node_found": True,
            "scope": "one_hop",
            "direction": "outbound",
            "candidate_node_count": 6,
            "retrieved_node_count": 6,
            "candidate_edge_count": 5,
            "retrieved_edge_count": 5,
            "nodes": nodes,
            "edges": edges,
        }
        graph_result = GraphProviderResult(provider="graph", status="available", context=context)

        ContextComposer(make_settings(graph_context_max_enumerated_nodes=4, graph_context_max_enumerated_edges=2)).compose(
            CopilotContextPackage(entities=EntityResolver().resolve("10.0.0.1"), graph=graph_result)
        )

        self.assertLessEqual(context["included_node_count"], context["retrieved_node_count"])
        self.assertLessEqual(context["retrieved_node_count"], context["candidate_node_count"])
        self.assertLessEqual(context["included_edge_count"], context["retrieved_edge_count"])
        self.assertLessEqual(context["retrieved_edge_count"], context["candidate_edge_count"])
        self.assertTrue(context["context_truncated"])
        self.assertEqual(context["context_truncation_reasons"], ["one_hop_peer_top_k"])

    def test_edges_with_excluded_endpoint_are_not_counted_as_included(self) -> None:
        context = {
            "target_ip": "10.0.0.1",
            "node_found": True,
            "scope": "one_hop",
            "direction": "outbound",
            "candidate_node_count": 3,
            "retrieved_node_count": 3,
            "candidate_edge_count": 2,
            "retrieved_edge_count": 2,
            "nodes": [{"id": "10.0.0.1"}, {"id": "10.0.0.2"}, {"id": "10.0.0.3"}],
            "edges": [
                {"source": "10.0.0.1", "target": "10.0.0.2"},
                {"source": "10.0.0.1", "target": "10.0.0.3"},
            ],
        }
        result = GraphProviderResult(provider="graph", status="available", context=context)

        ContextComposer(make_settings(graph_context_max_enumerated_nodes=2, graph_context_max_enumerated_edges=10)).compose(
            CopilotContextPackage(entities=EntityResolver().resolve("10.0.0.1"), graph=result)
        )

        self.assertEqual(context["included_node_count"], 2)
        self.assertEqual(context["included_edge_count"], 0)
        self.assertEqual(context["context_truncation_reasons"], ["one_hop_peer_top_k"])

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

    def test_refresh_decision_logs_snapshot_age_interval_and_version(self) -> None:
        service = GraphRefreshService(self.settings, FakeProductClient(self.topology_response()))  # type: ignore[arg-type]
        with self.assertLogs("src.core.graph.refresh", level="INFO") as logs:
            result = service.refresh_once(force=True)
        self.assertEqual(result.status, "ok")
        text = "\n".join(logs.output)
        self.assertIn("refresh_reason=forced", text)
        self.assertIn("snapshot_age_seconds=", text)
        self.assertIn("refresh_interval_seconds=", text)
        self.assertIn("active_snapshot_version=", text)

    def test_configured_cache_intervals_are_at_least_ten_minutes(self) -> None:
        settings = get_settings()
        self.assertGreaterEqual(settings.detection_cache_ttl_seconds, 600)
        self.assertGreaterEqual(settings.graph_refresh_interval_seconds, 600)

    def test_scheduled_refresh_reuses_fresh_active_snapshot(self) -> None:
        graph = nx.DiGraph()
        graph.add_edge("192.168.0.1", "192.168.0.2")
        replace_active_graph(graph, {"active_graph_version": "fresh"})
        client = FakeProductClient(self.topology_response())
        service = GraphRefreshService(self.settings, client)  # type: ignore[arg-type]
        result = service.refresh_once(reason="startup")
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.message, "active_snapshot_fresh")
        self.assertEqual(client.calls, 0)

    def test_scheduled_refresh_runs_after_snapshot_interval(self) -> None:
        graph = nx.DiGraph()
        graph.add_edge("192.168.0.1", "192.168.0.2")
        old = (datetime.now(timezone.utc) - timedelta(seconds=self.settings.graph_refresh_interval_seconds + 1)).isoformat()
        replace_active_graph(graph, {"active_graph_version": "old", "active_graph_loaded_at": old})
        client = FakeProductClient(self.topology_response())
        service = GraphRefreshService(self.settings, client)  # type: ignore[arg-type]
        result = service.refresh_once(reason="interval_elapsed")
        self.assertEqual(result.status, "ok")
        self.assertEqual(client.calls, 1)

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

    def test_large_to_small_valid_refresh_replaces_active_and_persisted_snapshot(self) -> None:
        initial = nx.DiGraph()
        initial.add_nodes_from(f"stale-{index}" for index in range(62_815))
        replace_active_graph(initial, {"active_graph_source": "test"})
        candidate = nx.DiGraph()
        candidate.add_nodes_from(f"current-{index}" for index in range(350))
        candidate.add_edge("current-0", "current-1")
        response = ProductTopologyResponse(
            raw_payload=[{"src_ip": "a", "dst_ip": "b"}],
            records=[TopologyConnectionRecord("a", "b")],
            endpoint_path="/topology",
            status_code=200,
            elapsed_seconds=0.01,
        )
        service = GraphRefreshService(self.settings, FakeProductClient(response))  # type: ignore[arg-type]
        with patch("src.core.graph.refresh.build_topology_graph", return_value=(candidate, 3_084)):
            result = service.refresh_once()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.activated)
        self.assertEqual(get_cached_graph().number_of_nodes(), 350)
        with Path(self.settings.graph_pickle_path).open("rb") as handle:
            persisted = pickle.load(handle)
        self.assertEqual(persisted.number_of_nodes(), 350)
        self.assertTrue(Path(result.processed_snapshot_path).exists())

    def test_small_to_large_valid_refresh_replaces_active_snapshot(self) -> None:
        initial = nx.DiGraph()
        initial.add_nodes_from(f"old-{index}" for index in range(350))
        replace_active_graph(initial, {"active_graph_source": "test"})
        candidate = nx.DiGraph()
        candidate.add_nodes_from(f"current-{index}" for index in range(62_815))
        candidate.add_edge("current-0", "current-1")
        response = ProductTopologyResponse(
            raw_payload=[{"src_ip": "a", "dst_ip": "b"}],
            records=[TopologyConnectionRecord("a", "b")],
            endpoint_path="/topology",
            status_code=200,
            elapsed_seconds=0.01,
        )
        service = GraphRefreshService(self.settings, FakeProductClient(response))  # type: ignore[arg-type]
        with patch("src.core.graph.refresh.build_topology_graph", return_value=(candidate, 3_084)):
            result = service.refresh_once()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.activated)
        self.assertEqual(get_cached_graph().number_of_nodes(), 62_815)

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


class GraphCompletenessContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = "192.168.0.125"
        self.entity = ResolvedEntity(type="ip", value=self.target, source="message")

    def graph_with_inbound(self, count: int) -> nx.DiGraph:
        graph = nx.DiGraph()
        for index in range(count):
            graph.add_edge(f"10.0.0.{index + 1}", self.target)
        replace_active_graph(graph, {"active_graph_source": "test"})
        return graph

    def test_node_summary_is_aggregate_only_without_peer_cap(self) -> None:
        self.graph_with_inbound(30)
        context = retrieve_graph_context(
            GraphRetrievalSpec(scope="node_summary", direction="both", depth=0, entities=[self.entity]),
            make_settings(graph_one_hop_max_nodes=500),
        )
        self.assertEqual(context["requested_scope"], "node_summary")
        self.assertEqual(context["returned_node_count"], 1)
        self.assertEqual(context["returned_edge_count"], 0)
        self.assertTrue(context["retrieval_complete"])
        self.assertFalse(context["retrieval_truncated"])
        self.assertIsNone(context["retrieval_truncation_reason"])
        self.assertTrue(context["requested_scope_complete"])
        self.assertTrue(context["complete_for_user_request"])

    def test_full_neighbors_uses_hard_max_and_reports_incomplete_request(self) -> None:
        self.graph_with_inbound(30)
        context = retrieve_graph_context(
            GraphRetrievalSpec(scope="full_neighbors", direction="both", depth=1, entities=[self.entity], exhaustive_connections_requested=True),
            make_settings(graph_full_neighbors_hard_max=10),
        )
        self.assertLessEqual(context["returned_node_count"], 10)
        self.assertFalse(context["retrieval_complete"])
        self.assertFalse(context["requested_scope_complete"])
        self.assertFalse(context["complete_for_user_request"])

    def test_full_neighbors_within_hard_max_is_complete_for_user(self) -> None:
        self.graph_with_inbound(5)
        context = retrieve_graph_context(
            GraphRetrievalSpec(scope="full_neighbors", direction="both", depth=1, entities=[self.entity], exhaustive_connections_requested=True),
            make_settings(graph_full_neighbors_hard_max=20),
        )
        self.assertTrue(context["retrieval_complete"])
        self.assertTrue(context["requested_scope_complete"])
        self.assertTrue(context["complete_for_user_request"])

    def test_complete_retrieval_and_serialization_are_independent(self) -> None:
        self.graph_with_inbound(12)
        settings = make_settings(
            graph_full_neighbors_hard_max=50,
            graph_full_enumeration_max_peers=2,
            graph_context_max_enumerated_nodes=4,
            graph_context_max_enumerated_edges=4,
        )
        result = GraphContextProvider(settings).provide(
            self.entity,
            route=replace(
                GraphRetrievalTests.route(self, "full_neighbors", "inbound", 1, [self.entity]),
                exhaustive_connections_requested=True,
            ),
        )
        text = ContextComposer(settings).compose(CopilotContextPackage(entities=EntityResolver().resolve(self.target), graph=result))
        self.assertTrue(result.context["retrieval_complete"])
        self.assertTrue(result.context["serialized_context_truncated"])
        self.assertFalse(result.context["serialized_context_complete_for_retrieved_subset"])
        self.assertFalse(result.context["complete_for_user_request"])
        self.assertIn('"complete_for_user_request":false', text)
        self.assertIn("Do not say all connections", text)

    def test_node_summary_totals_do_not_invent_peer_identities(self) -> None:
        self.graph_with_inbound(5)
        settings = make_settings()
        result = GraphContextProvider(settings).provide(
            self.entity,
            route=GraphRetrievalTests.route(self, "node_summary", "both", 0, [self.entity]),
        )
        text = ContextComposer(settings).compose(CopilotContextPackage(entities=EntityResolver().resolve(self.target), graph=result))
        self.assertIn('"inbound_total":5', text)
        self.assertNotIn('"top_peers"', text)
        self.assertEqual(result.context["context_node_count"], 1)
        self.assertEqual(result.context["context_edge_count"], 0)

    def test_pair_graph_context_is_separated_by_entity(self) -> None:
        other = ResolvedEntity(type="ip", value="192.168.0.126", source="message")
        graph = nx.DiGraph()
        graph.add_edge("10.0.0.1", self.target)
        graph.add_edge(other.value, "10.0.0.1")
        replace_active_graph(graph, {"active_graph_source": "test"})
        settings = make_settings()
        result = GraphContextProvider(settings).provide(
            None,
            route=GraphRetrievalTests.route(
                self,
                "multi_entity_comparison",
                "both",
                1,
                [self.entity, other],
                intent="graph_relationships",
                relationship_mode="compare",
            ),
        )
        text = ContextComposer(settings).compose(CopilotContextPackage(entities=EntityResolver().resolve(f"{self.target} {other.value}"), graph=result))
        self.assertIn(f'"entities":{{"{self.target}"', text)
        self.assertIn(f'"{other.value}":', text)
        self.assertIn('"peer_comparison":{', text)
        self.assertIn('"representation":"comparison_summary"', text)


class ComparisonFollowupRegressionTests(unittest.TestCase):
    EXPLICIT_IP = "192.168.0.125"
    ACTIVE_IP = "192.168.21.142"

    class RecordingExecutor:
        def __init__(self) -> None:
            self.plans = []

        def execute(self, plan, **kwargs):
            del kwargs
            self.plans.append(plan)
            results = []
            for step in plan.steps:
                entities = tuple(step.arguments.get("entities") or ())
                is_product = step.capability in {"asset.get_profile", "asset.get_detection"}
                results.append(
                    ToolResult(
                        status="ok",
                        entities=entities,
                        source_capability=step.capability,
                        retrieved_at="fixture",
                        freshness="current",
                        completeness="complete",
                        facts=(EvidenceFact(step.capability, "fixture", {"ok": True}),),
                        step_id=step.id,
                        context_included=True,
                        context_representation="full_minified" if is_product else "included",
                        source_payload_complete=is_product,
                        projection_usable=is_product,
                        usable_fact_count=1,
                    )
                )
            return results

    class InvalidPlanStreamingLLM:
        def __init__(self, route_json: str, plan_json: str) -> None:
            self.route_json = route_json
            self.plan_json = plan_json
            self.chat_purposes = []
            self.stream_purposes = []

        def chat(self, messages, **kwargs):
            del messages
            purpose = kwargs.get("purpose")
            self.chat_purposes.append(purpose)
            return fake_result(self.route_json if purpose == "intent_router" else self.plan_json)

        def stream_chat(self, messages, **kwargs):
            del messages
            self.stream_purposes.append(kwargs.get("purpose"))
            yield LLMStreamEvent("answer_delta", text="Comparison completed.")
            yield LLMStreamEvent(
                "done",
                data={
                    "provider": "fake",
                    "model": "fake",
                    "deployment": "glm",
                    "finish_reason": "stop",
                    "usage": {},
                    "latency_ms": 1,
                    "status_code": 200,
                    "stream_terminated": True,
                },
            )

    def test_missing_active_comparison_returns_clarification_before_planner(self) -> None:
        llm = FakeLLMClient([])
        service = CopilotService(
            make_settings(planner_enabled=True),
            llm,
            MemoryStore(10),
            SessionRoutingStateStore(),
        )

        events = list(
            service.chat_stream(
                f"How is {self.EXPLICIT_IP} different from previous asset?",
                "comparison-missing-active",
                request_id="comparison-missing-active",
            )
        )

        self.assertEqual([event.type for event in events], ["answer_delta", "done"])
        self.assertIn("second IP address", events[0].text)
        self.assertEqual(llm.calls, [])

    def test_three_entity_comparison_clarifies_before_planner(self) -> None:
        route_json = (
            '{"intent":"graph_relationships","scope":"multi_entity_comparison",'
            '"direction":"both","depth":1,"requires_graph":true,'
            '"requires_detection":false,"requires_asset_profile":false,'
            '"entity_binding":"explicit","requires_multiple_entities":true,'
            '"is_followup":false,"classification_confidence":0.95,'
            '"reason":"too many entities"}'
        )
        llm = FakeLLMClient([fake_result(route_json)])
        service = CopilotService(
            make_settings(planner_enabled=True, intent_router_retry_enabled=False),
            llm,
            MemoryStore(10),
            SessionRoutingStateStore(),
        )

        events = list(
            service.chat_stream(
                "Compare 192.168.1.1 192.168.1.2 192.168.1.3",
                "too-many",
                request_id="too-many",
            )
        )

        self.assertEqual([event.type for event in events], ["answer_delta", "done"])
        self.assertIn("no more than two IP", events[0].text)
        self.assertEqual(len(llm.calls), 1)

    def test_comparison_uses_recent_raw_turn_when_active_state_is_missing(self) -> None:
        route_json = (
            '{"intent":"graph_relationships","scope":"multi_entity_comparison",'
            '"direction":"both","depth":1,"requires_graph":true,'
            '"requires_detection":false,"requires_asset_profile":false,'
            '"entity_binding":"explicit","requires_multiple_entities":true,'
            '"is_followup":true,"classification_confidence":0.95,'
            '"reason":"compare explicit asset with recent asset"}'
        )
        llm = FakeLLMClient([fake_result(route_json), fake_result("comparison answer")])
        memory = MemoryStore(10)
        memory.append("recent-comparison", "user", "Tell me about 192.168.21.142.")
        memory.append("recent-comparison", "assistant", "192.168.21.142 was analyzed.")
        service = CopilotService(settings := make_settings(), llm, memory, SessionRoutingStateStore())
        captured_routes = []

        def fake_graph_provider(entity, **kwargs):
            del entity
            captured_routes.append(kwargs["route"])
            return GraphProviderResult(
                provider="graph",
                status="available",
                context={
                    "target_ips": [self.EXPLICIT_IP, self.ACTIVE_IP],
                    "target_ip": self.EXPLICIT_IP,
                    "node_found": True,
                    "scope": "multi_entity_comparison",
                    "direction": "both",
                    "depth": 1,
                    "relationship_mode": "compare",
                    "entities": [self.EXPLICIT_IP, self.ACTIVE_IP],
                    "entity_a": {"ip": self.EXPLICIT_IP, "present": True},
                    "entity_b": {"ip": self.ACTIVE_IP, "present": True},
                    "direct_relationship": {"relationship_status": "unknown"},
                    "degree_comparison": {},
                    "subnet_comparison": {},
                    "nodes": [],
                    "edges": [],
                },
                provenance=ProviderProvenance(source="observed_communication_graph", status="available"),
            )

        service.graph_provider.provide = fake_graph_provider  # type: ignore[method-assign]
        service.chat(
            f"How does {self.EXPLICIT_IP} diverge from the previous asset?",
            "recent-comparison",
            request_id="recent-comparison",
        )

        self.assertEqual(settings.intent_router_enabled, True)
        self.assertEqual(captured_routes[0].scope, "multi_entity_comparison")
        self.assertEqual(captured_routes[0].materialized_entities, (self.EXPLICIT_IP, self.ACTIVE_IP))
        router_context = llm.calls[0]["messages"][1]["content"]
        self.assertIn(self.ACTIVE_IP, router_context)
        self.assertIn("recent_turns", router_context)

    def test_invalid_llm_plan_uses_validated_comparison_fallback_without_stream_error(self) -> None:
        route_json = (
            '{"intent":"graph_relationships","scope":"multi_entity_comparison",'
            '"direction":"both","depth":1,"requires_graph":true,'
            '"requires_detection":true,"requires_asset_profile":true,'
            '"entity_binding":"explicit","requires_multiple_entities":true,'
            '"is_followup":true,"classification_confidence":0.95,'
            '"reason":"compare explicit asset with active asset"}'
        )
        invalid_plan_json = (
            '{"goal":"compare assets","target_entities":["192.168.0.125","192.168.21.142"],'
            '"steps":[{"step_id":"graph-only","capability":"graph.compare_assets",'
            '"arguments":{"entities":["192.168.0.125","192.168.21.142"],'
            '"scope":"multi_entity_comparison","direction":"both","depth":1,'
            '"relationship_mode":"compare"},"depends_on":[],"required":true,'
            '"expected_evidence":"graph_topology"}],'
            '"stop_condition":"required_evidence_collected"}'
        )
        llm = self.InvalidPlanStreamingLLM(route_json, invalid_plan_json)
        state_store = SessionRoutingStateStore()
        state_store.set(
            "comparison-fallback",
            SessionRoutingState(active_ip=self.ACTIVE_IP),
        )
        service = CopilotService(
            make_settings(planner_enabled=True, planner_repair_enabled=True),
            llm,
            MemoryStore(10),
            state_store,
        )
        recording_executor = self.RecordingExecutor()
        service._capability_runtime_snapshot = lambda: (  # type: ignore[method-assign]
            service.capability_registry,
            service.plan_validator,
            recording_executor,
        )

        events = list(
            service.chat_stream(
                f"How is {self.EXPLICIT_IP} different from it?",
                "comparison-fallback",
                request_id="comparison-fallback",
            )
        )

        self.assertNotIn("error", [event.type for event in events])
        self.assertEqual("".join(event.text for event in events if event.type == "answer_delta"), "Comparison completed.")
        self.assertEqual(llm.chat_purposes, ["intent_router", "planner"])
        self.assertEqual(llm.stream_purposes, ["chat"])
        self.assertEqual(len(recording_executor.plans), 2)

        fallback_plans = recording_executor.plans
        fallback = fallback_plans[0]
        expected_entities = (self.EXPLICIT_IP, self.ACTIVE_IP)
        self.assertTrue(all(plan.validated for plan in fallback_plans))
        self.assertTrue(all(plan.source == "deterministic_fallback" for plan in fallback_plans))
        self.assertTrue(all(plan.task.entities == expected_entities for plan in fallback_plans))
        fallback_steps = [step for plan in fallback_plans for step in plan.steps]
        self.assertEqual(
            {step.capability for step in fallback_steps},
            {"asset.get_profile", "asset.get_detection", "graph.compare_assets"},
        )
        for capability in ("asset.get_profile", "asset.get_detection"):
            steps = [step for step in fallback_steps if step.capability == capability]
            self.assertEqual(len(steps), 2)
            self.assertEqual(
                {tuple(step.arguments["entities"]) for step in steps},
                {(self.EXPLICIT_IP,), (self.ACTIVE_IP,)},
            )
        graph_step = next(step for step in fallback_steps if step.capability == "graph.compare_assets")
        self.assertEqual(tuple(graph_step.arguments["entities"]), expected_entities)


class ServiceAndTraceTests(unittest.TestCase):
    def test_state_updates_previous_route_and_general_question_preserves_active_entity(self) -> None:
        settings = make_settings()
        router_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"asset"}'
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
        self.assertEqual(state.active_entities, ("192.168.30.115",))
        self.assertEqual(state.active_entity_count, 1)
        self.assertEqual(state.previous_scope, "node_summary")

    def test_successful_response_stores_latest_raw_user_and_assistant_turn(self) -> None:
        settings = make_settings()
        router_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"asset"}'
        memory = MemoryStore(10)
        service = CopilotService(settings, FakeLLMClient([fake_result(router_json), fake_result("asset answer")]), memory, SessionRoutingStateStore())
        service.graph_provider.provide = lambda entity, **kwargs: GraphProviderResult(  # type: ignore[method-assign]
            provider="graph",
            status="available",
            target_entity=entity,
            context={"target_ip": entity.value, "node_found": True, "scope": kwargs["route"].scope, "direction": "both", "depth": 0},
            provenance=ProviderProvenance(source="observed_communication_graph", status="available"),
        )

        service.chat("Tell me about 192.168.21.142.", "raw-turn", request_id="raw-turn")

        history = memory.get("raw-turn")
        self.assertEqual(history[-2:], [
            {"role": "user", "content": "Tell me about 192.168.21.142."},
            {"role": "assistant", "content": "asset answer"},
        ])

    def test_single_asset_summary_and_possessive_followups_use_graph_and_state(self) -> None:
        settings = make_settings()
        asset_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"asset"}'
        malformed_asset_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"none","depth":1,"requires_graph":false,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":true,"classification_confidence":0.9,"reason":"asset followup"}'
        connections_json = '{"intent":"graph_neighbors","scope":"one_hop","direction":"both","depth":1,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":true,"classification_confidence":0.9,"reason":"connections"}'
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
        self.assertEqual(captured_routes[0].decision_source, "semantic_router")
        self.assertFalse(captured_routes[0].fallback_used)
        self.assertEqual(captured_entities[0].source, "message")

        self.assertEqual(captured_routes[1].intent, "asset_investigation")
        self.assertEqual(captured_routes[1].scope, "node_summary")
        self.assertEqual(captured_routes[1].direction, "both")
        self.assertEqual(captured_routes[1].depth, 0)
        self.assertTrue(captured_routes[1].use_graph)
        self.assertTrue(captured_routes[1].route_normalized)
        self.assertEqual(captured_routes[1].route_normalization_reason, "node_summary_requires_graph")
        self.assertEqual(captured_routes[1].decision_source, "semantic_router")
        self.assertEqual(captured_entities[1].source, "conversation")

        self.assertEqual(captured_routes[2].intent, "graph_neighbors")
        self.assertEqual(captured_routes[2].scope, "one_hop")
        self.assertTrue(captured_routes[2].use_graph)
        self.assertEqual(captured_entities[2].source, "conversation")
        self.assertEqual(captured_entities[3].source, "conversation")

        state = state_store.get("single")
        self.assertEqual(state.active_ip, "192.168.21.1")
        self.assertEqual(state.active_entities, ("192.168.21.1",))
        self.assertEqual(state.active_entity_count, 1)
        self.assertEqual(state.last_provider, "graph")
        self.assertEqual(state.previous_intent, "asset_investigation")
        self.assertEqual(state.previous_scope, "node_summary")
        self.assertEqual(state.previous_direction, "both")
        self.assertEqual(state.previous_depth, 0)

    def test_two_entity_state_is_preserved_for_followup_path(self) -> None:
        settings = make_settings()
        relationship_json = '{"intent":"graph_relationships","scope":"one_hop","direction":"both","depth":1,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":true,"is_followup":false,"classification_confidence":0.9,"reason":"direct"}'
        path_json = '{"intent":"graph_path","scope":"path","direction":"both","depth":0,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":true,"is_followup":true,"classification_confidence":0.9,"reason":"path"}'
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
        router_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"asset"}'
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
        self.assertEqual(state.active_entities, ("192.168.30.115",))
        self.assertEqual(state.active_entity_count, 1)

    def test_general_and_unclear_turns_preserve_active_pair_state(self) -> None:
        settings = make_settings()
        general_json = '{"intent":"general_knowledge","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"general"}'
        unclear_json = '{"intent":"unclear","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"unclear"}'
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
        general_json = '{"intent":"general_knowledge","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"general"}'
        unclear_json = '{"intent":"unclear","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"unclear"}'
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
        router_json = '{"intent":"asset_investigation","scope":"node_summary","direction":"both","depth":0,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"asset"}'
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
        self.assertEqual(state.active_entities, ("192.168.21.2",))
        self.assertEqual(state.active_entity_count, 1)

    def test_explicit_broad_comparison_executes_comparison_route_and_stores_pair(self) -> None:
        settings = make_settings()
        comparison_json = '{"intent":"graph_relationships","scope":"multi_entity_comparison","direction":"both","depth":1,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":true,"is_followup":false,"classification_confidence":0.9,"reason":"compare"}'
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
        router_json = '{"intent":"general_knowledge","scope":"none","direction":"none","depth":0,"requires_graph":false,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.9,"reason":"general"}'
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
        self.assertIn("recent question", serialized)
        self.assertIn("recent short answer", serialized)
        self.assertNotIn(long_old_answer, serialized)

    def test_compaction_preserves_pair_and_them_followup_uses_pair(self) -> None:
        settings = make_settings(
            conversation_summary_trigger_tokens=20,
            conversation_recent_raw_messages=2,
            conversation_summary_max_tokens=80,
        )
        comparison_json = '{"intent":"graph_relationships","scope":"multi_entity_comparison","direction":"both","depth":1,"requires_graph":true,"requires_detection":false,"requires_asset_profile":false,"requires_multiple_entities":true,"is_followup":true,"classification_confidence":0.9,"reason":"compare"}'
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

    def test_compaction_preserves_latest_completed_raw_turn_even_with_small_recent_setting(self) -> None:
        settings = make_settings(conversation_summary_trigger_tokens=20, conversation_recent_raw_messages=1)
        memory = MemoryStore(20)
        memory.append("s", "user", "old " * 100)
        memory.append("s", "assistant", "answer " * 100)
        memory.append("s", "user", "latest user about 192.168.21.142")
        memory.append("s", "assistant", "latest assistant answer")

        snapshot = memory.prepare_for_model("s", settings, SessionRoutingState(active_ip="192.168.21.142"))

        self.assertTrue(snapshot.summary_updated)
        self.assertEqual(
            memory.get("s")[-2:],
            [
                {"role": "user", "content": "latest user about 192.168.21.142"},
                {"role": "assistant", "content": "latest assistant answer"},
            ],
        )
        self.assertEqual(snapshot.messages[-2:], memory.get("s")[-2:])

    def test_episode_transition_keeps_latest_turn_for_routing_but_not_model_context(self) -> None:
        settings = make_settings(conversation_summary_enabled=True, conversation_recent_raw_messages=2)
        memory = MemoryStore(20)
        asset_context = MemoryContextKey(("192.168.21.142",), "asset_investigation", "none", "topology")
        general_context = MemoryContextKey((), "general", "none", "none")
        memory.prepare_for_model("s", settings, SessionRoutingState(), context_key=asset_context)
        memory.record_turn("s", "Tell me about 192.168.21.142.", "asset-only-answer", asset_context)

        snapshot = memory.prepare_for_model("s", settings, SessionRoutingState(), context_key=general_context)

        self.assertTrue(snapshot.episode_transition)
        self.assertNotIn("asset-only-answer", "\n".join(item["content"] for item in snapshot.messages))
        routing_recent = memory.recent_for_routing("s", 2)
        self.assertIn("asset-only-answer", "\n".join(item["content"] for item in routing_recent))

    def test_general_topic_detachment_excludes_asset_raw_history_from_model_context(self) -> None:
        settings = make_settings(conversation_summary_enabled=True, conversation_recent_raw_messages=2)
        memory = MemoryStore(20)
        asset_context = MemoryContextKey(("192.168.21.142",), "asset_investigation", "none", "topology")
        general_context = MemoryContextKey((), "general", "none", "none")
        memory.prepare_for_model("detached", settings, SessionRoutingState(), context_key=asset_context)
        memory.record_turn("detached", "Analyze 192.168.21.142.", "asset raw answer", asset_context)

        snapshot = memory.prepare_for_model("detached", settings, SessionRoutingState(), context_key=general_context)

        rendered = "\n".join(item["content"] for item in snapshot.messages)
        self.assertNotIn("Analyze 192.168.21.142", rendered)
        self.assertNotIn("asset raw answer", rendered)

    def test_trace_renders_router_and_retrieval_sections(self) -> None:
        trace = CopilotRequestTrace("r", "s", "m")
        trace.put("ROUTER INPUT", resolved_entity_count=1)
        trace.put("GRAPH RETRIEVAL", inbound_total=2, inbound_returned=1)
        with self.assertLogs("src.core.copilot.trace", level="INFO") as logs:
            render_human_copilot_trace(trace)
        text = "\n".join(logs.output)
        self.assertIn("ROUTER INPUT", text)
        self.assertIn("GRAPH RETRIEVAL", text)


if __name__ == "__main__":
    unittest.main()
