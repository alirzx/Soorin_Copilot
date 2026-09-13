"""Phase 4B.1 semantic Asset-set routing contract tests."""

from __future__ import annotations

from dataclasses import replace
import json
import math
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.config.settings import get_settings
from src.core.agent.contracts import TaskSpec
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.agent.structured_evidence import structured_query_identity
from src.core.context.intent import build_routing_context
from src.core.context.models import EntityResolution, ResolvedEntity
from src.core.context.structured_routing import (
    SemanticIntentRouter,
    normalize_intent_route,
    validate_structured_router_payload,
)
from src.core.graph.structured import StructuredQuerySpec
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.retrieval import LongTermMemorySelection
from src.core.memory.structured_query import StructuredAssetRef, StructuredQueryContext
from src.core.llm.providers.base import LLMProviderResult


def _empty_entities() -> EntityResolution:
    return EntityResolution(status="none", entity_mode="none")


def _base_payload(intent: str, structured_query: dict[str, object] | None) -> dict[str, object]:
    return {
        "intent": intent,
        "scope": "none",
        "direction": "none",
        "depth": 0,
        "requires_graph": True,
        "requires_detection": False,
        "requires_asset_profile": False,
        "requires_knowledge": False,
        "structured_query": structured_query,
        "entity_binding": "none",
        "requires_multiple_entities": False,
        "is_followup": False,
        "classification_confidence": 0.98,
        "reason": "Structured organizational Asset query.",
    }


def _continuity(*, refs: int = 3, mode: str = "search") -> StructuredQueryContext:
    query = StructuredQuerySpec.model_validate(
        {"mode": "search", "filters": {"role": "Domain Controller"}}
        if mode == "search"
        else {
            "mode": "aggregate",
            "filters": {"role": "Domain Controller"},
            "operation": "count",
        }
    )
    return StructuredQueryContext.create(
        query_identity=structured_query_identity(query, active_graph_version="graph-v1"),
        active_graph_version="graph-v1",
        query=query,
        matched_total=refs if mode == "search" else None,
        returned_count=refs if mode == "search" else None,
        count=12 if mode == "aggregate" else None,
        result_refs=tuple(
            StructuredAssetRef(ip=f"192.0.2.{index}", graph_key=f"asset-{index}")
            for index in range(1, refs + 1)
        ) if mode == "search" else (),
        source_request_id="request-1",
        retrieved_at="2026-09-11T00:00:00+00:00",
        created_at="2026-09-11T00:00:01+00:00",
    )


def _entity_payload(*, pair: bool = False) -> dict[str, object]:
    return {
        "intent": "graph_relationships" if pair else "asset_investigation",
        "scope": "multi_entity_comparison" if pair else "node_summary",
        "direction": "both",
        "depth": 1 if pair else 0,
        "requires_graph": True,
        "requires_detection": False,
        "requires_asset_profile": not pair,
        "requires_knowledge": False,
        "structured_query": None,
        "structured_result_reference": {
            "kind": "select_entities",
            "ordinals": [1, 2] if pair else [1],
        },
        "entity_binding": "none",
        "requires_multiple_entities": pair,
        "is_followup": True,
        "classification_confidence": 0.98,
        "reason": "Use the selected ordered result refs.",
    }


def test_structured_query_spec_reuses_allowlisted_phase4a_contract() -> None:
    spec = StructuredQuerySpec.model_validate(
        {
            "mode": "search",
            "filters": {
                "status": "CONFIRMED",
                "role": "Domain Controller",
                "model_confidence_max": 0.7,
            },
            "sort": "model_confidence",
            "direction": "asc",
            "limit": 25,
        }
    )
    request = spec.to_search_request()
    assert request.filters.role == "Domain Controller"
    assert request.filters.status == "CONFIRMED"
    assert request.filters.model_confidence_max == 0.7
    assert request.limit == 25

    with pytest.raises(ValidationError):
        StructuredQuerySpec.model_validate(
            {
                "mode": "search",
                "filters": {"role__contains": "Domain"},
            }
        )
    with pytest.raises(ValidationError):
        StructuredQuerySpec.model_validate(
            {
                "mode": "search",
                "filters": {"role": "Domain Controller"},
                "cypher": "MATCH (n) RETURN n",
            }
        )


