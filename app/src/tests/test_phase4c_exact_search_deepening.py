"""Phase 4C deterministic candidate-selection and fan-out guards."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.agent.contracts import (
    ExecutionPlan,
    RequestConstraints,
    StructuredAssetSearchEvidence,
    TaskSpec,
    ToolResult,
)
from src.core.agent.phase4c_nodes import Phase4CWorkflowNodes
from src.core.agent.reviewer import EvidenceReviewer
from src.core.agent.task_mapping import compile_direct_plan
from src.core.graph.structured import StructuredQuerySpec, structured_query_identity


def _task(request: str, *, sort: str | None = None) -> TaskSpec:
    payload: dict[str, object] = {
        "mode": "search",
        "filters": {"role": "Domain Controller"},
    }
    if sort is not None:
        payload.update({"sort": sort, "direction": "desc"})
    return TaskSpec(
        request=request,
        intent="asset_search",
        scope="none",
        direction="none",
        entities=(),
        required_capabilities=("graph.search_assets",),
        structured_query=StructuredQuerySpec.model_validate(payload),
        workflow_mode="direct",
    )


def _rows(count: int) -> tuple[dict[str, object], ...]:
    return tuple(
        {"ip": f"192.0.2.{index}", "graph_key": f"asset-{index}"}
        for index in range(1, count + 1)
    )


def _search_result(task: TaskSpec) -> ToolResult:
    assert task.structured_query is not None
    identity = structured_query_identity(
        task.structured_query,
        active_graph_version="graph-v1",
    )
    evidence = StructuredAssetSearchEvidence(
        capability="graph.search_assets",
        query_identity=identity,
        normalized_filters={"role": "Domain Controller"},
        active_graph_version="graph-v1",
        sort=task.structured_query.to_search_request().sort.value,
        direction=task.structured_query.to_search_request().direction.value,
        matched_total=1,
        returned_count=1,
        truncated=False,
        rows=({"ip": "192.0.2.1", "graph_key": "asset-1"},),
        retrieved_at="2026-09-11T10:00:00+00:00",
    )
    return ToolResult(
        status="ok",
        entities=(),
        source_capability="graph.search_assets",
        retrieved_at=evidence.retrieved_at,
        freshness="current",
        completeness="complete",
        total_count=1,
        included_count=1,
        omitted_count=0,
        source_payload_complete=True,
        projection_usable=True,
        normalized_query_hash=identity,
        context_identity=identity,
        structured_asset_set=evidence,
        step_id="search-1",
        provider="graph",
    )


def test_zero_candidates_fail_closed() -> None:
    selected, reason = Phase4CWorkflowNodes._select_candidates(
        _task("analyze the first result"),
        (),
    )
    assert selected == ()
    assert reason == "zero_candidates"


def test_one_candidate_is_safe_to_deepen() -> None:
    selected, reason = Phase4CWorkflowNodes._select_candidates(
        _task("analyze matching domain controllers"),
        _rows(1),
    )
    assert selected == ("192.0.2.1",)
    assert reason == "single_candidate"


def test_many_candidates_without_deterministic_selection_do_not_fan_out() -> None:
    selected, reason = Phase4CWorkflowNodes._select_candidates(
        _task("analyze matching domain controllers"),
        _rows(20),
    )
    assert selected == ()
    assert reason == "ambiguous_multi_candidate_selection"


def test_first_result_uses_deterministic_search_order() -> None:
    selected, reason = Phase4CWorkflowNodes._select_candidates(
        _task("analyze the first result"),
        _rows(5),
    )
    assert selected == ("192.0.2.1",)
    assert reason == "explicit_ranked_single"


def test_rank_semantics_require_structured_sort() -> None:
    selected, reason = Phase4CWorkflowNodes._select_candidates(
        _task("analyze the highest confidence result"),
        _rows(5),
    )
    assert selected == ()
    assert reason == "rank_without_structured_sort"

    selected, reason = Phase4CWorkflowNodes._select_candidates(
        _task("analyze the highest confidence result", sort="model_confidence"),
        _rows(5),
    )
    assert selected == ("192.0.2.1",)
    assert reason == "explicit_ranked_single"


def test_exact_two_candidates_can_be_compared_without_n_way_fanout() -> None:
    selected, reason = Phase4CWorkflowNodes._select_candidates(
        _task("compare the matching assets"),
        _rows(2),
    )
    assert selected == ("192.0.2.1", "192.0.2.2")
    assert reason == "exact_two_candidates_for_comparison"


def test_more_than_two_comparison_candidates_require_explicit_ranked_pair() -> None:
    selected, reason = Phase4CWorkflowNodes._select_candidates(
        _task("compare the matching assets"),
        _rows(10),
    )
    assert selected == ()
    assert reason == "ambiguous_multi_candidate_comparison"

    selected, reason = Phase4CWorkflowNodes._select_candidates(
        _task("compare the top two matching assets", sort="model_confidence"),
        _rows(10),
    )
    assert selected == ("192.0.2.1", "192.0.2.2")
    assert reason == "explicit_ranked_pair"


def test_total_turn_call_budget_drops_optional_knowledge_first() -> None:
    query_task = _task("compare the top two and give hardening guidance", sort="model_confidence")
    parent = compile_direct_plan(query_task, plan_id="parent")
    focal = TaskSpec(
        request=query_task.request,
        intent="asset_investigation",
        scope="multi_entity_comparison",
        direction="both",
        entities=("192.0.2.1", "192.0.2.2"),
        required_capabilities=(
            "asset.get_profile",
            "asset.get_detection",
            "graph.compare_assets",
        ),
        optional_capabilities=("knowledge.search",),
        workflow_mode="direct",
        requires_multiple_entities=True,
        graph_depth=1,
        relationship_mode="compare",
    )

    deepening = Phase4CWorkflowNodes._compile_deepening_plan(focal, parent)

    assert len(parent.steps) + len(deepening.steps) <= 6
    assert all(step.capability != "knowledge.search" for step in deepening.steps)
    assert sum(step.capability == "asset.get_profile" for step in deepening.steps) == 2
    assert sum(step.capability == "asset.get_detection" for step in deepening.steps) == 2
    assert sum(step.capability == "graph.compare_assets" for step in deepening.steps) == 1


def test_required_deepening_fails_closed_if_parent_consumes_budget() -> None:
    task = TaskSpec(
        request="analyze the first result",
        intent="asset_investigation",
        scope="node_summary",
        direction="both",
        entities=("192.0.2.1",),
        required_capabilities=("asset.get_profile", "asset.get_detection", "graph.get_summary"),
        workflow_mode="direct",
    )
    parent = ExecutionPlan(
        task=_task("analyze the first result"),
        steps=tuple(compile_direct_plan(_task("analyze the first result")).steps) * 5,
        maximum_allowed_calls=6,
        plan_id="almost-full",
    )

    with pytest.raises(ValueError, match="phase4c_deepening_call_budget_exceeded"):
        Phase4CWorkflowNodes._compile_deepening_plan(task, parent)


def test_structured_set_turn_never_becomes_focal_baseline() -> None:
    reason = Phase4CWorkflowNodes._baseline_capture_rejection_reason(
        _task("analyze the first result"),
        (),
        None,
        None,
    )
    assert reason == "structured_asset_set_not_focal_baseline"


@pytest.mark.parametrize("capability", ["asset.get_profile", "asset.get_detection"])
def test_missing_product_focal_evidence_is_material_limitation(capability: str) -> None:
    task = _task("analyze the first result")
    search = _search_result(task)
    failed = ToolResult(
        status="unavailable",
        entities=("192.0.2.1",),
        source_capability=capability,
        retrieved_at="2026-09-11T10:00:01+00:00",
        freshness="unknown",
        completeness="unknown",
        limitations=("provider unavailable",),
        step_id="deepening-1",
        provider="product",
    )
    reviewer = EvidenceReviewer()
    pack = reviewer.build_pack(task, [search, failed])
    nodes = object.__new__(Phase4CWorkflowNodes)
    nodes.settings = SimpleNamespace(agent_max_supplemental_retrievals=1)
    nodes.service = SimpleNamespace(evidence_reviewer=reviewer)

    update = nodes.review_retrieval({
        "task": task,
        "tool_results": [search, failed],
        "evidence_pack": pack,
        "request_constraints": RequestConstraints(),
        "message": task.request,
        "request_id": "phase4c-product-failure",
        "supplemental_retrieval_count": 0,
    })

    assert update["review_decision"].outcome == "answer_with_limitations"
    assert update["review_decision"].supplemental_allowed is False
    assert any(capability in item for item in update["review_decision"].material_limitations)


def test_optional_knowledge_failure_remains_caveat_not_operational_substitute() -> None:
    task = _task("analyze the first result and give hardening guidance")
    search = _search_result(task)
    unavailable = ToolResult(
        status="not_configured",
        entities=(),
        source_capability="knowledge.search",
        retrieved_at="2026-09-11T10:00:01+00:00",
        freshness="unknown",
        completeness="unknown",
        limitations=("knowledge unavailable",),
        step_id="deepening-1",
        provider="knowledge",
    )
    reviewer = EvidenceReviewer()
    pack = reviewer.build_pack(task, [search, unavailable])
    nodes = object.__new__(Phase4CWorkflowNodes)
    nodes.settings = SimpleNamespace(agent_max_supplemental_retrievals=1)
    nodes.service = SimpleNamespace(evidence_reviewer=reviewer)

    update = nodes.review_retrieval({
        "task": task,
        "tool_results": [search, unavailable],
        "evidence_pack": pack,
        "request_constraints": RequestConstraints(),
        "message": task.request,
        "request_id": "phase4c-knowledge-failure",
        "supplemental_retrieval_count": 0,
    })

    assert update["review_decision"].outcome == "sufficient"
    assert not update["review_decision"].material_limitations
    assert any("knowledge.search" in item or "knowledge unavailable" in item for item in update["review_decision"].limitations)
