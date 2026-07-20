"""Map validated semantic routes into the initial agent task contract."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from uuid import uuid4

from src.core.agent.contracts import ExecutionPlan, PlanStep, TaskSpec


MULTI_STEP_WORDING = re.compile(
    r"\b(?:investigate\s+why|investigate\s+whether|determine\s+whether|correlate|"
    r"comprehensive\s+(?:investigation|analysis|report)|analy[sz]e\s+the\s+likely\s+cause|"
    r"greater\s+(?:potential\s+)?impact|deep(?:ly|\s+investigation)|expected\s+or\s+suspicious)\b",
    re.IGNORECASE,
)


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
    multi_step = (
        len(capabilities) >= 3
        or "security_or_anomaly" in signals
        or bool(MULTI_STEP_WORDING.search(request))
    )
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
        detail_level="detailed" if "detailed" in request.lower() else "brief" if any(word in request.lower() for word in ("brief", "short")) else "standard",
        is_followup=bool(getattr(route, "followup_detected", False)),
        graph_depth=int(getattr(route, "depth", 0) or 0),
        relationship_mode=str(getattr(route, "relationship_mode", "none")),
    )


def compile_direct_plan(task: TaskSpec, *, plan_id: str | None = None) -> ExecutionPlan:
    """Compile a deterministic plan for a validated direct semantic task."""
    steps: list[PlanStep] = []
    for capability in task.required_capabilities:
        if capability in {"asset.get_profile", "asset.get_detection"}:
            targets = task.entities[:2]
            for entity in targets:
                steps.append(
                    PlanStep(
                        id=f"step-{len(steps) + 1}",
                        capability=capability,
                        arguments={"entities": [entity]},
                        expected_evidence_type="operational_product",
                    )
                )
        elif capability == "knowledge.search":
            steps.append(
                PlanStep(
                    id=f"step-{len(steps) + 1}",
                    capability=capability,
                    arguments={"query": task.request},
                    expected_evidence_type="documentation",
                )
            )
        else:
            steps.append(
                PlanStep(
                    id=f"step-{len(steps) + 1}",
                    capability=capability,
                    arguments={
                        "entities": list(task.entities),
                        "scope": task.scope,
                        "direction": task.direction,
                        "depth": task.graph_depth,
                        "relationship_mode": task.relationship_mode,
                    },
                    expected_evidence_type="graph_topology",
                )
            )
    steps = steps[:6]
    return ExecutionPlan(
        task=task,
        steps=tuple(steps),
        max_iterations=1,
        validated=False,
        plan_id=plan_id or uuid4().hex[:12],
        goal=task.request,
        target_entities=task.entities,
        maximum_allowed_calls=6,
        planner_called=False,
        source="deterministic",
    )


def compile_supplemental_plan(
    task: TaskSpec,
    capability: str,
    arguments: dict[str, Any],
    *,
    plan_id: str,
) -> ExecutionPlan:
    """Compile one complete reviewer-proposed step before deterministic validation."""
    supplemental_task = replace(
        task,
        required_capabilities=(capability,),
        max_steps=1,
    )
    evidence_type = (
        "documentation"
        if capability == "knowledge.search"
        else "graph_topology"
        if capability.startswith("graph.")
        else "operational_product"
    )
    return ExecutionPlan(
        task=supplemental_task,
        steps=(
            PlanStep(
                id="supplemental-1",
                capability=capability,
                arguments=dict(arguments),
                expected_evidence_type=evidence_type,
            ),
        ),
        max_iterations=1,
        validated=False,
        plan_id=plan_id,
        goal=task.request,
        target_entities=task.entities,
        maximum_allowed_calls=1,
        planner_called=False,
        source="deterministic_fallback",
    )