def test_search_route_needs_no_focal_entity_and_keeps_selectors_out_of_entities() -> None:
    payload = _base_payload(
        "asset_search",
        {
            "mode": "search",
            "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
            "sort": "asset_name",
            "direction": "asc",
        },
    )
    decision = validate_structured_router_payload(
        payload,
        _empty_entities(),
        min_confidence=0.5,
        routing_state=SessionRoutingState(active_ip="10.0.0.9"),
        ui_context={"selected_ip": "10.0.0.8"},
    )
    assert decision.intent == "asset_search"
    assert decision.structured_query is not None
    assert decision.entity_binding == "none"
    assert decision.materialized_entities == ()
    route = normalize_intent_route(decision, _empty_entities())
    assert route.use_graph is True
    assert route.structured_query == decision.structured_query
    assert route.materialized_entities == ()
    assert route.target_entities == []


def test_aggregate_route_is_typed_and_mode_mismatch_fails_closed() -> None:
    payload = _base_payload(
        "asset_aggregate",
        {
            "mode": "aggregate",
            "filters": {"role": "Domain Controller"},
            "operation": "count",
        },
    )
    decision = validate_structured_router_payload(
        payload,
        _empty_entities(),
        min_confidence=0.5,
    )
    assert decision.structured_query is not None
    aggregate = decision.structured_query.to_aggregate_request()
    assert aggregate.operation.value == "count"
    assert aggregate.filters.role == "Domain Controller"

    invalid = dict(payload)
    invalid["structured_query"] = {
        "mode": "search",
        "filters": {"role": "Domain Controller"},
    }
    with pytest.raises(ValueError, match="structured_query_mode"):
        validate_structured_router_payload(invalid, _empty_entities(), min_confidence=0.5)


def test_structured_route_invariants_fail_closed() -> None:
    payload = _base_payload(
        "asset_search",
        {"mode": "search", "filters": {"vendor": "VMware"}},
    )
    bad_binding = dict(payload, entity_binding="active_single")
    with pytest.raises(ValueError, match="entity_binding"):
        validate_structured_router_payload(bad_binding, _empty_entities(), min_confidence=0.5)

    bad_deepen = dict(payload, requires_asset_profile=True)
    with pytest.raises(ValueError, match="cannot_deepen"):
        validate_structured_router_payload(bad_deepen, _empty_entities(), min_confidence=0.5)

    bad_graph = dict(payload, requires_graph=False)
    with pytest.raises(ValueError, match="requires_graph"):
        validate_structured_router_payload(bad_graph, _empty_entities(), min_confidence=0.5)


def test_non_set_routes_keep_legacy_validator_contract() -> None:
    payload = {
        "intent": "general_knowledge",
        "scope": "none",
        "direction": "none",
        "depth": 0,
        "requires_graph": False,
        "requires_detection": False,
        "requires_asset_profile": False,
        "requires_knowledge": True,
        "structured_query": None,
        "entity_binding": "none",
        "requires_multiple_entities": False,
        "is_followup": False,
        "classification_confidence": 0.9,
        "reason": "Reference knowledge request.",
    }
    decision = validate_structured_router_payload(
        payload,
        _empty_entities(),
        min_confidence=0.5,
    )
    assert decision.intent == "general_knowledge"
    assert decision.structured_query is None

    polluted = dict(payload)
    polluted["structured_query"] = {
        "mode": "search",
        "filters": {"status": "CONFIRMED"},
    }
    with pytest.raises(ValueError, match="requires_set_intent"):
        validate_structured_router_payload(polluted, _empty_entities(), min_confidence=0.5)


