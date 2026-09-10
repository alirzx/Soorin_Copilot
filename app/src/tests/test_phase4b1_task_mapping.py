"""Structured task-mapping and fail-closed transition tests."""

from __future__ import annotations

import pytest

from src.core.agent.plan_validator import PlanValidationError, PlanValidator
from src.core.agent.registry import CapabilityRegistry
from src.core.agent.task_mapping import compile_direct_plan, enforce_task_envelope, task_spec_from_route
from src.core.agent.contracts import RequestConstraints, TaskEnvelope, TurnPolicy
from src.core.context.models import RouteDecision
from src.core.graph.structured import StructuredQuerySpec


def _route(query: StructuredQuerySpec) -> RouteDecision:
    return RouteDecision(
        use_graph=True,
        reason="structured_asset_set",
        structured_query=query,
        intent="asset_search" if query.mode.value == "search" else "asset_aggregate",
        scope="none",
        direction="none",
        entity_binding="none",
        resolved_entity_binding="none",
        materialized_entities=(),
    )


def test_structured_query_survives_route_to_task_without_consuming_entity_budget() -> None:
    query = StructuredQuerySpec.model_validate(
        {"mode": "search", "filters": {"role": "Domain Controller"}}
    )
    envelope = TaskEnvelope(
        ordered_entities=("10.0.0.9",),
        operation="new_task",
        task_family="asset_investigation",
    )
    task = task_spec_from_route(
        _route(query),
        "List Domain Controllers",
        RequestConstraints(),
        TurnPolicy(operation="new_task", target="ui_entity", target_entities=("10.0.0.9",)),
        envelope,
    )
    assert task.structured_query == query
    assert task.entities == ()
    assert task.required_capabilities == ("graph.search_assets",)
    assert task.workflow_mode == "direct"


def test_structured_aggregate_maps_to_future_bounded_capability() -> None:
    query = StructuredQuerySpec.model_validate(
        {
            "mode": "aggregate",
            "filters": {"status": "CONFIRMED"},
            "operation": "count",
        }
    )
    task = task_spec_from_route(_route(query), "How many confirmed assets?", RequestConstraints())
    assert task.entities == ()
    assert task.required_capabilities == ("graph.aggregate_assets",)


def test_comparison_envelope_remains_authoritative_over_structured_route() -> None:
    query = StructuredQuerySpec.model_validate(
        {"mode": "search", "filters": {"role": "Domain Controller"}}
    )
    envelope = TaskEnvelope(
        ordered_entities=("10.0.0.1", "10.0.0.2"),
        operation="compare_previous_current",
        comparison_required=True,
        task_family="asset_comparison",
    )
    route = enforce_task_envelope(_route(query), envelope)
    assert route.structured_query is None
    assert route.materialized_entities == envelope.ordered_entities
    assert route.intent == "graph_relationships"
    assert route.scope == "multi_entity_comparison"


def test_structured_direct_plan_compiles_typed_arguments_and_unknown_registry_fails_closed() -> None:
    query = StructuredQuerySpec.model_validate(
        {"mode": "search", "filters": {"vendor": "VMware"}}
    )
    task = task_spec_from_route(_route(query), "List VMware assets", RequestConstraints())
    plan = compile_direct_plan(task)
    assert plan.steps[0].capability == "graph.search_assets"
    assert plan.steps[0].arguments == {
        "filters": {"vendor": "VMware"},
        "sort": "graph_key",
        "direction": "asc",
    }

    with pytest.raises(PlanValidationError) as exc:
        PlanValidator(CapabilityRegistry()).validate(plan)
    assert exc.value.code == "unknown_capability"
