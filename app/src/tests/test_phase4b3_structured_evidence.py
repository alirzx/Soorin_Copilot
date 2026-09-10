"""Phase 4B.3 structured Asset-set evidence contract tests."""

from __future__ import annotations

from types import SimpleNamespace

from src.core.agent.contracts import RequestConstraints, TaskSpec
from src.core.agent.evidence_policy import EvidenceRequirementPolicy, MemorySufficiencyGate
from src.core.agent.registry import build_capability_registry
from src.core.agent.reviewer import EvidenceReviewer
from src.core.agent.structured_evidence import structured_query_identity
from src.core.agent.task_mapping import task_spec_from_route
from src.core.context.models import GraphProviderResult, ProviderProvenance, RouteDecision
from src.core.graph.structured import StructuredQuerySpec


class _UnusedProvider:
    settings = SimpleNamespace(product_read_timeout_seconds=1, rag_qdrant_timeout_seconds=1, rag_top_k=3)

    def fetch(self, *_args, **_kwargs):
        raise AssertionError("Product provider must not be called")

    def search(self, *_args, **_kwargs):
        raise AssertionError("Knowledge provider must not be called")


class _StructuredGraphProvider:
    settings = SimpleNamespace(
        agent_request_timeout_seconds=2,
        graph_asset_search_max_limit=200,
        graph_full_neighbors_hard_max=500,
    )

    def __init__(self, *, version: str = "graph-v7", empty: bool = False) -> None:
        self.version = version
        self.empty = empty

    def search_assets(self, request):
        rows = [] if self.empty else [
            {
                "graph_key": "asset-1",
                "graph_version": self.version,
                "ip": "192.0.2.10",
                "asset_name": "dc-01",
                "status": "CONFIRMED",
                "role": "Domain Controller",
            }
        ]
        matched = 0 if self.empty else 12
        return self._result(
            status="not_found" if self.empty else "available",
            context={
                "active_graph_version": self.version,
                "filters": request.filters.model_dump(mode="json", exclude_none=True),
                "rows": rows,
                "returned_count": len(rows),
                "matched_total": matched,
                "truncated": bool(matched > len(rows)),
                "sort": request.sort.value,
                "direction": request.direction.value,
                "retrieved_at": "2026-09-11T00:00:00Z",
                "retrieval_complete": matched == len(rows),
                "serialized_context_complete_for_retrieved_subset": True,
            },
        )

    def aggregate_assets(self, request):
        groups = (
            [{"value": "CONFIRMED", "count": 9}, {"value": "REVIEW", "count": 3}]
            if request.group_by
            else []
        )
        return self._result(context={
            "active_graph_version": self.version,
            "filters": request.filters.model_dump(mode="json", exclude_none=True),
            "operation": request.operation.value,
            "group_by": request.group_by.value if request.group_by else None,
            "count": 12,
            "groups": groups,
            "truncated": False,
            "retrieved_at": "2026-09-11T00:00:00Z",
            "retrieved_node_count": 12,
            "retrieval_complete": True,
            "serialized_context_complete_for_retrieved_subset": True,
        })

    @staticmethod
    def _result(*, context, status="available"):
        return GraphProviderResult(
            provider="graph",
            status=status,
            context=context,
            provenance=ProviderProvenance(
                source="neo4j_structured_asset_projection",
                status=status,
            ),
            limitations=[
                "Enrichment is a discovery projection, not live Product profile or detection evidence."
            ],
        )


def _registry(*, version: str = "graph-v7", empty: bool = False):
    unused = _UnusedProvider()
    return build_capability_registry(
        asset_profile_provider=unused,
        detection_provider=unused,
        graph_provider=_StructuredGraphProvider(version=version, empty=empty),
        knowledge_service=unused,
    )


def _task(query: StructuredQuerySpec) -> TaskSpec:
    route = RouteDecision(
        use_graph=True,
        reason="structured_asset_set",
        structured_query=query,
        intent="asset_search" if query.mode.value == "search" else "asset_aggregate",
        scope="none",
        direction="none",
        entity_binding="none",
        resolved_entity_binding="none",
    )
    return task_spec_from_route(route, "structured request", RequestConstraints())