def test_task_spec_contract_can_carry_structured_query_without_expanding_entities() -> None:
    query = StructuredQuerySpec.model_validate(
        {"mode": "search", "filters": {"role": "Domain Controller"}}
    )
    task = TaskSpec(
        request="List Domain Controllers",
        intent="asset_search",
        scope="none",
        direction="none",
        entities=(),
        required_capabilities=(),
        structured_query=query,
    )
    assert task.entities == ()
    assert task.structured_query == query


def test_router_prompt_contains_only_bounded_structured_contract() -> None:
    settings = get_settings()
    prompt_path = settings.intent_router_system_prompt_path
    text = open(prompt_path, encoding="utf-8").read()
    assert "asset_search" in text
    assert "asset_aggregate" in text
    assert "structured_query" in text
    assert "Properties are selectors, not entities" in text
    assert "Never emit Cypher" in text
    assert "structured_result_reference" in text
    assert "Investigation-timeline ordinals" in text


def test_router_input_exposes_only_bounded_structured_continuity_summary() -> None:
    state = SessionRoutingState(structured_query_context=_continuity(refs=3))

    payload = build_routing_context("use the earlier matches", _empty_entities(), state)

    summary = payload["latest_structured_context"]
    assert summary["available"] is True
    assert summary["bounded_ref_count"] == 3
    assert [item["ip"] for item in summary["ordered_refs"]] == [
        "192.0.2.1", "192.0.2.2", "192.0.2.3"
    ]
    assert "rows" not in summary
    assert "classification_summary" not in json.dumps(summary)


@pytest.mark.parametrize(
    "message",
    ["analyze the first one", "inspect the top match", "tell me about the leading result"],
)
def test_typed_first_result_selection_is_phrase_agnostic_and_outranks_ui_active_pair(
    message: str,
) -> None:
    state = SessionRoutingState(
        active_entities=("198.51.100.10", "198.51.100.11"),
        structured_query_context=_continuity(),
    )
    incidental_pair = EntityResolution(
        status="resolved",
        entities=[
            ResolvedEntity(type="ip", value="198.51.100.10", source="conversation"),
            ResolvedEntity(type="ip", value="198.51.100.11", source="conversation"),
        ],
        entity_mode="multiple",
        valid_entity_count=2,
        reference_detected=True,
        reference_type="active_pair",
    )

    decision = validate_structured_router_payload(
        _entity_payload(),
        incidental_pair,
        min_confidence=0.5,
        message=message,
        routing_state=state,
        ui_context={"selected_ip": "203.0.113.99"},
    )
    route = normalize_intent_route(decision, EntityResolution(
        status="resolved",
        entities=[ResolvedEntity(type="ip", value="192.0.2.1", source="conversation")],
        primary_entity=ResolvedEntity(type="ip", value="192.0.2.1", source="conversation"),
        entity_mode="single",
        valid_entity_count=1,
    ))

    assert decision.materialized_entities == ("192.0.2.1",)
    assert decision.structured_result_reference.kind == "select_entities"
    assert route.materialized_entities == ("192.0.2.1",)


def test_typed_first_two_selection_materializes_exactly_two_ordered_refs() -> None:
    state = SessionRoutingState(structured_query_context=_continuity())

    decision = validate_structured_router_payload(
        _entity_payload(pair=True),
        _empty_entities(),
        min_confidence=0.5,
        routing_state=state,
    )

    assert decision.materialized_entities == ("192.0.2.1", "192.0.2.2")
    assert decision.requires_multiple_entities


def test_explicit_message_entity_overrides_structured_selection() -> None:
    explicit = EntityResolution(
        status="resolved",
        entities=[ResolvedEntity(type="ip", value="203.0.113.4", source="message")],
        primary_entity=ResolvedEntity(type="ip", value="203.0.113.4", source="message"),
        entity_mode="single",
        explicit_candidate_count=1,
        valid_entity_count=1,
    )
    payload = _entity_payload()
    payload["entity_binding"] = "explicit"

    decision = validate_structured_router_payload(
        payload,
        explicit,
        min_confidence=0.5,
        routing_state=SessionRoutingState(structured_query_context=_continuity()),
    )

    assert decision.materialized_entities == ("203.0.113.4",)
    assert decision.structured_result_reference.kind == "none"


