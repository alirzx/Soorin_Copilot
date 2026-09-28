"""Focused offline regression tests for Phase 2/2.1 stabilization."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config.settings import get_settings
from src.core.agent.context_identity import context_result_identity
from src.core.agent.contracts import (
    CapabilitySpec,
    EvidencePack,
    EvidenceFact,
    ExecutionPlan,
    PlanStep,
    RetryPolicy,
    TaskSpec,
    ToolResult,
)
from src.core.agent.evidence import apply_context_inclusion, context_package_from_evidence
from src.core.agent.plan_validator import PlanValidationError, PlanValidator
from src.core.agent.planner import BoundedPlanner, PlannerError
from src.core.agent.registry import CapabilityRegistry, EntityInput
from src.core.agent.reviewer import EvidenceReviewer
from src.core.context.composer import ContextComposer
from src.core.context.entities import EntityResolver
from src.core.context.models import (
    AssetProfileProviderResult,
    CopilotContextPackage,
    DetectionProviderResult,
    GraphProviderResult,
    ProviderProvenance,
    ResolvedEntity,
    approx_tokens,
)
from src.core.context.product_views import build_product_view
from src.core.copilot.service import CopilotService
from src.core.graph.neo4j import _ProjectionAdjacency
from src.core.graph.retrieval import GraphRetrievalPolicy, GraphRetrievalSpec, retrieve_graph_context
from src.core.llm.providers.base import LLMProviderResult
from src.core.llm.token_estimator import TokenEstimator
from src.core.memory.episodes import MemoryContextKey
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.store import MemoryStore
from src.core.rag.models import KnowledgeChunk, KnowledgeSearchResult


IP_A = "192.0.2.10"
IP_B = "192.0.2.20"


def _settings(**overrides):
    return replace(get_settings(), **overrides)


def _task(
    *,
    entities=(IP_A,),
    intent="asset_investigation",
    scope="node_summary",
    relationship_mode="none",
    capability="graph.get_summary",
):
    return TaskSpec(
        request="fixture request",
        intent=intent,
        scope=scope,
        direction="both",
        entities=tuple(entities),
        required_capabilities=(capability,) if capability else (),
        graph_depth=0 if scope == "node_summary" else 1,
        relationship_mode=relationship_mode,
    )


def _graph_tool(capability: str, entities: tuple[str, ...], scope: str) -> ToolResult:
    identity = context_result_identity(capability, entities, scope=scope)
    provider = GraphProviderResult(
        provider="graph",
        status="available",
        context={
            "context_identity": identity,
            "target_ip": entities[0],
            "target_ips": list(entities),
            "scope": scope,
            "requested_scope": scope,
            "nodes": [{"id": entity, "hop": 0} for entity in entities],
            "edges": [],
            "retrieval_complete": True,
            "requested_scope_complete": True,
        },
        provenance=ProviderProvenance(source="fixture_graph", status="available"),
    )
    return ToolResult(
        status="ok",
        entities=entities,
        source_capability=capability,
        retrieved_at="now",
        freshness="current",
        completeness="complete",
        facts=(EvidenceFact(capability, "fixture", True),),
        provider="graph",
        raw_payload=provider.context,
        provider_result=provider,
        context_identity=identity,
    )


def test_node_summary_is_aggregate_only_without_magic_peer_sample():
    graph = _ProjectionAdjacency(
        [IP_A, *[f"192.0.2.{index}" for index in range(30, 60)], *[f"198.51.100.{index}" for index in range(30, 60)]],
        [{"source": f"192.0.2.{index}", "target": IP_A} for index in range(30, 60)]
        + [{"source": IP_A, "target": f"198.51.100.{index}"} for index in range(30, 60)],
    )
    spec = GraphRetrievalSpec(
        scope="node_summary",
        direction="both",
        depth=0,
        entities=[ResolvedEntity("ip", IP_A, "message")],
    )
    result = retrieve_graph_context(graph, spec, _settings())

    assert result["inbound_total"] == 30
    assert result["outbound_total"] == 30
    assert result["retrieved_node_count"] == 1
    assert result["retrieved_edge_count"] == 0
    assert result["nodes"] == [{
        "id": IP_A,
        "hop": 0,
        "subnet": "192.0.2.0/24",
        "inbound": False,
        "outbound": False,
        "bidirectional": False,
    }]


def test_graph_retrieval_policy_caps_one_and_two_hop_working_sets():
    policy = GraphRetrievalPolicy.from_settings(
        _settings(
            graph_one_hop_max_nodes=500,
            graph_two_hop_max_nodes=500,
            graph_max_edges=1000,
        )
    )

    assert policy.one_hop_max_nodes == 100
    assert policy.one_hop_max_edges == 200
    assert policy.two_hop_max_nodes == 150
    assert policy.two_hop_max_edges == 300


@pytest.mark.parametrize(
    ("scope", "expected_cap", "expected_representation", "peer_limit"),
    [
        ("one_hop", 1800, "one_hop_summary", 24),
        ("two_hop", 2500, "two_hop_summary", 36),
    ],
)
def test_neighborhood_serialization_is_bounded_and_reports_omitted_peers(
    scope,
    expected_cap,
    expected_representation,
    peer_limit,
):
    peers = [f"10.20.0.{index}" for index in range(1, 81)]
    context = {
        "target_ip": IP_A,
        "target_ips": [IP_A],
        "scope": scope,
        "requested_scope": scope,
        "direction": "both",
        "depth": 2 if scope == "two_hop" else 1,
        "inbound_total": 40,
        "outbound_total": 40,
        "bidirectional_total": 0,
        "candidate_node_count": 81,
        "retrieved_node_count": 81,
        "candidate_edge_count": 80,
        "retrieved_edge_count": 80,
        "nodes": [
            {"id": IP_A, "hop": 0},
            *(
                {
                    "id": peer,
                    "hop": 2 if scope == "two_hop" and index > 40 else 1,
                    "inbound": index <= 40,
                    "outbound": index > 40,
                    "subnet": "10.20.0.0/24",
                }
                for index, peer in enumerate(peers, start=1)
            ),
        ],
        "edges": [{"source": IP_A, "target": peer} for peer in peers],
        "retrieval_complete": True,
        "retrieval_truncated": False,
        "requested_scope_complete": True,
    }
    graph = GraphProviderResult(provider="graph", status="available", context=context)
    composer = ContextComposer(_settings())

    composer.compose(CopilotContextPackage(entities=EntityResolver().resolve(IP_A), graph=graph))
    payload = json.loads(composer.last_parts["graph"].split("\n", 1)[1].rsplit("\n", 1)[0])

    assert payload["representation"] == expected_representation
    assert payload["serialization_counts"]["included_peer_count"] <= peer_limit
    assert payload["serialization_counts"]["omitted_peer_count"] > 0
    assert payload["serialization_counts"]["serialized_edge_count"] == 0
    assert context["model_context_token_cap"] == expected_cap
    assert context["model_context_token_estimate"] <= expected_cap


def test_path_serializer_includes_only_final_path_and_respects_cap():
    context = {
        "source_ip": IP_A,
        "destination_ip": IP_B,
        "scope": "path",
        "requested_scope": "path",
        "path_exists": True,
        "hop_count": 2,
        "path_nodes": [IP_A, "192.0.2.15", IP_B],
        "path_edges": [
            {"source": IP_A, "target": "192.0.2.15"},
            {"source": "192.0.2.15", "target": IP_B},
        ],
        "nodes": [{"id": "203.0.113.99", "visited_only": True}],
        "edges": [{"source": "203.0.113.99", "target": IP_A}],
        "retrieval_complete": True,
        "retrieval_truncated": False,
        "requested_scope_complete": True,
    }
    graph = GraphProviderResult(provider="graph", status="available", context=context)
    composer = ContextComposer(_settings())

    composer.compose(CopilotContextPackage(entities=EntityResolver().resolve(f"{IP_A} {IP_B}"), graph=graph))
    text = composer.last_parts["graph"]

    assert '"representation":"selected_path"' in text
    assert "203.0.113.99" not in text
    assert context["included_node_count"] == 3
    assert context["included_edge_count"] == 2
    assert context["model_context_token_cap"] == 1200
    assert approx_tokens(text) <= 1200


@pytest.mark.parametrize(
    ("capability", "entities", "scope", "expected"),
    [
        ("graph.get_summary", (IP_A,), "node_summary", f"graph.get_summary:{IP_A}"),
        ("graph.get_neighbors", (IP_A,), "one_hop", f"graph.get_neighbors:{IP_A}:one_hop"),
        ("graph.get_relationship", (IP_A, IP_B), "one_hop", f"graph.get_relationship:{IP_A}|{IP_B}"),
        ("graph.find_path", (IP_A, IP_B), "path", f"graph.find_path:{IP_A}|{IP_B}"),
        ("graph.compare_assets", (IP_B, IP_A), "multi_entity_comparison", f"graph.compare_assets:{IP_A}|{IP_B}"),
    ],
)
def test_all_graph_capabilities_have_stable_result_identity(capability, entities, scope, expected):
    assert context_result_identity(capability, entities, scope=scope) == expected


def test_multiple_graph_results_are_independently_included_and_reviewed():
    summary = _graph_tool("graph.get_summary", (IP_A,), "node_summary")
    comparison = _graph_tool("graph.compare_assets", (IP_A, IP_B), "multi_entity_comparison")
    package = CopilotContextPackage(
        entities=EntityResolver().resolve(f"{IP_A} {IP_B}"),
        graphs=[summary.provider_result, comparison.provider_result],
    )
    composer = ContextComposer(_settings(llm_context_window_tokens=32768))
    text = composer.compose(package)
    updated = apply_context_inclusion([summary, comparison], composer.last_inclusion)

    assert text.count("[SOORIN_GRAPH_CONTEXT_JSON]") == 2
    assert all(result.context_included for result in updated)
    omitted = apply_context_inclusion([comparison], {})
    decision = EvidenceReviewer().review(
        _task(
            entities=(IP_A, IP_B),
            intent="graph_relationships",
            scope="multi_entity_comparison",
            relationship_mode="compare",
            capability="graph.compare_assets",
        ),
        omitted,
    )
    assert decision.outcome == "answer_with_limitations"


def test_planner_prompt_is_tracked_and_malformed_output_uses_one_call(tmp_path: Path):
    prompt = tmp_path / "planner.md"
    prompt.write_text("Return one JSON plan only.", encoding="utf-8")

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            assert messages[0]["content"] == "Return one JSON plan only."
            return SimpleNamespace(text="not-json")

    llm = FakeLLM()
    planner = BoundedPlanner(llm, system_prompt_path=str(prompt))
    with pytest.raises(PlannerError):
        planner.plan(replace(_task(), workflow_mode="multi_step"), (), request_id="r")
    assert llm.calls == 1


def test_missing_planner_prompt_fails_clearly(tmp_path: Path):
    with pytest.raises(RuntimeError, match="missing"):
        BoundedPlanner(SimpleNamespace(), system_prompt_path=str(tmp_path / "missing.md"))


def test_memory_detaches_raw_history_and_retains_bounded_episode_summary():
    settings = _settings(
        conversation_summary_enabled=True,
        conversation_summary_trigger_tokens=20,
        conversation_recent_raw_messages=2,
        conversation_summary_max_tokens=100,
    )
    memory = MemoryStore(20)
    first_key = MemoryContextKey.from_task(_task())
    second_key = MemoryContextKey.from_task(_task(entities=(IP_B,)))
    memory.prepare_for_model("s", settings, SessionRoutingState(), context_key=first_key)
    memory.record_turn("s", f"Investigate {IP_A}", "OLD FULL REPORT " * 100, first_key)

    detached = memory.prepare_for_model(
        "s",
        settings,
        SessionRoutingState(),
        context_key=second_key,
    )
    assert detached.episode_transition
    assert "OLD FULL REPORT" not in json.dumps(detached.messages)
    assert len(memory.repository.list_episodes("s")) == 1

    revisited = memory.prepare_for_model(
        "s",
        settings,
        SessionRoutingState(),
        context_key=first_key,
    )
    assert revisited.previous_episode_summary_included
    assert "[SOORIN CONVERSATION SUMMARY]" in revisited.messages[0]["content"]


def test_general_topic_does_not_receive_asset_episode_history():
    settings = _settings()
    memory = MemoryStore(10)
    asset_key = MemoryContextKey.from_task(_task())
    general_key = MemoryContextKey.from_task(_task(entities=(), intent="general_knowledge", scope="none", capability=""))
    memory.prepare_for_model("s", settings, SessionRoutingState(), context_key=asset_key)
    memory.record_turn("s", "asset", "asset-only-answer", asset_key)
    snapshot = memory.prepare_for_model("s", settings, SessionRoutingState(), context_key=general_key)
    assert "asset-only-answer" not in json.dumps(snapshot.messages)


def test_product_full_view_preserves_complete_payload_as_minified_json():
    payload = {
        "classification": {"role": "domain_controller", "confidence": 0.98},
        "identity": {"hostname": "dc-01", "conflicts": ["vendor mismatch"]},
        "risk": {"severity": "high", "anomaly_score": 0.91},
        "behavior": {"kerberos": True, "ldap": True},
        "verbose": [{"noise": "x" * 200} for _ in range(50)],
    }
    view = build_product_view(
        payload,
        provider="detection",
        views=("full",),
        detail="deep",
        max_context_tokens=450,
        purpose="investigation",
    )
    compact = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ordinary = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert view.payload == payload
    assert json.loads(compact) == view.payload
    assert len(compact) <= len(ordinary)
    assert view.token_estimate == approx_tokens(compact)
    assert view.source_payload_complete
    assert view.projection_usable
    assert view.usable_fact_count == view.inventory.scalar_count
    assert not view.truncated
    assert view.projection_omitted_count == 0

    with pytest.raises(ValueError, match="at least 128"):
        build_product_view(
            payload,
            provider="detection",
            views=("overview",),
            detail="brief",
            max_context_tokens=1,
            purpose="fixture",
        )


def test_large_product_payload_is_compacted_for_model_view():
    payload = {
        "classification": {"role": "domain_controller", "confidence": 0.97},
        "identity": {"hostname": "dc-01", "conflicts": ["vendor mismatch"]},
        "risk": {"severity": "critical", "critical_alerts": ["ticket anomaly"]},
        "anomaly": {"important_evidence": "unusual authentication behavior"},
        **{f"verbose_field_{index}": "noise " * 80 for index in range(245)},
    }

    view = build_product_view(
        payload,
        provider="detection",
        views=("overview", "evidence"),
        detail="standard",
        max_context_tokens=900,
        purpose="live_payload_regression",
    )

    assert view.payload != payload
    assert set(view.payload) == {"provider", "views", "projection_metadata"}
    assert view.projection_usable
    assert view.usable_fact_count > 0
    assert view.projection_omitted_count > 0
    assert not view.truncated
    assert view.token_estimate < 900


def test_complete_product_view_preserves_null_false_zero_and_empty_values():
    payload = {
        "null_value": None,
        "false_value": False,
        "zero_value": 0,
        "empty_string": "",
        "empty_array": [],
        "empty_object": {},
    }
    view = build_product_view(
        payload,
        provider="detection",
        views=("full",),
        detail="deep",
        max_context_tokens=200,
        purpose="summary",
    )

    assert view.payload == payload
    assert json.loads(json.dumps(view.payload, separators=(",", ":"))) == payload
    assert view.usable_fact_count == 4
    assert view.projection_usable
    assert not view.truncated
    assert view.projection_omitted_count == 0


def test_multiple_product_views_produce_one_complete_context_block_per_entity():
    payload = {"classification": {"role": "server"}, "null": None, "zero": 0}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    provider_result = DetectionProviderResult(
        provider="detection",
        status="available",
        ip=IP_A,
        raw_payload=payload,
        serialized_json=serialized,
        full_payload_fetched=True,
    )
    view_payloads = {
        view: build_product_view(
            payload,
            provider="detection",
            views=(view,),
            detail="standard",
            max_context_tokens=500,
            purpose="test",
        ).payload
        for view in ("overview", "evidence")
    }
    results = [
        ToolResult(
            status="ok",
            entities=(IP_A,),
            source_capability="asset.get_detection",
            retrieved_at="now",
            freshness="current",
            completeness="complete",
            raw_payload=payload,
            provider_result=provider_result,
            selected_views=(view,),
            view_payload=view_payloads[view],
            source_payload_complete=True,
            projection_usable=True,
            usable_fact_count=3,
        )
        for view in ("overview", "evidence")
    ]
    task = _task(capability="asset.get_detection")
    pack = EvidencePack(task, tuple(results), (), ())
    entities = EntityResolver().resolve(IP_A)
    package = context_package_from_evidence(pack, entities)
    composer = ContextComposer(_settings())

    text = composer.compose(package)
    updated = apply_context_inclusion(results, composer.last_inclusion)

    assert len(package.detections) == 1
    assert package.detections[0].serialized_json != serialized
    assert text.count(f'[ASSET_DETECTION_CONTEXT_JSON ip="{IP_A}"]') == 1
    assert all(result.context_representation == "projected" for result in updated)
    assert all(result.source_payload_complete for result in updated)
    assert all(not result.projection_truncated for result in updated)
    assert all(result.projection_omitted_count == 0 for result in updated)


def test_reviewer_rejects_product_without_complete_minified_contract():
    task = _task(capability="asset.get_detection")
    empty = ToolResult(
        status="ok",
        entities=(IP_A,),
        source_capability="asset.get_detection",
        retrieved_at="now",
        freshness="current",
        completeness="complete",
        context_included=True,
        raw_payload={"classification": "server"},
        selected_views=("overview",),
        source_payload_complete=True,
        projection_usable=False,
        usable_fact_count=0,
    )
    reviewer = EvidenceReviewer()

    final = reviewer.review(task, [empty], allow_supplemental=False)
    retry = reviewer.review(task, [empty], allow_supplemental=True)

    assert final.outcome == "answer_with_limitations"
    assert "usable validated projection" in final.limitations[0]
    assert retry.outcome == "answer_with_limitations"
    assert not retry.supplemental_allowed


def test_graph_comparison_context_is_bounded_and_preserves_other_provider_budgets():
    a_peers = [f"10.0.0.{index}" for index in range(1, 101)]
    b_peers = [*a_peers[:40], *(f"10.0.1.{index}" for index in range(1, 61))]
    graph = GraphProviderResult(
        provider="graph",
        status="available",
        context={
            "target_ip": IP_A,
            "target_ips": [IP_A, IP_B],
            "scope": "multi_entity_comparison",
            "requested_scope": "multi_entity_comparison",
            "source_capability": "graph.compare_assets",
            "entity_a": {"ip": IP_A, "present": True, "total_peer_count": 100, "inbound_total": 60, "outbound_total": 40, "bidirectional_total": 10, "peers_retrieved": a_peers, "subnets": ["10.0.0.0/24"], "retrieval_truncated": False},
            "entity_b": {"ip": IP_B, "present": True, "total_peer_count": 100, "inbound_total": 45, "outbound_total": 55, "bidirectional_total": 5, "peers_retrieved": b_peers, "subnets": ["10.0.0.0/24", "10.0.1.0/24"], "retrieval_truncated": False},
            "direct_relationship": {"a_to_b": True, "b_to_a": False, "relationship_status": "directed"},
            "degree_comparison": {"higher_total_peer_entity": "tie", "broader_outbound_entity": IP_B},
            "subnet_comparison": {"entity_a_subnets": ["10.0.0.0/24"], "entity_b_subnets": ["10.0.0.0/24", "10.0.1.0/24"], "shared_subnets": ["10.0.0.0/24"], "entity_b_unique_subnets": ["10.0.1.0/24"]},
            "shared_peer_total": 40,
            "shared_peers_retrieved": a_peers[:40],
            "entity_a_unique_peer_total": 60,
            "entity_b_unique_peer_total": 60,
            "candidate_node_count": 202,
            "retrieved_node_count": 102,
            "candidate_edge_count": 200,
            "retrieved_edge_count": 200,
            "nodes": [{"id": IP_A}, {"id": IP_B}, *({"id": peer} for peer in a_peers)],
            "edges": [{"source": IP_A, "target": peer} for peer in a_peers] + [{"source": IP_B, "target": peer} for peer in b_peers],
            "retrieval_complete": True,
            "retrieval_truncated": False,
            "requested_scope_complete": True,
        },
        provenance=ProviderProvenance(source="fixture_graph", status="available"),
    )
    product_payload = {"classification": {"role": "server"}, "zero": 0, "missing": None}
    detection = DetectionProviderResult(
        provider="detection",
        status="available",
        ip=IP_A,
        serialized_json=json.dumps(product_payload, separators=(",", ":"), sort_keys=True),
        raw_payload=product_payload,
        full_payload_fetched=True,
    )
    profile = AssetProfileProviderResult(
        provider="asset_profile",
        status="available",
        ip=IP_B,
        serialized_json=json.dumps(product_payload, separators=(",", ":"), sort_keys=True),
        raw_payload=product_payload,
        full_payload_fetched=True,
    )
    chunk = KnowledgeChunk("c1", "d1", "Bounded SOC guidance.", 0.9)
    knowledge = KnowledgeSearchResult(
        status="ok",
        query="comparison guidance",
        backend="fake",
        retrieved_at="now",
        freshness="indexed",
        chunks=(chunk,),
        citations=(chunk.citation(),),
        total_candidates=1,
        included_count=1,
    )
    package = CopilotContextPackage(
        entities=EntityResolver().resolve(f"{IP_A} {IP_B}"),
        graph=graph,
        detections=[detection],
        asset_profiles=[profile],
        knowledge=knowledge,
    )
    composer = ContextComposer(
        _settings(
            llm_context_window_tokens=10000,
            llm_reserved_output_tokens=2000,
            llm_context_safety_margin_tokens=1000,
        )
    )

    composer.compose(package, base_input_tokens=500)
    graph_payload = json.loads(composer.last_parts["graph"].split("\n", 1)[1].rsplit("\n", 1)[0])

    assert graph_payload["representation"] == "comparison_summary"
    assert graph_payload["serialization"]["retrieved_node_records"] == 102
    assert graph_payload["serialization"]["retrieved_edge_records"] == 200
    assert graph_payload["serialization"]["serialized_peer_references"] <= 36
    assert graph_payload["direct_relationship"] == {
        "source": IP_A,
        "target": IP_B,
        "forward_edge": True,
        "reverse_edge": False,
        "status": "source_to_target",
        "relationship_status": "directed",
        "preserved": True,
    }
    assert graph_payload["serialization"]["direct_relationship_preserved"] is True
    assert graph_payload["serialization"]["neighborhood_edge_records_serialized"] == 0
    assert graph_payload["serialization"]["neighborhood_edge_records_omitted"] == 200
    assert len(graph_payload["peer_comparison"]["top_shared_peers"]) <= 12
    assert approx_tokens(composer.last_parts["graph"]) <= 2200
    assert graph.context["model_context_token_cap"] == 2200
    assert graph.context["direct_relationship_preserved"] is True
    assert composer.last_parts["asset_profile"]
    assert composer.last_parts["detection"]
    assert composer.last_parts["knowledge"]


@pytest.mark.parametrize(
    ("direct", "expected"),
    [
        (
            {"a_to_b": True, "b_to_a": False, "relationship_status": "forward_direct_relationship"},
            {
                "source": IP_A,
                "target": IP_B,
                "forward_edge": True,
                "reverse_edge": False,
                "status": "source_to_target",
                "relationship_status": "forward_direct_relationship",
                "preserved": True,
            },
        ),
        (
            {"a_to_b": False, "b_to_a": False, "relationship_status": "no_direct_relationship"},
            {
                "source": IP_A,
                "target": IP_B,
                "forward_edge": False,
                "reverse_edge": False,
                "status": "no_direct_relationship",
                "relationship_status": "no_direct_relationship",
                "preserved": True,
            },
        ),
        (
            {"a_to_b": True, "b_to_a": True, "relationship_status": "bidirectional_direct_relationship"},
            {
                "source": IP_A,
                "target": IP_B,
                "forward_edge": True,
                "reverse_edge": True,
                "status": "bidirectional",
                "relationship_status": "bidirectional_direct_relationship",
                "preserved": True,
            },
        ),
    ],
)
def test_graph_comparison_context_preserves_required_direct_relationship_fact(direct, expected):
    graph = GraphProviderResult(
        provider="graph",
        status="available",
        context={
            "target_ips": [IP_A, IP_B],
            "scope": "multi_entity_comparison",
            "requested_scope": "multi_entity_comparison",
            "source_capability": "graph.compare_assets",
            "entity_a": {"ip": IP_A, "present": True, "peers_retrieved": []},
            "entity_b": {"ip": IP_B, "present": True, "peers_retrieved": []},
            "direct_relationship": direct,
            "nodes": [{"id": IP_A}, {"id": IP_B}],
            "edges": [],
            "retrieval_complete": True,
            "requested_scope_complete": True,
        },
        provenance=ProviderProvenance(source="fixture_graph", status="available"),
    )
    composer = ContextComposer(_settings())

    composer.compose(CopilotContextPackage(entities=EntityResolver().resolve(f"{IP_A} {IP_B}"), graph=graph))
    graph_payload = json.loads(composer.last_parts["graph"].split("\n", 1)[1].rsplit("\n", 1)[0])

    assert graph_payload["direct_relationship"] == expected
    assert graph_payload["serialization"]["direct_relationship_preserved"] is True
    assert graph.context["direct_relationship_preserved"] is True


def test_graph_comparison_budget_keeps_direct_fact_when_optional_details_are_omitted():
    peers = [f"203.0.113.{index}" for index in range(1, 250)]
    graph = GraphProviderResult(
        provider="graph",
        status="available",
        context={
            "target_ips": [IP_A, IP_B],
            "scope": "multi_entity_comparison",
            "requested_scope": "multi_entity_comparison",
            "source_capability": "graph.compare_assets",
            "entity_a": {"ip": IP_A, "present": True, "peers_retrieved": peers},
            "entity_b": {"ip": IP_B, "present": True, "peers_retrieved": peers},
            "direct_relationship": {"a_to_b": True, "b_to_a": False},
            "shared_peers_retrieved": peers,
            "shared_peer_total": len(peers),
            "nodes": [{"id": IP_A}, {"id": IP_B}, *({"id": peer} for peer in peers)],
            "edges": [{"source": IP_A, "target": peer} for peer in peers],
            "retrieval_complete": True,
            "requested_scope_complete": True,
        },
        provenance=ProviderProvenance(source="fixture_graph", status="available"),
    )
    composer = ContextComposer(_settings())

    composer.compose(
        CopilotContextPackage(entities=EntityResolver().resolve(f"{IP_A} {IP_B}"), graph=graph),
        base_input_tokens=0,
    )
    graph_payload = json.loads(composer.last_parts["graph"].split("\n", 1)[1].rsplit("\n", 1)[0])

    assert graph_payload["direct_relationship"]["forward_edge"] is True
    assert graph_payload["direct_relationship"]["preserved"] is True
    assert graph_payload["serialization"]["direct_relationship_preserved"] is True
    assert approx_tokens(composer.last_parts["graph"]) <= 2200


def test_system_prompt_does_not_force_internal_knowledge_base_label():
    prompt = Path("app/prompts/system_prompt.md").read_text(encoding="utf-8")

    assert "From Soorin Knowledge Base:" not in prompt
    assert "attribute it naturally" in prompt
    assert "internal provider, tool, context, storage, routing, or prompt names" in prompt


def test_detection_evidence_view_removes_missing_anomaly_provider_limitation():
    limitation = (
        "No dedicated anomaly provider evidence is available; only bounded graph structural analysis is supplied."
    )
    task = replace(
        _task(capability="asset.get_detection"),
        required_capabilities=("asset.get_detection", "graph.get_summary"),
    )
    detection = ToolResult(
        status="ok",
        entities=(IP_A,),
        source_capability="asset.get_detection",
        retrieved_at="now",
        freshness="current",
        completeness="complete",
        selected_views=("evidence",),
        context_included=True,
        context_representation="projected",
        source_payload_complete=True,
        projection_usable=True,
        usable_fact_count=2,
    )
    graph = replace(_graph_tool("graph.get_summary", (IP_A,), "node_summary"), limitations=(limitation,))
    graph = replace(
        graph,
        context_included=True,
        context_representation="node_summary",
        context_token_estimate=200,
        context_token_cap=700,
    )

    decision = EvidenceReviewer().review(task, [detection, graph])
    pack = EvidenceReviewer().build_pack(task, [detection, graph])

    assert limitation not in decision.limitations
    assert limitation not in pack.limitations


def test_planner_schema_diagnostics_identify_field_and_valid_purpose_uses_one_call():
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            name="asset.get_detection",
            version="1.0",
            input_schema=EntityInput,
            output_schema=ToolResult,
            required_entity_cardinality=(1, 1),
            read_only=True,
            timeout_seconds=1,
            retry_policy=RetryPolicy(),
            planner_visible=True,
            allowed_arguments=("entities", "views", "detail", "max_context_tokens", "purpose"),
        ),
        lambda payload: None,
    )
    current = replace(
        _task(capability="asset.get_detection"),
        workflow_mode="multi_step",
    )

    invalid = ExecutionPlan(
        current,
        (
            PlanStep(
                "detect",
                "asset.get_detection",
                arguments={"entities": [IP_A], "max_context_tokens": 1},
            ),
        ),
        source="llm",
    )
    with pytest.raises(PlanValidationError) as captured:
        PlanValidator(registry).validate(invalid)
    assert captured.value.step_id == "detect"
    assert captured.value.capability == "asset.get_detection"
    assert captured.value.field == "max_context_tokens"
    assert captured.value.validation_rule == "greater_than_equal"

    unsupported = replace(
        invalid,
        steps=(
            replace(
                invalid.steps[0],
                arguments={"entities": [IP_A], "raw_url": "not-allowed"},
            ),
        ),
    )
    with pytest.raises(PlanValidationError) as unsupported_error:
        PlanValidator(registry).validate(unsupported)
    assert unsupported_error.value.code == "unsupported_argument"
    assert unsupported_error.value.field == "raw_url"
    assert unsupported_error.value.validation_rule == "capability_argument_allowlist"

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "goal": "inspect",
                        "target_entities": [IP_A],
                        "steps": [{
                            "step_id": "detect",
                            "capability": "asset.get_detection",
                            "arguments": {"entities": [IP_A], "purpose": "Investigate identity conflicts"},
                            "depends_on": [],
                            "required": True,
                            "expected_evidence": "operational_product",
                        }],
                        "stop_condition": "required_evidence_collected",
                    }
                )
            )

    llm = FakeLLM()
    proposed = BoundedPlanner(llm).plan(current, registry.list(), request_id="planner-field")
    validated = PlanValidator(registry).validate(proposed)
    assert llm.calls == 1
    assert validated.steps[0].arguments["purpose"] == "investigate_identity_conflicts"


def test_token_window_budget_subtracts_configured_safety_margin():
    budget = TokenEstimator.window_budget(6000, 1500, 1000, 8000)
    assert budget.remaining_before_safety == 500
    assert budget.remaining_usable_tokens == -500
    assert not budget.fits


def test_unsafe_final_window_blocks_chat_provider_after_recomposition():
    route_json = json.dumps(
        {
            "intent": "general_knowledge",
            "scope": "none",
            "direction": "none",
            "depth": 0,
            "requires_graph": False,
            "requires_detection": False,
            "requires_asset_profile": False,
            "requires_knowledge": False,
            "entity_binding": "none",
            "requires_multiple_entities": False,
            "is_followup": False,
            "reason": "general fixture",
        }
    )

    class FakeLLM:
        def __init__(self):
            self.purposes = []

        def chat(self, messages, **kwargs):
            purpose = kwargs.get("purpose")
            self.purposes.append(purpose)
            if purpose != "intent_router":
                raise AssertionError("Final chat provider must be blocked by the token guard.")
            return LLMProviderResult(
                text=route_json,
                provider="fake",
                model="fake-router",
                deployment="glm",
            )

    llm = FakeLLM()
    service = CopilotService(
        _settings(
            chat_store_history=False,
            llm_usage_reporting_enabled=False,
            llm_context_window_tokens=160,
            llm_context_safety_margin_tokens=100,
            llm_reserved_output_tokens=120,
        ),
        llm,
        MemoryStore(0),
    )
    response = service.chat(
        "Explain the security implications of a deliberately verbose conceptual fixture question.",
        session_id="token-guard",
        request_id="token-guard",
    )

    assert response["provider"] == "deterministic"
    assert response["model"] == "token-window-guard"
    assert llm.purposes == ["intent_router"]
