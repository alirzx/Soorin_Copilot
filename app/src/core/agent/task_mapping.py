"""Map validated semantic routes into the initial agent task contract."""

from __future__ import annotations

from typing import Any

from src.core.agent.contracts import ExecutionPlan, PlanStep, TaskSpec


def task_spec_from_route(route: Any, request: str) -> TaskSpec:
    capabilities: list[str] = []
    if getattr(route, "use_asset_profile", False):
        capabilities.append("asset.get_profile")
    if getattr(route, "use_detection", False):
        capabilities.append("asset.get_detection")
    if getattr(route, "use_graph", False):
        scope = getattr(route, "scope", "none")
        graph_capability = {
            "node_summary": "graph.get_summary",
            "one_hop": "graph.get_relationship"
            if getattr(route, "requires_multiple_entities", False)
            else "graph.get_neighbors",
            "full_neighbors": "graph.get_neighbors",
            "two_hop": "graph.get_neighbors",
            "multi_entity_comparison": "graph.compare_assets",
            "path": "graph.find_path",
        }.get(scope, "graph.get_summary")
        capabilities.append(graph_capability)
    if getattr(route, "use_knowledge", False):
        capabilities.append("knowledge.search")
    signals = set(getattr(route, "matched_signals", ()) or ())
    multi_step = len(capabilities) >= 3 or "security_or_anomaly" in signals
    return TaskSpec(
        request=request,
        intent=str(getattr(route, "intent", "unclear")),
        scope=str(getattr(route, "scope", "none")),
        direction=str(getattr(route, "direction", "none")),
        entities=tuple(getattr(route, "materialized_entities", ()) or ()),
        required_capabilities=tuple(capabilities),
        workflow_mode="multi_step" if multi_step else "direct",
        semantic_decision_source=str(getattr(route, "decision_source", "unknown")),
        requires_multiple_entities=bool(getattr(route, "requires_multiple_entities", False)),
        max_steps=min(4, max(1, len(capabilities))),
    )


def bounded_plan_placeholder(task: TaskSpec) -> ExecutionPlan:
    """Expose a validated read-only extension point without invoking an LLM planner."""
    steps = tuple(
        PlanStep(id=f"step-{index}", capability=capability)
        for index, capability in enumerate(task.required_capabilities[: task.max_steps], start=1)
    )
    return ExecutionPlan(
        task=task,
        steps=steps,
        max_iterations=1,
        validated=True,
    )