def test_set_continuation_accepts_full_typed_query_and_ignores_incidental_ui() -> None:
    payload = _base_payload(
        "asset_aggregate",
        {
            "mode": "aggregate",
            "filters": {"role": "Domain Controller"},
            "operation": "count",
        },
    )
    payload["structured_result_reference"] = {"kind": "set_query", "ordinals": []}
    payload["is_followup"] = True

    decision = validate_structured_router_payload(
        payload,
        _empty_entities(),
        min_confidence=0.5,
        routing_state=SessionRoutingState(structured_query_context=_continuity()),
        ui_context={"selected_ip": "203.0.113.99"},
    )

    assert decision.structured_query is not None
    assert decision.structured_query.filters.role == "Domain Controller"
    assert decision.materialized_entities == ()
    assert decision.structured_result_reference.kind == "set_query"


@pytest.mark.parametrize(
    ("intent", "query", "expected"),
    [
        (
            "asset_aggregate",
            {
                "mode": "aggregate",
                "filters": {"role": "Domain Controller"},
                "operation": "count",
            },
            ("count", None, None, None),
        ),
        (
            "asset_search",
            {
                "mode": "search",
                "filters": {"role": "Domain Controller"},
                "sort": "model_confidence",
                "direction": "asc",
            },
            (None, None, "model_confidence", "asc"),
        ),
        (
            "asset_aggregate",
            {
                "mode": "aggregate",
                "filters": {"role": "Domain Controller"},
                "operation": "group_count",
                "group_by": "status",
            },
            ("group_count", "status", None, None),
        ),
    ],
)
def test_typed_set_followups_keep_prior_filters_for_count_sort_and_grouping(
    intent: str,
    query: dict[str, object],
    expected: tuple[str | None, str | None, str | None, str | None],
) -> None:
    payload = _base_payload(intent, query)
    payload["structured_result_reference"] = {"kind": "set_query", "ordinals": []}
    payload["is_followup"] = True

    decision = validate_structured_router_payload(
        payload,
        _empty_entities(),
        min_confidence=0.5,
        routing_state=SessionRoutingState(structured_query_context=_continuity()),
    )

    assert decision.structured_query is not None
    assert decision.structured_query.filters.role == "Domain Controller"
    assert (
        decision.structured_query.operation.value if decision.structured_query.operation else None,
        decision.structured_query.group_by.value if decision.structured_query.group_by else None,
        decision.structured_query.sort.value if decision.structured_query.sort else None,
        decision.structured_query.direction.value if decision.structured_query.direction else None,
    ) == expected


def test_empty_out_of_bounds_and_aggregate_selection_fail_validation() -> None:
    for context, pattern in (
        (_continuity(refs=0), "out_of_bounds"),
        (_continuity(refs=1), "out_of_bounds"),
        (_continuity(mode="aggregate"), "requires_search_context"),
    ):
        payload = _entity_payload()
        payload["structured_result_reference"] = {
            "kind": "select_entities",
            "ordinals": [2],
        }
        with pytest.raises(ValueError, match=pattern):
            validate_structured_router_payload(
                payload,
                _empty_entities(),
                min_confidence=0.5,
                routing_state=SessionRoutingState(structured_query_context=context),
            )


