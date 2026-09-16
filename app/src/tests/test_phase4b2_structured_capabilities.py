"""Phase 4B.2 bounded structured Graph capability integration tests."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.core.agent.contracts import ExecutionPlan, PlanStep, RequestConstraints, TaskSpec
from src.core.agent.executor import CapabilityExecutor
from src.core.agent.plan_validator import PlanValidationError, PlanValidator
from src.core.agent.planner import BoundedPlanner
from src.core.agent.registry import build_capability_registry
from src.core.agent.specialists import GraphAnalysisSpecialist
from src.core.agent.task_mapping import compile_direct_plan, task_spec_from_route
from src.core.context.models import ResolvedEntity, RouteDecision
from src.core.context.providers.graph import GraphContextProvider
from src.core.graph.structured import (
    AssetAggregateCapabilityInput,
    AssetAggregateGroup,
    AssetAggregateResult,
    AssetSearchCapabilityInput,
    AssetSearchResult,
    StructuredAssetRow,
    StructuredQuerySpec,
    semantic_query_identity,
)


class _MustNotCall:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            product_read_timeout_seconds=1,
            rag_qdrant_timeout_seconds=1,
            rag_top_k=3,
        )
        self.calls = 0

    def fetch(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("Product must not be called for structured Graph retrieval")

    def search(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("Knowledge must not be called for structured Graph retrieval")


class _RecordingGraphService:
    def __init__(self) -> None:
        self.search_requests = []
        self.aggregate_requests = []

    def search_assets(self, request):
        self.search_requests.append(request)
        rows = tuple(
            StructuredAssetRow(
                graph_key=f"asset-{index}",
                graph_version="v-active",
                ip=f"192.0.2.{index}",
                status="CONFIRMED",
                role="Domain Controller",
            )
            for index in range(1, 3)
        )
        return AssetSearchResult(
            active_graph_version="v-active",
            filters=request.filters,
            rows=rows,
            returned_count=len(rows),
            matched_total=50,
            truncated=True,
            next_cursor="internal-cursor",
            sort=request.sort,
            direction=request.direction,
            retrieved_at="2026-09-10T00:00:00Z",
            limitations=("Fixture result is bounded.",),
        )

    def aggregate_assets(self, request):
        self.aggregate_requests.append(request)
        groups = (
            AssetAggregateGroup(value="CONFIRMED", count=7),
            AssetAggregateGroup(value="UNCONFIRMED", count=2),
        ) if request.group_by else ()
        return AssetAggregateResult(
            active_graph_version="v-active",
            filters=request.filters,
            operation=request.operation,
            count=9,
            group_by=request.group_by,
            groups=groups,
            retrieved_at="2026-09-10T00:00:00Z",
        )


def _runtime():
    product = _MustNotCall()
    knowledge = _MustNotCall()
    service = _RecordingGraphService()
    provider = object.__new__(GraphContextProvider)
    provider.settings = SimpleNamespace(
        agent_request_timeout_seconds=2,
        graph_asset_search_max_limit=3,
        graph_full_neighbors_hard_max=50,
    )
    provider.graph_service = service
    registry = build_capability_registry(
        asset_profile_provider=product,
        detection_provider=product,
        graph_provider=provider,
        knowledge_service=knowledge,
    )
    return registry, provider, service, product, knowledge


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


def _task(query: StructuredQuerySpec, request: str = "structured request") -> TaskSpec:
    return task_spec_from_route(_route(query), request, RequestConstraints())


def test_registry_and_planner_catalog_expose_only_safe_zero_entity_contracts() -> None:
    registry, *_ = _runtime()
    names = {spec.name for spec in registry.list()}
    assert {
        "graph.get_summary",
        "graph.get_neighbors",
        "graph.get_relationship",
        "graph.compare_assets",
        "graph.find_path",
        "graph.search_assets",
        "graph.aggregate_assets",
    } <= names

    expected_arguments = {
        "graph.search_assets": ["filters", "sort", "direction", "limit"],
        "graph.aggregate_assets": [
            "filters", "operation", "group_by", "group_by_fields", "limit"
        ],
    }
    for name, allowed in expected_arguments.items():
        spec = registry.get(name)
        assert spec.read_only and spec.planner_visible
        assert spec.required_entity_cardinality == (0, 0)
        assert spec.maximum_graph_depth == 0
        assert set(spec.input_schema.model_json_schema()["properties"]) == {
            *allowed,
            "semantic_query_id",
        }
        entry = BoundedPlanner._catalog_entry(spec)
        assert entry["allowed_arguments"] == allowed
        assert set(entry["argument_schema"]["properties"]) == set(allowed)
        serialized = str(entry).casefold()
        assert "cypher" not in serialized
        assert "raw_query" not in serialized
        assert "cursor" not in serialized


def test_typed_capability_inputs_reuse_selector_and_enum_validation() -> None:
    search = AssetSearchCapabilityInput.model_validate({
        "filters": {
            "status": "CONFIRMED",
            "role": "Domain Controller",
            "model_confidence_max": 0.7,
        },
        "sort": "model_confidence",
        "direction": "desc",
        "limit": 3,
    })
    assert search.to_request().filters.role == "Domain Controller"
    assert search.to_request().limit == 3

    count = AssetAggregateCapabilityInput.model_validate({
        "filters": {"role": "Domain Controller"},
        "operation": "count",
    })
    grouped = AssetAggregateCapabilityInput.model_validate({
        "operation": "group_count",
        "group_by": "status",
        "limit": 3,
    })
    assert count.to_request().group_by is None
    assert grouped.to_request().group_by.value == "status"

    invalid_payloads = (
        (AssetSearchCapabilityInput, {"cursor": "opaque"}),
        (AssetSearchCapabilityInput, {"cypher": "MATCH (n) RETURN n"}),
        (AssetSearchCapabilityInput, {"filters": {"regex": ".*"}}),
        (AssetSearchCapabilityInput, {"filters": {"model_confidence_min": 0.8, "model_confidence_max": 0.2}}),
        (AssetAggregateCapabilityInput, {"operation": "group_count"}),
        (AssetAggregateCapabilityInput, {"operation": "group_count", "group_by": "arbitrary"}),
        (AssetAggregateCapabilityInput, {"operation": "count", "group_by": "status"}),
        (AssetAggregateCapabilityInput, {"operation": "count", "sort": "graph_key"}),
    )
    for model, payload in invalid_payloads:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


@pytest.mark.parametrize(
    ("query_payload", "capability", "arguments"),
    [
        (
            {
                "mode": "search",
                "filters": {"role": "Domain Controller", "model_confidence_max": 0.7},
                "sort": "model_confidence",
                "direction": "asc",
                "limit": 3,
            },
            "graph.search_assets",
            {
                "filters": {"role": "Domain Controller", "model_confidence_max": 0.7},
                "sort": "model_confidence",
                "direction": "asc",
                "limit": 3,
            },
        ),
        (
            {
                "mode": "aggregate",
                "filters": {"status": "CONFIRMED"},
                "operation": "group_count",
                "group_by": "status",
                "limit": 2,
            },
            "graph.aggregate_assets",
            {
                "filters": {"status": "CONFIRMED"},
                "operation": "group_count",
                "group_by": "status",
                "limit": 2,
            },
        ),
    ],
)
def test_direct_plan_is_one_deterministic_zero_entity_step(query_payload, capability, arguments) -> None:
    query = StructuredQuerySpec.model_validate(query_payload)
    task = _task(query)
    plan = compile_direct_plan(task)
    assert task.workflow_mode == "direct"
    assert task.entities == plan.target_entities == ()
    assert len(plan.steps) == 1
    assert plan.steps[0].capability == capability
    arguments["semantic_query_id"] = semantic_query_identity(query)
    assert plan.steps[0].arguments == arguments
    assert plan.steps[0].arguments.get("depth", 0) == 0
    assert not plan.planner_called


def test_direct_plan_fails_closed_on_structured_invariant_mismatches() -> None:
    search = StructuredQuerySpec.model_validate({"mode": "search", "filters": {"vendor": "VMware"}})
    base = _task(search)
    with pytest.raises(ValueError, match="mode contradicts"):
        compile_direct_plan(replace(base, intent="asset_aggregate"))
    with pytest.raises(ValueError, match="matching intent"):
        compile_direct_plan(replace(base, structured_query=None))
    with pytest.raises(ValueError, match="focal entities"):
        compile_direct_plan(replace(base, entities=("192.0.2.1",)))
    with pytest.raises(ValueError, match="exactly one"):
        compile_direct_plan(replace(base, optional_capabilities=("graph.get_summary",)))


def test_plan_validator_handles_structured_limits_without_weakening_topology() -> None:
    registry, *_ = _runtime()
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED"},
        "limit": 3,
    })
    plan = compile_direct_plan(_task(query))
    assert PlanValidator(registry).validate(plan).validated

    extra = replace(plan.steps[0], arguments={**plan.steps[0].arguments, "cypher": "MATCH (n) RETURN n"})
    with pytest.raises(PlanValidationError) as captured:
        PlanValidator(registry).validate(replace(plan, steps=(extra,), source="llm"))
    assert captured.value.code == "unsupported_argument"

    attached_entity = replace(plan.steps[0], arguments={**plan.steps[0].arguments, "entities": ["192.0.2.1"]})
    with pytest.raises(PlanValidationError):
        PlanValidator(registry).validate(replace(plan, steps=(attached_entity,)))

    excessive = replace(plan.steps[0], arguments={**plan.steps[0].arguments, "limit": 4})
    with pytest.raises(PlanValidationError) as captured:
        PlanValidator(registry).validate(replace(plan, steps=(excessive,)))
    assert captured.value.code == "maximum_result_scope_exceeded"

    topology_task = TaskSpec(
        request="relationship",
        intent="graph_relationships",
        scope="one_hop",
        direction="both",
        entities=("192.0.2.1",),
        required_capabilities=("graph.get_relationship",),
        graph_depth=1,
    )
    topology_plan = ExecutionPlan(
        task=topology_task,
        steps=(PlanStep("topology", "graph.get_relationship", arguments={"entities": ["192.0.2.1"]}),),
    )
    with pytest.raises(PlanValidationError) as captured:
        PlanValidator(registry).validate(topology_plan)
    assert captured.value.code == "entity_cardinality_invalid"


def test_structured_capabilities_execute_once_through_graph_specialist() -> None:
    registry, _provider, service, product, knowledge = _runtime()
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
        "limit": 2,
    })
    plan = PlanValidator(registry).validate(compile_direct_plan(_task(query)))
    output = GraphAnalysisSpecialist(CapabilityExecutor(registry)).run({
        "request_id": "phase4b2-search",
        "session_id": "phase4b2",
        "workflow_id": "phase4b2-workflow",
        "execution_plan": plan,
    })
    result = output["tool_results"][0]
    normalized = output["specialist_result"]
    assert len(service.search_requests) == 1
    assert service.search_requests[0].cursor is None
    assert result.source_capability == "graph.search_assets"
    assert result.entities == ()
    assert result.total_count == 50 and result.included_count == 2
    assert result.truncated
    assert len(result.raw_payload["rows"]) == 2
    assert "next_cursor" not in result.raw_payload
    assert normalized.executed_capabilities == ("graph.search_assets",)
    assert normalized.candidate_count == 50 and normalized.retrieved_count == 2
    assert product.calls == knowledge.calls == 0

    aggregate = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "operation": "group_count",
        "group_by": "status",
        "limit": 2,
    })
    aggregate_plan = PlanValidator(registry).validate(compile_direct_plan(_task(aggregate)))
    aggregate_output = GraphAnalysisSpecialist(CapabilityExecutor(registry)).run({
        "request_id": "phase4b2-aggregate",
        "session_id": "phase4b2",
        "workflow_id": "phase4b2-workflow-aggregate",
        "execution_plan": aggregate_plan,
    })
    aggregate_result = aggregate_output["tool_results"][0]
    assert len(service.aggregate_requests) == 1
    assert aggregate_result.source_capability == "graph.aggregate_assets"
    assert aggregate_result.entities == ()
    assert aggregate_result.total_count == aggregate_result.included_count == 9
    assert product.calls == knowledge.calls == 0


def test_asset_set_route_ignores_active_ip_but_explicit_ip_keeps_entity_workflow() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"role": "Domain Controller"},
    })
    selected = ResolvedEntity(type="ip", value="192.0.2.9", source="ui")
    structured_route = replace(
        _route(query),
        target_entity=selected,
        target_entities=[selected],
        materialized_entities=(selected.value,),
        materialized_entity_count=1,
    )
    structured_task = task_spec_from_route(structured_route, "List Domain Controllers")
    assert structured_task.entities == ()
    assert structured_task.structured_query.filters.ip is None
    assert compile_direct_plan(structured_task).steps[0].arguments["filters"] == {"role": "Domain Controller"}

    entity_route = RouteDecision(
        use_graph=False,
        use_asset_profile=True,
        use_detection=True,
        reason="explicit_asset",
        intent="asset_investigation",
        scope="node_summary",
        direction="none",
        entity_binding="explicit",
        resolved_entity_binding="explicit",
        materialized_entities=(selected.value,),
        materialized_entity_count=1,
        target_entity=replace(selected, source="message"),
    )
    entity_task = task_spec_from_route(entity_route, "Analyze 192.0.2.9")
    assert entity_task.structured_query is None
    assert entity_task.entities == ("192.0.2.9",)
    assert "graph.search_assets" not in entity_task.required_capabilities
