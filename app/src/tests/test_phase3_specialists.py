"""Focused offline tests for Phase 3 stabilization and specialist subgraphs."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from src.core.agent.contracts import (
    EvidencePack,
    ExecutionPlan,
    PlanStep,
    ReviewDecision,
    TaskSpec,
    ToolResult,
)
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.agent.specialists import AssetInvestigationSpecialist, GraphAnalysisSpecialist
from src.core.agent.task_mapping import compile_direct_plan, task_spec_from_route
from src.core.agent.workflow import BoundedCopilotWorkflow
from src.core.context.entities import EntityResolver
from src.core.context.router import DeterministicFallbackRouter
from src.core.copilot.trace import render_human_copilot_trace, trace_from_investigation_state
from src.core.llm.providers.base import LLMProviderResult
from src.core.memory.routing_state import SessionRoutingState


ENTITY_A = "192.168.0.125"
ENTITY_B = "192.168.21.142"


def _fallback(message: str):
    state = SessionRoutingState(
        active_entities=(ENTITY_B,),
        previous_intent="asset_investigation",
        previous_scope="node_summary",
        last_providers=("asset_profile", "detection"),
    )
    resolution = EntityResolver().resolve(message, routing_state=state)
    return DeterministicFallbackRouter().route(message, resolution, state, fallback_reason="fixture")


@pytest.mark.parametrize("wording", ["Compare", "How is"])
def test_router_fallback_preserves_two_entity_comparison_and_required_evidence(wording: str) -> None:
    message = (
        f"{wording} {ENTITY_A} with it"
        if wording == "Compare"
        else f"How is {ENTITY_A} different from it?"
    )
    route = _fallback(message)
    task = task_spec_from_route(route, message)
    plan = compile_direct_plan(task)
    assert route.materialized_entities == (ENTITY_A, ENTITY_B)
    assert route.scope == "multi_entity_comparison"
    assert route.use_graph and route.use_asset_profile and route.use_detection
    assert {step.capability for step in plan.steps} == {
        "asset.get_profile",
        "asset.get_detection",
        "graph.compare_assets",
    }
    assert len(plan.steps) == 5


def test_router_fallback_selects_relationship_and_path_without_stale_route_override() -> None:
    relationship = _fallback(f"What is the direct relationship between {ENTITY_A} and {ENTITY_B}?")
    path = _fallback(f"Find the path from {ENTITY_A} to {ENTITY_B}")
    assert relationship.scope == "one_hop"
    assert task_spec_from_route(relationship, "relationship").required_capabilities == (
        "graph.get_relationship",
    )
    assert path.scope == "path"
    assert task_spec_from_route(path, "path").required_capabilities == ("graph.find_path",)
    assert relationship.reason != "fallback_previous_operational_route"
    assert path.reason != "fallback_previous_operational_route"


def _tool_result(step: PlanStep) -> ToolResult:
    graph = step.capability.startswith("graph.")
    return ToolResult(
        status="ok",
        entities=tuple(step.arguments.get("entities") or ()),
        source_capability=step.capability,
        retrieved_at="2026-01-01T00:00:00Z",
        freshness="current",
        completeness="complete",
        step_id=step.id,
        provider="graph" if graph else "product",
        selected_views=tuple(step.arguments.get("views") or ()),
        source_payload_complete=True,
        projection_usable=True,
        usable_fact_count=1,
        provider_result=SimpleNamespace(
            context={
                "candidate_node_count": 5,
                "retrieved_node_count": 5,
                "complete_for_user_request": True,
            }
        ) if graph else None,
    )


class RecordingExecutor:
    def __init__(self) -> None:
        self.plans: list[ExecutionPlan] = []

    def execute(self, plan: ExecutionPlan, **_kwargs):
        assert plan.validated
        self.plans.append(plan)
        return [_tool_result(step) for step in plan.steps]


def _mixed_plan() -> ExecutionPlan:
    task = TaskSpec(
        request=f"Analyze whether {ENTITY_A} creates a security risk for {ENTITY_B}.",
        intent="graph_relationships",
        scope="multi_entity_comparison",
        direction="both",
        entities=(ENTITY_A, ENTITY_B),
        required_capabilities=("asset.get_profile", "asset.get_detection", "graph.compare_assets"),
        workflow_mode="multi_step",
        requires_multiple_entities=True,
        graph_depth=1,
        relationship_mode="compare",
    )
    return ExecutionPlan(
        task=task,
        steps=(
            PlanStep("profile-a", "asset.get_profile", arguments={"entities": [ENTITY_A], "views": ["identity_role"]}),
            PlanStep("profile-b", "asset.get_profile", arguments={"entities": [ENTITY_B], "views": ["services"]}),
            PlanStep("detection-a", "asset.get_detection", arguments={"entities": [ENTITY_A], "views": ["anomaly_risk"]}),
            PlanStep("graph-pair", "graph.compare_assets", arguments={"entities": [ENTITY_A, ENTITY_B]}),
        ),
        validated=True,
        plan_id="mixed-plan",
        maximum_allowed_calls=6,
    )


def test_asset_and_graph_specialists_are_typed_bounded_and_add_no_llm_calls() -> None:
    executor = RecordingExecutor()
    plan = _mixed_plan()
    common = {
        "request_id": "req-specialists",
        "trace_id": "trace-specialists",
        "session_id": "session-specialists",
        "workflow_id": "wf-specialists",
        "execution_plan": plan,
    }
    asset = AssetInvestigationSpecialist(executor).run(common)["specialist_result"]
    graph = GraphAnalysisSpecialist(executor).run(common)["specialist_result"]
    assert asset.status == graph.status == "completed"
    assert set(asset.executed_capabilities) == {"asset.get_profile", "asset.get_detection"}
    assert graph.executed_capabilities == ("graph.compare_assets",)
    assert graph.scope == "multi_entity_comparison"
    assert graph.direction == "both" and graph.depth == 1
    assert graph.candidate_count == graph.retrieved_count == 5
    assert asdict(asset)["entities"] == (ENTITY_A, ENTITY_B)
    assert asdict(graph)["complete_for_request"] is True
    assert sum(len(item.steps) for item in executor.plans) == len(plan.steps)
    assert not hasattr(AssetInvestigationSpecialist(executor), "llm_client")
    assert not hasattr(GraphAnalysisSpecialist(executor), "llm_client")


class _FakeLLM:
    def chat(self, *_args, **_kwargs):
        return LLMProviderResult("grounded", "fake", "fixture", usage={"prompt_tokens": 4, "completion_tokens": 2})


def _synthesis_nodes() -> CopilotWorkflowNodes:
    nodes = object.__new__(CopilotWorkflowNodes)
    nodes.settings = SimpleNamespace(llm_expose_reasoning=False)
    nodes.stream_sink = None
    nodes.service = SimpleNamespace(llm_client=_FakeLLM())
    return nodes


def _synthesis_state(review: ReviewDecision) -> dict:
    task = _mixed_plan().task
    return {
        "request_id": "status-request",
        "session_id": "status-session",
        "trace_id": "status-trace",
        "review_decision": review,
        "context_review": {"decision": "synthesize", "required_context_missing": False},
        "tool_results": [],
        "synthesis_request": {"max_tokens": 100, "temperature": 0.2, "top_p": 0.9, "timeout_seconds": 30},
        "model_messages": [],
        "evidence_pack": EvidencePack(task, (), (), ()),
        "resolved_entities": SimpleNamespace(entities=()),
        "fallback_used": False,
    }


def test_terminal_status_is_completed_without_material_limitations() -> None:
    result = _synthesis_nodes().synthesize(_synthesis_state(ReviewDecision("sufficient")))
    assert result["workflow_status"] == "completed"
    assert result["limitation_reasons"] == []


def test_missing_required_evidence_is_completed_with_typed_limitations() -> None:
    state = _synthesis_state(
        ReviewDecision(
            "missing_required_evidence",
            reasons=("graph evidence unavailable",),
            missing_capabilities=("graph.compare_assets",),
        )
    )
    result = _synthesis_nodes().synthesize(state)
    assert result["workflow_status"] == "completed_with_limitations"
    assert result["limitation_reasons"] == ["graph evidence unavailable"]


def test_final_state_trace_restores_detail_and_does_not_leak_payloads() -> None:
    plan = _mixed_plan()
    state = {
        "request_id": "trace-request",
        "trace_id": "trace-id",
        "session_id": "trace-session",
        "message": f"Compare {ENTITY_A} with {ENTITY_B}",
        "workflow_status": "completed_with_limitations",
        "limitation_reasons": ["graph evidence incomplete"],
        "terminal": True,
        "resolved_entities": SimpleNamespace(
            status="resolved",
            entities=(SimpleNamespace(value=ENTITY_A, source="message"), SimpleNamespace(value=ENTITY_B, source="conversation")),
            reference_detected=True,
        ),
        "routing_result": SimpleNamespace(
            intent="graph_relationships",
            scope="multi_entity_comparison",
            direction="both",
            depth=1,
            decision_source="deterministic_fallback",
            fallback_used=True,
            semantic_router_called=True,
            semantic_router_latency_ms=12,
        ),
        "task": plan.task,
        "execution_plan": plan,
        "tool_results": [_tool_result(plan.steps[-1])],
        "evidence_pack": EvidencePack(plan.task, (), (), (), graph_completeness="complete"),
        "review_decision": ReviewDecision("answer_with_limitations"),
        "node_records": [{"node": "route", "status": "completed", "latency_ms": 12, "next_edge": "validate_task"}],
        "specialist_records": [{"specialist": "graph_analysis", "status": "completed", "entity_count": 2}],
        "context_review": {"decision": "synthesize", "input_tokens": 100, "remaining_usable_tokens": 200},
        "synthesis_result": {"usage": {"prompt_tokens": 4}, "latency_ms": 20},
        "memory_update_result": {"completed": True},
        "active_entity_state": SessionRoutingState(active_entities=(ENTITY_A, ENTITY_B)),
        "final_response": {"_warnings": ["limited"], "raw_payload": "must-not-leak"},
        "full_prompt": "must-not-leak",
    }
    trace = trace_from_investigation_state(state)
    rendered = render_human_copilot_trace(
        trace,
        settings=SimpleNamespace(log_format="console", log_color="never", copilot_human_trace_detail="detailed"),
        emit=False,
    )
    for section in ("LANGGRAPH WORKFLOW", "SPECIALISTS", "LLM CALLS", "FINAL STATUS"):
        assert section in rendered
    assert "graph evidence incomplete" in rendered
    assert "must-not-leak" not in rendered


def test_route_input_summary_uses_merged_resolution_state() -> None:
    summary = BoundedCopilotWorkflow._input_summary(
        "route",
        {
            "resolved_entities": {"entities": [ENTITY_A, ENTITY_B]},
            "active_entity_state": SessionRoutingState(previous_scope="node_summary"),
        },
    )
    assert summary == "entities=2,previous_scope=node_summary"