def test_vague_active_single_remains_in_existing_namespace_when_context_exists() -> None:
    active = ResolvedEntity(type="ip", value="198.51.100.10", source="conversation")
    entities = EntityResolution(
        status="resolved",
        entities=[active],
        primary_entity=active,
        entity_mode="single",
        candidate_count=1,
        valid_entity_count=1,
        reference_detected=True,
        reference_type="active_single",
    )
    payload = {
        "intent": "asset_investigation",
        "scope": "node_summary",
        "direction": "both",
        "depth": 0,
        "requires_graph": True,
        "requires_detection": False,
        "requires_asset_profile": True,
        "requires_knowledge": False,
        "structured_query": None,
        "entity_binding": "active_single",
        "requires_multiple_entities": False,
        "is_followup": True,
        "classification_confidence": 0.98,
        "reason": "Continue with the active focal Asset.",
    }

    decision = validate_structured_router_payload(
        payload,
        entities,
        min_confidence=0.5,
        routing_state=SessionRoutingState(
            active_entities=(active.value,),
            structured_query_context=_continuity(),
        ),
    )

    assert decision.materialized_entities == (active.value,)
    assert decision.structured_result_reference.kind == "none"


def test_explicit_pair_outranks_structured_result_selection() -> None:
    explicit = EntityResolution(
        status="resolved",
        entities=[
            ResolvedEntity(type="ip", value="203.0.113.4", source="message"),
            ResolvedEntity(type="ip", value="203.0.113.5", source="message"),
        ],
        entity_mode="multiple",
        candidate_count=2,
        explicit_candidate_count=2,
        valid_entity_count=2,
    )
    payload = _entity_payload(pair=True)
    payload["entity_binding"] = "explicit"

    decision = validate_structured_router_payload(
        payload,
        explicit,
        min_confidence=0.5,
        routing_state=SessionRoutingState(structured_query_context=_continuity()),
    )

    assert decision.materialized_entities == ("203.0.113.4", "203.0.113.5")
    assert decision.structured_result_reference.kind == "none"


class _RepairLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses

    def chat(self, *_args, **_kwargs) -> LLMProviderResult:
        return LLMProviderResult(
            text=self.responses.pop(0),
            provider="fake",
            model="fake",
            finish_reason="stop",
            usage={},
            status_code=200,
        )


def test_router_repair_path_preserves_typed_result_selection() -> None:
    repaired = _entity_payload(pair=True)
    settings = replace(
        get_settings(),
        intent_router_enabled=True,
        intent_router_retry_enabled=True,
        intent_router_min_confidence=0.5,
    )
    router = SemanticIntentRouter(
        settings,
        _RepairLLM(["not json", json.dumps(repaired)]),
    )

    decision = router.classify(
        "compare the first two matches",
        _empty_entities(),
        SessionRoutingState(structured_query_context=_continuity()),
        request_id="repair-selection",
    )

    assert decision.decision_source == "semantic_router_repair"
    assert decision.materialized_entities == ("192.0.2.1", "192.0.2.2")
    assert decision.structured_result_reference.kind == "select_entities"


@pytest.mark.parametrize(
    ("message", "raw_score", "expected"),
    [
        ("List assets with model confidence above 90", 90, math.nextafter(0.9, math.inf)),
        ("List assets with model confidence above 90%", 90, math.nextafter(0.9, math.inf)),
        ("List assets with model confidence above 0.90", 0.9, math.nextafter(0.9, math.inf)),
    ],
)
def test_semantic_router_normalizes_natural_score_before_typed_validation(
    message: str,
    raw_score: float,
    expected: float,
) -> None:
    payload = _base_payload(
        "asset_search",
        {
            "mode": "search",
            "filters": {"model_confidence_min": raw_score},
        },
    )
    payload["structured_result_reference"] = {"kind": "none", "ordinals": []}
    settings = replace(
        get_settings(),
        intent_router_enabled=True,
        intent_router_retry_enabled=False,
        intent_router_min_confidence=0.5,
    )
    router = SemanticIntentRouter(settings, _RepairLLM([json.dumps(payload)]))

    decision = router.classify(
        message,
        _empty_entities(),
        SessionRoutingState(),
        request_id="natural-score",
    )

    assert decision.structured_query is not None
    assert decision.structured_query.filters.model_confidence_min == expected


