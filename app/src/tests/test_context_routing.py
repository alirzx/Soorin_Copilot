"""Focused tests for deterministic entity and graph-routing behavior."""

from __future__ import annotations

import unittest
import pickle
import tempfile
from dataclasses import replace
from pathlib import Path

import networkx as nx

from src.config.settings import get_settings
from src.core.context.composer import ContextComposer
from src.core.context.entities import EntityResolver
from src.core.context.intent import GLMIntentRouter
from src.core.context.models import (
    CopilotContextPackage,
    EntityResolution,
    GraphProviderResult,
    IntentDecision,
    ProviderProvenance,
    ResolvedEntity,
)
from src.core.context.providers.graph import GraphContextProvider
from src.core.context.router import GraphContextRouter
from src.core.copilot.trace import CopilotRequestTrace, render_human_copilot_trace
from src.core.graph.loader import load_graph, set_graph_path
from src.core.llm.providers.base import LLMProviderResult
from src.core.memory.routing_state import SessionRoutingState


class EntityResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = EntityResolver()

    def test_explicit_ip_overrides_ui_selection(self) -> None:
        result = self.resolver.resolve(
            "Tell me about 10.10.10.10.",
            ui_context={"selected_ip": "192.168.30.115"},
            routing_state=SessionRoutingState(active_ip="192.168.0.149"),
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.primary_entity.value, "10.10.10.10")
        self.assertEqual(result.primary_entity.source, "message")

    def test_invalid_ipv4_is_not_resolved(self) -> None:
        result = self.resolver.resolve("Tell me about 999.1.1.1.")
        self.assertEqual(result.status, "none")
        self.assertEqual(result.explicit_candidate_count, 1)
        self.assertEqual(result.valid_entity_count, 0)

    def test_multiple_valid_ips_are_ambiguous(self) -> None:
        result = self.resolver.resolve("Compare 10.0.0.1 and 10.0.0.2.")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.valid_entity_count, 2)

    def test_ui_selected_ip_resolves_without_reference_wording(self) -> None:
        result = self.resolver.resolve(
            "What is a firewall?",
            ui_context={"selected_ip": "192.168.30.115"},
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.primary_entity.value, "192.168.30.115")
        self.assertEqual(result.primary_entity.source, "ui")

    def test_it_reference_uses_active_entity(self) -> None:
        result = self.resolver.resolve(
            "What do we know about it?",
            routing_state=SessionRoutingState(active_ip="192.168.30.115"),
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.primary_entity.source, "conversation")

    def test_topic_detachment_suppresses_inherited_entity(self) -> None:
        result = self.resolver.resolve(
            "Not about this asset; what is a MAC address in general?",
            ui_context={"selected_ip": "192.168.30.115"},
            routing_state=SessionRoutingState(active_ip="192.168.30.115"),
        )
        self.assertEqual(result.status, "none")
        self.assertTrue(result.reference_suppressed)


class GraphRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = EntityResolver()
        self.router = GraphContextRouter()

    def test_ui_selected_entity_routes_graph_context(self) -> None:
        entities = self.resolver.resolve(
            "tell me what is this?",
            ui_context={"selected_ip": "192.168.30.115"},
        )
        route = self.router.route("tell me what is this?", entities)
        self.assertTrue(route.use_graph)
        self.assertEqual(route.reason, "ui_selected_graph_context")
        self.assertEqual(route.target_entity.value, "192.168.30.115")

    def test_explicit_asset_question_routes_to_graph(self) -> None:
        entities = self.resolver.resolve("How about 192.168.30.115?")
        route = self.router.route("How about 192.168.30.115?", entities)
        self.assertTrue(route.use_graph)
        self.assertEqual(route.intent, "asset_investigation")

    def test_conversation_followup_routes_after_graph_provider(self) -> None:
        state = SessionRoutingState(active_ip="192.168.30.115", last_provider="graph")
        entities = self.resolver.resolve("Go deeper.", routing_state=state)
        route = self.router.route("Go deeper.", entities, state)
        self.assertTrue(route.use_graph)
        self.assertEqual(route.intent, "graph_followup")

    def test_general_question_without_entity_skips_graph(self) -> None:
        entities = self.resolver.resolve("What is a firewall?")
        route = self.router.route("What is a firewall?", entities)
        self.assertFalse(route.use_graph)

    def test_glm_intent_can_route_uncertain_entity_query(self) -> None:
        entities = self.resolver.resolve("192.168.30.115")
        initial = self.router.route("192.168.30.115", entities)
        self.assertTrue(initial.should_call_intent_router)
        decision = IntentDecision(
            intent="asset_investigation",
            use_graph=True,
            confidence=0.91,
            reason="User likely asks about the resolved IP.",
            decision_source="glm",
            router_called=True,
        )
        route = self.router.route("192.168.30.115", entities, intent_decision=decision)
        self.assertTrue(route.use_graph)
        self.assertEqual(route.decision_source, "glm")