def test_search_tool_result_preserves_typed_query_counts_rows_and_provenance() -> None:
    result = _registry().execute("graph.search_assets", {
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
        "sort": "asset_name",
        "direction": "asc",
        "limit": 50,
    })

    evidence = result.structured_asset_set
    assert result.evidence_type == "graph_asset_search"
    assert result.total_count == 12
    assert result.included_count == 1
    assert result.omitted_count == 11
    assert result.truncated and result.completeness == "partial"
    assert evidence is not None and evidence.mode == "search"
    assert evidence.normalized_filters == {
        "role": "Domain Controller",
        "status": "CONFIRMED",
    }
    assert evidence.active_graph_version == "graph-v7"
    assert evidence.matched_total == 12 and evidence.returned_count == 1
    assert evidence.rows[0]["ip"] == "192.0.2.10"
    assert evidence.provenance == "neo4j_active_organizational_projection"
    assert result.normalized_query_hash == result.context_identity == evidence.query_identity


def test_aggregate_tool_result_and_evidence_pack_preserve_typed_groups() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "filters": {"role": "Domain Controller"},
        "operation": "group_count",
        "group_by": "status",
        "limit": 10,
    })
    result = _registry().execute("graph.aggregate_assets", {
        "filters": query.filters.model_dump(mode="json", exclude_none=True),
        "operation": "group_count",
        "group_by": "status",
        "limit": 10,
    })
    pack = EvidenceReviewer().build_pack(_task(query), [result])
    evidence = result.structured_asset_set

    assert result.evidence_type == "graph_asset_aggregate"
    assert evidence is not None and evidence.mode == "aggregate"
    assert evidence.operation == "group_count" and evidence.group_by == "status"
    assert evidence.count == 12 and sum(group["count"] for group in evidence.groups) == 12
    assert evidence.active_graph_version == "graph-v7"
    assert pack.structured_asset_sets == (evidence,)
    assert pack.tool_results[0].structured_asset_set is evidence


def test_structured_query_identity_is_canonical_and_graph_version_bound() -> None:
    first = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"role": "Domain Controller", "status": "CONFIRMED"},
        "sort": "graph_key",
        "direction": "asc",
        "limit": 10,
    })
    reordered = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
        "direction": "asc",
        "sort": "graph_key",
        "limit": 50,
    })
    different = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"vendor": "VMware"},
    })

    identity = structured_query_identity(first, active_graph_version="graph-v7")
    assert identity == structured_query_identity(reordered, active_graph_version="graph-v7")
    assert identity != structured_query_identity(different, active_graph_version="graph-v7")
    assert identity != structured_query_identity(first, active_graph_version="graph-v8")


def test_successful_empty_search_remains_current_typed_evidence() -> None:
    result = _registry(empty=True).execute("graph.search_assets", {
        "filters": {"status": "CONFIRMED"},
    })
    evidence = result.structured_asset_set

    assert result.status == "not_found"
    assert result.total_count == result.included_count == result.omitted_count == 0
    assert not result.truncated
    assert evidence is not None
    assert evidence.matched_total == evidence.returned_count == 0
    assert evidence.rows == ()


def test_requirement_policy_uses_current_structured_classes_without_product_requirements() -> None:
    search = _task(StructuredQuerySpec.model_validate({"mode": "search", "filters": {"vendor": "VMware"}}))
    aggregate = _task(StructuredQuerySpec.model_validate({"mode": "aggregate", "operation": "count"}))

    search_requirements = EvidenceRequirementPolicy().derive(search)
    aggregate_requirements = EvidenceRequirementPolicy().derive(aggregate)

    assert [(item.capability, item.evidence_class, item.freshness_class) for item in search_requirements.requirements] == [
        ("graph.search_assets", "graph_asset_search", "current_verification")
    ]
    assert [(item.capability, item.evidence_class, item.freshness_class) for item in aggregate_requirements.requirements] == [
        ("graph.aggregate_assets", "graph_asset_aggregate", "current_verification")
    ]
    assert MemorySufficiencyGate().evaluate(search_requirements, ())[0].decision == "memory_unavailable"
    assert not any(item.capability.startswith("asset.") for item in search_requirements.requirements)


def test_existing_topology_requirement_class_is_unchanged() -> None:
    task = TaskSpec(
        request="Show neighbors",
        intent="graph_neighbors",
        scope="one_hop",
        direction="both",
        entities=("192.0.2.10",),
        required_capabilities=("graph.get_neighbors",),
    )
    requirements = EvidenceRequirementPolicy().derive(task)
    assert requirements.requirements[0].evidence_class == "graph_neighbors"