def test_semantic_router_scopes_percent_units_to_the_matching_score_field() -> None:
    payload = _base_payload(
        "asset_search",
        {
            "mode": "search",
            "filters": {
                "model_confidence_min": 0.9,
                "mapping_confidence_min": 90,
            },
        },
    )
    payload["structured_result_reference"] = {"kind": "none", "ordinals": []}
    settings = replace(
        get_settings(),
        intent_router_enabled=True,
        intent_router_retry_enabled=False,
        intent_router_min_confidence=0.5,
    )
    router = SemanticIntentRouter(settings, _RepairLLM([json.dumps(payload)]))

    decision = router.classify(
        "List assets with model confidence at least 0.9 and mapping confidence at least 90%.",
        _empty_entities(),
        SessionRoutingState(),
        request_id="mixed-score-units",
    )

    assert decision.structured_query is not None
    assert decision.structured_query.filters.model_confidence_min == 0.9
    assert decision.structured_query.filters.mapping_confidence_min == 0.9


def test_empty_result_selection_repair_failure_routes_to_clarification_not_active_or_ui() -> None:
    invalid = _entity_payload()
    settings = replace(
        get_settings(),
        intent_router_enabled=True,
        intent_router_retry_enabled=True,
        intent_router_min_confidence=0.5,
    )
    router = SemanticIntentRouter(
        settings,
        _RepairLLM([json.dumps(invalid), json.dumps(invalid)]),
    )
    active = ResolvedEntity(type="ip", value="198.51.100.10", source="conversation")
    entities = EntityResolution(
        status="resolved",
        entities=[active],
        primary_entity=active,
        entity_mode="single",
        candidate_count=1,
        valid_entity_count=1,
        reference_detected=True,
        reference_type="active_single",
    )
    routing_state = SessionRoutingState(
        active_entities=(active.value,),
        structured_query_context=_continuity(refs=0),
    )
    service = SimpleNamespace(settings=settings, intent_router=router)

    routed = CopilotWorkflowNodes(service).route({
        "message": "inspect the selected retained result",
        "resolved_entities": entities,
        "active_entity_state": routing_state,
        "recent_messages": [],
        "ui_context": {"selected_ip": "203.0.113.99"},
        "request_id": "empty-result-selection",
        "trace_id": "trace-empty-result-selection",
    })

    assert routed["workflow_status"] == "clarification_required"
    assert routed["clarification"]["code"] == "structured_result_reference_unavailable"
    assert "routing_result" not in routed


def test_route_node_recomputes_pair_authority_for_structured_result_selection() -> None:
    continuity = _continuity()
    routing_state = SessionRoutingState(
        active_entities=("198.51.100.10", "198.51.100.11"),
        previous_scope="multi_entity_comparison",
        structured_query_context=continuity,
    )
    old_pair = EntityResolution(
        status="resolved",
        entities=[
            ResolvedEntity(type="ip", value="198.51.100.10", source="conversation"),
            ResolvedEntity(type="ip", value="198.51.100.11", source="conversation"),
        ],
        entity_mode="multiple",
        valid_entity_count=2,
        reference_detected=True,
        reference_type="active_pair",
    )
    decision = validate_structured_router_payload(
        _entity_payload(pair=True),
        old_pair,
        min_confidence=0.5,
        routing_state=routing_state,
    )
    service = SimpleNamespace(
        settings=get_settings(),
        intent_router=SimpleNamespace(classify=lambda *_args, **_kwargs: decision),
    )

    routed = CopilotWorkflowNodes(service).route({
        "message": "compare the first two matches",
        "resolved_entities": old_pair,
        "active_entity_state": routing_state,
        "recent_messages": [],
        "request_id": "route-result-pair",
        "trace_id": "trace-result-pair",
    })

    route = routed["routing_result"]
    assert route.materialized_entities == ("192.0.2.1", "192.0.2.2")
    assert routed["task_envelope"].ordered_entities == ("192.0.2.1", "192.0.2.2")
    assert route.scope == "multi_entity_comparison"


