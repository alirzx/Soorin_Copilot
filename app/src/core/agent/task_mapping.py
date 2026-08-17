"""Map validated semantic routes into the initial agent task contract."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Literal
from uuid import uuid4

from src.core.agent.contracts import (
    EvidenceMode,
    ExecutionPlan,
    PlanStep,
    RequestConstraints,
    TaskSpec,
)
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

EXPLICIT_MEMORY_RECALL_REQUEST = re.compile(
    r"\b(?:which\s+asset.*remember|what\s+(?:do\s+you\s+)?(?:know|remember).*(?:prior|memory|before\s+the\s+restart)|"
    r"summari[sz]e\s+what\s+you\s+remember|continue\s+with\s+the\s+same\s+asset.*before\s+the\s+restart|"
    r"conversation\s+memory|episodic\s+memory|long[\s-]*term\s+memory|prior\s+investigations?|what(?:'s|\s+is)\s+my\s+name|"
    r"what\s+(?:do|did)\s+you\s+remember|what\s+you\s+remember|do\s+you\s+remember|"
    r"what\s+did\s+(?:i|we)\s+(?:tell|say)|what\s+was\s+the\s+previous\s+contradiction|"
    r"from\s+(?:stored\s+(?:context|conversation\s+context)|our\s+previous\s+investigation)|"
    r"what\s+(?:investigation\s+)?state\s+(?:(?:did\s+)?you\s+)?retain(?:ed)?|"
    r"what\s+was\s+retained\s+after\s+(?:login|sign(?:ed|ing)\s+back\s+in|restart)|"
    r"what\s+did\s+we\s+discuss|remind\s+me\s+what\s+we\s+knew|retained\s+investigation\s+state|"
    r"(?:investigation\s+tag|owner\s+(?:validation\s+)?status|identity\s+contradiction).{0,100}"
    r"(?:do\s+we\s+have|did\s+we\s+establish|earlier|previously))\b",
    re.IGNORECASE,
)

HISTORICAL_SUMMARY_REQUEST = re.compile(
    r"\b(?:what\s+have\s+we\s+(?:concluded|established|found|learned)|"
    r"what\s+did\s+we\s+(?:conclude|establish|find|learn)|"
    r"what\s+do\s+we\s+know\s+so\s+far|(?:investigation|findings?|conclusions?)\s+so\s+far|"
    r"historical\s+summary|previous\s+findings?|earlier\s+(?:findings?|conclusions?))\b",
    re.IGNORECASE,
)

NO_LIVE_EVIDENCE_REQUEST = re.compile(
    r"\b(?:before\s+making\s+any\s+live\s+provider\s+calls|without\s+refreshing|without\s+(?:fetching|calling|using)\s+"
    r"(?:any\s+)?(?:live\s+)?evidence|without\s+(?:performing\s+)?(?:any\s+)?live\s+(?:lookup|check|retrieval)|"
    r"without\s+checking\s+(?:any\s+)?current\s+(?:status|state|systems?)|"
    r"do\s+not\s+retrieve\b|"
    r"do\s+not\s+(?:use|call|retrieve|check|look\s+up)\s+(?:any\s+)?(?:current\s+|live\s+)?(?:product|detection|graph|knowledge|evidence|providers?|refresh|systems?|status|state|data|lookup)|"
    r"don't\s+use\s+live|do\s+not\s+refresh|don't\s+refresh|no\s+live\s+(?:provider|evidence|refresh)|"
    r"without\s+(?:using\s+)?live\s+(?:sources?|data)|do\s+not\s+use\s+live\s+(?:sources?|data)|"
    r"use\s+only\s+memory|using\s+only\s+stored\s+(?:conversation\s+)?context|memory\s+only|"
    r"based\s+only\s+on\s+what\s+we\s+(?:discussed|knew|established)|historical\s+only)\b",
    re.IGNORECASE,
)

MEMORY_WRITE_REQUEST = re.compile(
    r"\b(?:remember|keep|retain|store)\b.{0,160}\b(?:for\s+(?:this|the)\s+(?:conversation|investigation)|"
    r"in\s+(?:this|the)\s+(?:conversation|investigation)|my\s+name|tag|owner\s+validation|analyst\s+note|contradiction)\b",
    re.IGNORECASE,
)

CURRENT_EVIDENCE_REQUEST = re.compile(
    r"\b(?:fresh|current|currently|now|right\s+now|still|verify\s+(?:now|again)|recheck|refresh|latest|"
    r"live\s+(?:evidence|data|state))\b",
    re.IGNORECASE,
)

NO_LIVE_HISTORICAL_REQUEST = re.compile(
    r"\b(?:memory\s+only|use\s+only\s+memory|historical\s+only|based\s+only\s+on\s+what\s+we\s+"
    r"(?:discussed|knew|established)|stored\s+(?:context|memory)|previous\s+investigation|retained\s+state)\b",
    re.IGNORECASE,
)

IDENTITY_CONTRADICTION_REQUEST = re.compile(
    r"\b(?:identity|role|classification|fingerprint)\b.{0,100}\b(?:contradiction|conflict|mismatch|inconsisten(?:cy|t))\b|"
    r"\b(?:contradiction|conflict|mismatch|inconsisten(?:cy|t))\b.{0,100}\b(?:identity|role|classification|fingerprint)\b",
    re.IGNORECASE,
)

TOPOLOGY_EVIDENCE_REQUEST = re.compile(
    r"\b(?:graph|topology|connections?|neighbors?|relationships?|paths?|communications?|network\s+behavior)\b",
    re.IGNORECASE,
)

KNOWLEDGE_EVIDENCE_REQUEST = re.compile(
    r"\b(?:why|explain|explanation|background|runbook|playbook|guidance|procedure|mitre|nist|hardening)\b",
    re.IGNORECASE,
)

PREVIOUS_CURRENT_COMPARISON_REQUEST = re.compile(
    r"\b(?:same|changed|different|still|previous(?:ly)?|earlier|compared\s+(?:with|to)|since)\b",
    re.IGNORECASE,
)

RecallClassification = Literal["none", "explicit_memory", "historical_summary"]


def classify_historical_recall(request: str) -> RecallClassification:
    """Classify only clear historical-memory intent; ordinary asset questions remain live."""
    if EXPLICIT_MEMORY_RECALL_REQUEST.search(request):
        return "explicit_memory"
    if HISTORICAL_SUMMARY_REQUEST.search(request):
        return "historical_summary"
    return "none"


def derive_request_constraints(request: str) -> RequestConstraints:
    """Resolve live-evidence and memory authority without an LLM."""
    no_live = bool(NO_LIVE_EVIDENCE_REQUEST.search(request))
    recall_classification = classify_historical_recall(request)
    recall = recall_classification != "none"
    memory_write = bool(MEMORY_WRITE_REQUEST.search(request))
    require_current = bool(CURRENT_EVIDENCE_REQUEST.search(request)) and not no_live
    pure_memory_write = memory_write and not require_current and not re.search(
        r"\b(?:check|verify|analy[sz]e|investigate|compare|show\s+(?:connections?|neighbors?|path))\b",
        request,
        re.IGNORECASE,
    )
    memory_only = (
        (
            recall
            or bool(no_live and NO_LIVE_HISTORICAL_REQUEST.search(request))
        )
        and not require_current
    ) or pure_memory_write
    reasons: list[str] = []
    if no_live:
        reasons.append("explicit_no_live")
    if recall:
        reasons.append(f"deterministic_memory_recall:{recall_classification}")
    if memory_write:
        reasons.append("session_memory_write")
    if require_current:
        reasons.append("current_evidence_requested")
    return RequestConstraints(
        allow_live=not no_live and not memory_only,
        require_current=require_current,
        memory_only=memory_only,
        memory_write=memory_write,
        reason_codes=tuple(reasons),
    )


def evidence_mode_from_request(request: str) -> EvidenceMode:
    """Recognize explicit recall scope without delegating tool authority to the model."""
    constraints = derive_request_constraints(request)
    if constraints.memory_only:
        return "memory_only"
    if not constraints.allow_live:
        return "no_live_refresh"
    if constraints.require_current:
        return "current_verification"
    return "normal"


def task_spec_from_route(
    route: Any,
    request: str,
    constraints: RequestConstraints | None = None,
) -> TaskSpec:
    request_lower = request.lower()
    constraints = constraints or derive_request_constraints(request)
    evidence_mode = evidence_mode_from_request(request)
    allow_capabilities = constraints.allow_live
    entities = tuple(dict.fromkeys(getattr(route, "materialized_entities", ()) or ()))
    if getattr(route, "scope", "none") == "multi_entity_comparison" and len(entities) != 2:
        raise ValueError("comparison_requires_two_distinct_entities")
    required_capabilities: list[str] = []
    optional_capabilities: list[str] = []
    identity_contradiction = bool(IDENTITY_CONTRADICTION_REQUEST.search(request))
    use_profile = bool(getattr(route, "use_asset_profile", False))
    use_detection = bool(getattr(route, "use_detection", False))
    use_graph = bool(getattr(route, "use_graph", False))
    use_knowledge = bool(getattr(route, "use_knowledge", False))
    if allow_capabilities and identity_contradiction:
        # Identity contradictions require the two current evidence families that
        # can establish identity and its conflicting classification signals.
        use_profile = True
        use_detection = True
        use_graph = use_graph and bool(TOPOLOGY_EVIDENCE_REQUEST.search(request))
        use_knowledge = use_knowledge and bool(KNOWLEDGE_EVIDENCE_REQUEST.search(request))
    if allow_capabilities and use_profile:
        required_capabilities.append("asset.get_profile")
    if allow_capabilities and use_detection:
        required_capabilities.append("asset.get_detection")
    if allow_capabilities and use_graph:
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
    if allow_capabilities and use_knowledge:
        target = (
            required_capabilities
            if SOURCE_SPECIFIC_KNOWLEDGE.search(request)
            else optional_capabilities
        )
        target.append("knowledge.search")
    capabilities = (*required_capabilities, *optional_capabilities)
    signals = set(getattr(route, "matched_signals", ()) or ())
    focused_identity_verification = (
        identity_contradiction
        and set(capabilities) <= {"asset.get_profile", "asset.get_detection"}
        and not MULTI_STEP_WORDING.search(request)
    )
    multi_step = not focused_identity_verification and (
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
        workflow_mode="direct" if not allow_capabilities else "multi_step" if multi_step else "direct",
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
        temporal_mode=(
            "historical"
            if evidence_mode in {"memory_only", "no_live_refresh"}
            else "compare_previous_current"
            if evidence_mode == "current_verification" and PREVIOUS_CURRENT_COMPARISON_REQUEST.search(request)
            else "current"
        ),
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