class GraphProviderTests(unittest.TestCase):
    def test_unknown_selected_node_returns_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            graph_path = Path(temp_dir) / "graph.pkl"
            graph = nx.DiGraph()
            graph.add_edge("192.168.30.115", "192.168.0.149")
            with graph_path.open("wb") as handle:
                pickle.dump(graph, handle)

            set_graph_path(graph_path)
            load_graph(force_reload=True)

            entity = EntityResolver().resolve(
                "what is this?",
                ui_context={"selected_ip": "192.168.99.99"},
            ).primary_entity
            result = GraphContextProvider().provide(entity)
            self.assertEqual(result.status, "not_found")
            self.assertFalse(result.context["node_found"])


class ContextComposerTests(unittest.TestCase):
    def test_ui_selected_context_is_labeled_as_relevance_bounded(self) -> None:
        entity = ResolvedEntity(type="ip", value="192.168.30.115", source="ui")
        package = CopilotContextPackage(
            entities=EntityResolution(status="resolved", entities=[entity], primary_entity=entity),
            graph=GraphProviderResult(
                provider="graph",
                status="not_found",
                target_entity=entity,
                context={
                    "target_ip": "192.168.30.115",
                    "node_found": False,
                    "degree": {"in": 0, "out": 0, "total": 0},
                    "limitations": [],
                },
                provenance=ProviderProvenance(source="observed_communication_graph", status="not_found"),
            ),
        )
        text = ContextComposer().compose(package)
        self.assertIn("Active UI-selected investigation entity.", text)
        self.assertIn("Use this evidence only when relevant to the current question.", text)


class FakeLLMClient:
    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, *args, **kwargs) -> LLMProviderResult:
        return LLMProviderResult(text=self.text, provider="fake", model="fake")


class IntentRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = EntityResolver()
        self.settings = replace(
            get_settings(),
            intent_router_enabled=True,
            intent_router_min_confidence=0.65,
            intent_router_timeout_seconds=2,
        )

    def test_malformed_glm_result_falls_back(self) -> None:
        router = GLMIntentRouter(self.settings, FakeLLMClient("not json"))  # type: ignore[arg-type]
        entities = self.resolver.resolve("192.168.30.115")
        decision = router.classify("192.168.30.115", entities, SessionRoutingState())
        self.assertEqual(decision.decision_source, "fallback")
        self.assertFalse(decision.use_graph)

    def test_low_confidence_glm_result_falls_back(self) -> None:
        router = GLMIntentRouter(
            self.settings,
            FakeLLMClient('{"intent":"asset_investigation","use_graph":true,"confidence":0.2}'),  # type: ignore[arg-type]
        )
        entities = self.resolver.resolve("192.168.30.115")
        decision = router.classify("192.168.30.115", entities, SessionRoutingState())
        self.assertEqual(decision.decision_source, "fallback")

    def test_disabled_router_does_not_call_provider(self) -> None:
        settings = replace(self.settings, intent_router_enabled=False)
        router = GLMIntentRouter(settings, FakeLLMClient("{}"))  # type: ignore[arg-type]
        entities = self.resolver.resolve("192.168.30.115")
        decision = router.classify("192.168.30.115", entities, SessionRoutingState())
        self.assertEqual(decision.decision_source, "disabled")
        self.assertFalse(decision.router_called)


class TraceRenderingTests(unittest.TestCase):
    def test_human_trace_renders_request_scoped_sections(self) -> None:
        trace = CopilotRequestTrace(
            request_id="trace123",
            session_id="session123",
            message_preview="hello",
        )
        trace.put("REQUEST", ui_context_present=True, ui_selected_ip="192.168.30.115")
        trace.put("RESULT", status="ok", total_latency_ms=12, warnings=0, errors=0)
        with self.assertLogs("src.core.copilot.trace", level="INFO") as logs:
            render_human_copilot_trace(trace)
        rendered = "\n".join(logs.output)
        self.assertIn("COPILOT REQUEST", rendered)
        self.assertIn("== REQUEST ==", rendered)
        self.assertIn("ui_selected_ip", rendered)


if __name__ == "__main__":
    unittest.main()