def test_route_node_set_continuation_discards_incidental_pair_envelope() -> None:
    routing_state = SessionRoutingState(
        active_entities=("198.51.100.10", "198.51.100.11"),
        previous_scope="multi_entity_comparison",
        structured_query_context=_continuity(),
    )
    old_pair = EntityResolution(
        status="resolved",
        entities=[
            ResolvedEntity(type="ip", value="198.51.100.10", source="conversation"),
            ResolvedEntity(type="ip", value="198.51.100.11", source="conversation"),
        ],
        entity_mode="multiple",
        valid_entity_count=2,
        reference_detected=True,
        reference_type="compare_with_reference",
    )
    payload = _base_payload(
        "asset_aggregate",
        {
            "mode": "aggregate",
            "filters": {"role": "Domain Controller"},
            "operation": "count",
        },
    )
    payload["structured_result_reference"] = {"kind": "set_query", "ordinals": []}
    payload["is_followup"] = True
    decision = validate_structured_router_payload(
        payload,
        old_pair,
        min_confidence=0.5,
        routing_state=routing_state,
    )
    service = SimpleNamespace(
        settings=get_settings(),
        intent_router=SimpleNamespace(classify=lambda *_args, **_kwargs: decision),
    )
    entity_scoped_memory = SimpleNamespace(
        memory=SimpleNamespace(entity_ids=("198.51.100.10",)),
        estimated_tokens=12,
    )

    routed = CopilotWorkflowNodes(service).route({
        "message": "how many of those are there?",
        "resolved_entities": old_pair,
        "active_entity_state": routing_state,
        "recent_messages": [],
        "long_term_memory_selection": LongTermMemorySelection(
            status="ok",
            memories=(entity_scoped_memory,),
            selected_count=1,
            estimated_tokens=12,
        ),
        "request_id": "route-result-count",
        "trace_id": "trace-result-count",
    })

    route = routed["routing_result"]
    assert route.intent == "asset_aggregate"
    assert route.materialized_entities == ()
    assert routed["task_envelope"].ordered_entities == ()
    assert routed["task_envelope"].comparison_required is False
    assert routed["long_term_memory_selection"].memories == ()
    assert routed["long_term_memory_selection"].selected_count == 0
    assert "entity_scoped_ltm_excluded_for_global_query" in (
        routed["long_term_memory_selection"].limitations
    )


def test_historical_result_recall_is_no_live_and_does_not_bind_active_or_ui_entities() -> None:
    routing_state = SessionRoutingState(
        active_entities=("198.51.100.10", "198.51.100.11"),
        structured_query_context=_continuity(),
    )
    payload = {
        "intent": "general_knowledge",
        "scope": "none",
        "direction": "none",
        "depth": 0,
        "requires_graph": False,
        "requires_detection": False,
        "requires_asset_profile": False,
        "requires_knowledge": False,
        "structured_query": None,
        "structured_result_reference": {"kind": "historical_recall", "ordinals": []},
        "entity_binding": "none",
        "requires_multiple_entities": False,
        "is_followup": True,
        "classification_confidence": 0.98,
        "reason": "Recall the bounded prior structured result.",
    }
    decision = validate_structured_router_payload(
        payload,
        _empty_entities(),
        min_confidence=0.5,
        routing_state=routing_state,
        ui_context={"selected_ip": "203.0.113.99"},
    )
    service = SimpleNamespace(
        settings=get_settings(),
        intent_router=SimpleNamespace(classify=lambda *_args, **_kwargs: decision),
    )

    routed = CopilotWorkflowNodes(service).route({
        "message": "what did that last search return?",
        "resolved_entities": _empty_entities(),
        "active_entity_state": routing_state,
        "recent_messages": [],
        "ui_context": {"selected_ip": "203.0.113.99"},
        "request_id": "route-result-history",
        "trace_id": "trace-result-history",
    })

    assert routed["routing_result"].materialized_entities == ()
    assert routed["request_constraints"].allow_live is False
    assert routed["request_constraints"].memory_only is True
    assert routed["turn_policy"].operation == "memory_recall"
    assert routed["turn_policy"].operational_state_mutation_allowed is False
