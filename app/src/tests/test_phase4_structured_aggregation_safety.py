"""Regression coverage for structured aggregation breadth and route safety."""

from __future__ import annotations

from src.core.agent.workflow import BoundedCopilotWorkflow
from src.core.context.structured_hardening import deterministic_structured_fallback
from src.core.graph.structured import (
    AssetAggregateGroup,
    AssetAggregateOperation,
    AssetGroupField,
    StructuredQueryMode,
)


PRODUCT_GROUP_FIELDS = {
    "ip",
    "asset_name",
    "status",
    "suggested_type",
    "model_confidence",
    "mapping_confidence",
    "unknown_score",
    "classification_summary",
    "vendor",
    "product",
    "role",
    "roles",
    "tag",
    "sub_tag",
    "last_detection_at",
}


def test_all_product_exact_search_properties_are_groupable() -> None:
    actual = {field.value for field in AssetGroupField}
    assert PRODUCT_GROUP_FIELDS <= actual
    assert "enrichment_status" in actual


def test_roles_array_group_value_is_stable_and_not_exploded() -> None:
    group = AssetAggregateGroup(
        value=["Linux Server", "SSH Server"],
        group_values={"roles": ["Linux Server", "SSH Server"]},
        count=2,
        member_ips=("192.168.20.4", "192.168.20.5"),
    )
    assert group.value == '["Linux Server","SSH Server"]'
    assert group.group_values["roles"] == '["Linux Server","SSH Server"]'
    assert group.count == 2


def test_regression_prompt_compiles_to_confirmed_classification_summary_grouping() -> None:
    prompt = (
        "find all confirm asset and group them by their classifcation summary, "
        "and show their IP with percentage."
    )
    decision = deterministic_structured_fallback(prompt)
    assert decision is not None
    assert decision.intent == "asset_aggregate"
    assert decision.query.mode is StructuredQueryMode.AGGREGATE
    assert decision.query.operation is AssetAggregateOperation.GROUP_COUNT
    assert decision.query.filters.status == "confirmed"
    assert decision.query.group_by is AssetGroupField.CLASSIFICATION_SUMMARY
    assert decision.query.group_by_fields == (AssetGroupField.CLASSIFICATION_SUMMARY,)


def test_route_clarification_never_reaches_task_validation() -> None:
    state = {
        "workflow_status": "clarification_required",
        "clarification": {"code": "structured_query_clarification_required"},
    }
    assert BoundedCopilotWorkflow._after_route(state) == "clarification"


def test_missing_routing_result_fails_safe_instead_of_keyerror() -> None:
    state = {"workflow_status": "running"}
    assert BoundedCopilotWorkflow._after_route(state) == "safe_failure"


def test_route_is_a_conditional_workflow_transition() -> None:
    workflow = BoundedCopilotWorkflow()
    graph = workflow.graph.get_graph()
    route_edges = [edge for edge in graph.edges if edge.source == "route"]
    assert route_edges
    assert all(edge.conditional for edge in route_edges)
