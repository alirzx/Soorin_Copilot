"""Phase 4B.1 semantic Asset-set routing contract tests."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from src.config.settings import get_settings
from src.core.agent.contracts import TaskSpec
from src.core.context.models import EntityResolution
from src.core.context.structured_routing import (
    normalize_intent_route,
    validate_structured_router_payload,
)
from src.core.graph.structured import StructuredQuerySpec
from src.core.memory.routing_state import SessionRoutingState


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
