"""Map validated semantic routes into the initial agent task contract."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from uuid import uuid4

from src.core.agent.contracts import EvidenceMode, ExecutionPlan, PlanStep, TaskSpec
from src.core.context.product_views import select_product_views


MULTI_STEP_WORDING = re.compile(
    r"\b(?:investigate\s+why|investigate\s+whether|determine\s+whether|correlate|"
    r"comprehensive\s+(?:investigation|analysis|report)|analy[sz]e\s+the\s+likely\s+cause|"
    r"greater\s+(?:potential\s+)?impact|deep(?:ly|\s+investigation)|expected\s+or\s+suspicious)\b",
    re.IGNORECASE,
)

SOURCE_SPECIFIC_KNOWLEDGE = re.compile(
    r"\b(?:according\s+to|quote|cite|use\s+only|what\s+does)\b.*"
    r"\b(?:indexed|uploaded|knowledge[\s-]*base|document|source|nist|mitre)\b",
    re.IGNORECASE,
)

MEMORY_RECALL_REQUEST = re.compile(
    r"\b(?:which\s+asset.*remember|what\s+(?:do\s+you\s+)?(?:know|remember).*(?:prior|memory|before\s+the\s+restart)|"
    r"summari[sz]e\s+what\s+you\s+remember|continue\s+with\s+the\s+same\s+asset.*before\s+the\s+restart|"
    r"conversation\s+memory|episodic\s+memory|long[\s-]*term\s+memory|prior\s+investigations?|what(?:'s|\s+is)\s+my\s+name|"
    r"what\s+did\s+(?:i|we)\s+(?:tell|say)|what\s+was\s+the\s+previous\s+contradiction|"
    r"from\s+(?:stored\s+context|our\s+previous\s+investigation))\b",
    re.IGNORECASE,
)

NO_LIVE_EVIDENCE_REQUEST = re.compile(
    r"\b(?:before\s+making\s+any\s+live\s+provider\s+calls|without\s+refreshing|without\s+(?:fetching|calling|using)\s+"
    r"(?:any\s+)?(?:live\s+)?evidence|do\s+not\s+(?:use|call)\s+(?:live\s+)?(?:product|detection|graph|knowledge|evidence|providers?|refresh)|"
    r"don't\s+use\s+live|do\s+not\s+refresh|don't\s+refresh|no\s+live\s+(?:provider|evidence|refresh)|memory\s+only)\b",
    re.IGNORECASE,
)


def evidence_mode_from_request(request: str) -> EvidenceMode:
    """Recognize explicit recall scope without delegating tool authority to the model."""
    if MEMORY_RECALL_REQUEST.search(request):
        return "memory_only"
    if NO_LIVE_EVIDENCE_REQUEST.search(request):
        return "no_live_refresh"
    return "normal"


def task_spec_from_route(route: Any, request: str) -> TaskSpec:
    request_lower = request.lower()
    evidence_mode = evidence_mode_from_request(request)
    entities = tuple(dict.fromkeys(getattr(route, "materialized_entities", ()) or ()))
    if getattr(route, "scope", "none") == "multi_entity_comparison" and len(entities) != 2:
        raise ValueError("comparison_requires_two_distinct_entities")
    required_capabilities: list[str] = []
    optional_capabilities: list[str] = []
    if evidence_mode == "normal" and getattr(route, "use_asset_profile", False):
        required_capabilities.append("asset.get_profile")
    if evidence_mode == "normal" and getattr(route, "use_detection", False):
        required_capabilities.append("asset.get_detection")
    if evidence_mode == "normal" and getattr(route, "use_graph", False):
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
        required_capabilities.append(graph_capability)
    if evidence_mode == "normal" and getattr(route, "use_knowledge", False):
        target = (
            required_capabilities
            if SOURCE_SPECIFIC_KNOWLEDGE.search(request)
            else optional_capabilities
        )
        target.append("knowledge.search")
    capabilities = (*required_capabilities, *optional_capabilities)
    signals = set(getattr(route, "matched_signals", ()) or ())
    multi_step = (
        len(capabilities) >= 3
        or "security_or_anomaly" in signals
        or bool(MULTI_STEP_WORDING.search(request))
    )
    return TaskSpec(
        request=request,
        intent="memory_recall" if evidence_mode == "memory_only" else str(getattr(route, "intent", "unclear")),
        scope=str(getattr(route, "scope", "none")),
        direction=str(getattr(route, "direction", "none")),
        entities=entities,
        required_capabilities=tuple(required_capabilities),
        optional_capabilities=tuple(optional_capabilities),
        workflow_mode="direct" if evidence_mode == "memory_only" else "multi_step" if multi_step else "direct",
        semantic_decision_source=str(getattr(route, "decision_source", "unknown")),
        requires_multiple_entities=bool(getattr(route, "requires_multiple_entities", False)),
        recommended_steps=min(4, max(1, len(capabilities))),
        detail_level=(
            "deep"
            if any(word in request_lower for word in ("detailed", "deep", "comprehensive", "report"))
            else "brief"
            if any(word in request_lower for word in ("brief", "short"))
            else "standard"
        ),
        is_followup=bool(getattr(route, "followup_detected", False)),
        graph_depth=int(getattr(route, "depth", 0) or 0),
        relationship_mode=str(getattr(route, "relationship_mode", "none")),
        temporal_mode="historical" if evidence_mode == "memory_only" else "current",
        evidence_mode=evidence_mode,
        response_depth=(
            "report"
            if "report" in request_lower
            else "deep"
            if any(word in request_lower for word in ("detailed", "deep", "comprehensive"))
            else "brief"
            if any(word in request_lower for word in ("brief", "short"))
            else "standard"
        ),
    )


def compile_direct_plan(task: TaskSpec, *, plan_id: str | None = None) -> ExecutionPlan:
    """Compile a deterministic plan for a validated direct semantic task."""
    steps: list[PlanStep] = []
    capabilities = (*task.required_capabilities, *task.optional_capabilities)
    for capability in capabilities:
        requirement = (
            "required"
            if capability in task.required_capabilities
            else "optional"
        )
        if capability in {"asset.get_profile", "asset.get_detection"}:
            targets = task.entities[:2]
            provider = "asset_profile" if capability == "asset.get_profile" else "detection"
            detail = "deep" if task.detail_level in {"detailed", "deep", "report"} else task.detail_level
            views = select_product_views(provider, task.request, detail)
            purpose = (
                "explain_detection"
                if provider == "detection" and "evidence" in views
                else "establish_identity"
                if "identity" in views
                else "asset_summary"
            )
            for entity in targets:
                steps.append(
                    PlanStep(
                        id=f"step-{len(steps) + 1}",
                        capability=capability,
                        arguments={
                            "entities": [entity],
                            "views": list(views),
                            "detail": detail,
                            "max_context_tokens": 5000 if detail == "deep" else 3000,
                            "purpose": purpose,
                        },
                        requirement=requirement,
                        expected_evidence_type="operational_product",
                    )
                )
        elif capability == "knowledge.search":
            steps.append(
                PlanStep(
                    id=f"step-{len(steps) + 1}",
                    capability=capability,
                    arguments={
                        "query": task.request,
                        "purpose": "interpret_evidence",
                        "max_context_tokens": 3000,
                    },
                    requirement=requirement,
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
                    requirement=requirement,
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
        optional_capabilities=(),
        recommended_steps=1,
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
