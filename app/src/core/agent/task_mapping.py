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
    TaskEnvelope,
    TaskSpec,
    TurnPolicy,
)
from src.core.context.models import EntityResolution, ResolvedEntity
from src.core.context.product_views import select_product_views
from src.core.memory.routing_state import SessionRoutingState


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
    r"what\s+(?:do|did)\s+you\s+remember|what\s+you\s+(?:already\s+)?remember|do\s+you\s+remember|"
    r"what\s+(?:did\s+)?i\s+ask(?:ed)?\s+you\s+to\s+remember|"
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
    r"historical\s+(?:summary|finding)|previous(?:ly)?\s+(?:validated|established|findings?|conclusions?)|"
    r"last\s+validated|what\s+did\s+we\s+(?:know|conclude)\s+about|"
    r"based\s+on\s+what\s+we\s+already\s+knew|from\s+our\s+previous\s+investigation|"
    r"(?:tell\s+me\s+about|what\s+do\s+you\s+remember\s+from)\s+(?:all\s+)?(?:the\s+)?"
    r"(?:assets?|network\s+analysis|investigations?))\b",
    re.IGNORECASE,
)

NO_LIVE_EVIDENCE_REQUEST = re.compile(
    r"\b(?:before\s+making\s+any\s+live\s+provider\s+calls|without\s+refreshing|"
    r"without\s+(?:fetching|calling|using)\s+(?:any\s+)?(?:live\s+)?evidence|"
    r"without\s+(?:performing\s+)?(?:any\s+)?live\s+(?:lookup|check|retrieval)|"
    r"without\s+checking\s+(?:any\s+)?current\s+(?:information|status|state|systems?)|"
    r"do\s+not\s+(?:retrieve|look\s+anything\s+up)\b|"
    r"do\s+not\s+(?:use|call|retrieve|check|look\s+up)\s+(?:anything|any\s+)?(?:current\s+|live\s+)?(?:product|detection|graph|knowledge|evidence|providers?|refresh|systems?|status|state|data|lookup|tools?)?|"
    r"don't\s+use\s+live|do\s+not\s+refresh|don't\s+refresh|no\s+live\s+(?:provider|evidence|refresh)|"
    r"without\s+(?:using\s+)?live\s+(?:sources?|data)|do\s+not\s+use\s+live\s+(?:sources?|data)|"
    r"use\s+only\s+memory|use\s+only\s+what\s+(?:you|we)\s+(?:already\s+)?(?:remember|discussed|concluded|knew)|using\s+only\s+stored\s+(?:conversation\s+)?context|memory\s+only|"
    r"based\s+only\s+on\s+what\s+we\s+(?:discussed|knew|established)|historical\s+only)\b",
    re.IGNORECASE,
)

MEMORY_WRITE_REQUEST = re.compile(
    r"\b(?:remember\s*(?:that\b|[,;:]\s*(?:i(?:'m|m|\s+am)|my\s+(?:analyst\s+)?name\s+is|call\s+me))|"
    r"note\s+that|keep\s+in\s+mind|for\s+this\s+investigation\s+remember)\b|"
    r"\b(?:remember|keep|retain|store)\b.{0,160}\b(?:for\s+(?:this|the)\s+(?:conversation|investigation)|"
    r"in\s+(?:this|the)\s+(?:conversation|investigation)|my\s+name|tag|owner\s+validation|analyst\s+note|contradiction)\b",
    re.IGNORECASE,
)

CURRENT_EVIDENCE_REQUEST = re.compile(
    r"\b(?:fresh|current|currently|right\s+now|still|verify\s+(?:now|again)|recheck|refresh|latest|"
    r"live\s+(?:evidence|data|state))\b|\bverify\b.{0,40}\b(?:now|again)\b",
    re.IGNORECASE,
)

NO_LIVE_HISTORICAL_REQUEST = re.compile(
    r"\b(?:memory\s+only|use\s+only\s+memory|use\s+only\s+what\s+(?:you|we)\s+(?:already\s+)?(?:remember|discussed|concluded|knew)|historical\s+only|based\s+only\s+on\s+what\s+we\s+"
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

BROAD_CONVERSATION_RECALL_REQUEST = re.compile(
    r"\b(?:what\s+(?:do\s+you\s+)?(?:know|remember)|summari[sz]e|recall|tell\s+me)\b"
    r".{0,100}\b(?:our\s+chats?|our\s+conversations?|chat\s+history|conversation\s+history|"
    r"(?:this|our)\s+conversation|(?:all\s+)?previous\s+investigations?|"
    r"everything\s+we(?:'ve|\s+have)?\s+discussed)\b|"
    r"\b(?:everything|all)\s+(?:you\s+)?(?:remember|established|from)\b.{0,100}"
    r"\b(?:this|our)\s+conversation\b|"
    r"\bwhat\s+(?:investigations?|analyses)\s+have\s+we\s+(?:done|performed)\b|"
    r"\bwhat\s+did\s+we\s+talk\s+about(?:\s+earlier)?\b|"
    r"\bsummari[sz]e\s+what\s+we\s+have\s+done\s+so\s+far\b",
    re.IGNORECASE,
)

EXPLICIT_RECALL_ENTITY_SCOPE = re.compile(
    r"\b(?:about|regarding|for)\s+(?:(?:this|that|the\s+selected)\s+"
    r"(?:asset|host|node)|(?:\d{1,3}\.){3}\d{1,3})\b",
    re.IGNORECASE,
)

RecallClassification = Literal["none", "explicit_memory", "historical_summary"]


def is_broad_conversation_recall(request: str) -> bool:
    """Return true only for an explicit request to recall the whole thread."""
    return bool(
        BROAD_CONVERSATION_RECALL_REQUEST.search(request)
        and not EXPLICIT_RECALL_ENTITY_SCOPE.search(request)
    )


def derive_task_envelope(
    entities: EntityResolution,
    constraints: RequestConstraints,
    turn_policy: TurnPolicy,
    routing_state: SessionRoutingState,
) -> TaskEnvelope:
    """Freeze deterministic task authority before any semantic router runs."""
    resolved_entities = tuple(item.value for item in entities.entities)
    # Let validation produce the established bounded clarification for an
    # oversized request; do not turn it into an envelope-construction failure.
    ordered_entities = resolved_entities if len(resolved_entities) <= 2 else ()
    previous_pair_comparison = (
        turn_policy.target == "active_pair"
        and routing_state.previous_scope == "multi_entity_comparison"
    )
    comparison_required = (
        len(ordered_entities) == 2
        and (
            turn_policy.operation == "compare_previous_current"
            or entities.reference_type == "compare_with_reference"
            or previous_pair_comparison
        )
    )
    temporal_scope: TemporalMode = (
        "historical"
        if constraints.memory_only or not constraints.allow_live
        else "compare_previous_current"
        if turn_policy.operation == "compare_previous_current"
        else "current"
    )
    task_family = (
        "asset_comparison"
        if comparison_required
        else "memory_recall"
        if turn_policy.operation == "memory_recall"
        else "asset_investigation"
        if resolved_entities
        else "general"
    )
    return TaskEnvelope(
        ordered_entities=ordered_entities,
        reference_type=entities.reference_type or "none",
        operation=turn_policy.operation,
        temporal_scope=temporal_scope,
        freshness_requirement=(
            "current_required" if constraints.require_current else "historical_only"
            if not constraints.allow_live else "current_when_available"
        ),
        allow_live=constraints.allow_live,
        require_current=constraints.require_current,
        comparison_required=comparison_required,
        task_family=task_family,
        reason_codes=tuple(dict.fromkeys((*constraints.reason_codes, *turn_policy.reason_codes))),
    )


def enforce_task_envelope(route: Any, envelope: TaskEnvelope | None) -> Any:
    """Restore deterministic task authority after semantic/fallback routing."""
    if envelope is None:
        return route
    updates: dict[str, Any] = {}
    if envelope.ordered_entities:
        updates.update(
            materialized_entities=envelope.ordered_entities,
            materialized_entity_count=len(envelope.ordered_entities),
        )
    if envelope.comparison_required:
        updates.update(
            use_graph=True,
            intent="graph_relationships",
            scope="multi_entity_comparison",
            direction="both",
            depth=1,
            requires_multiple_entities=True,
            relationship_mode="compare",
            route_normalized=True,
            route_normalization_reason="task_envelope_preserves_pair_comparison",
        )
    return replace(route, **updates) if updates else route


def classify_historical_recall(request: str) -> RecallClassification:
    """Classify only clear historical-memory intent; ordinary asset questions remain live."""
    if EXPLICIT_MEMORY_RECALL_REQUEST.search(request):
        return "explicit_memory"
    if HISTORICAL_SUMMARY_REQUEST.search(request):
        return "historical_summary"
    return "none"


def historical_evidence_classes_for_request(request: str) -> tuple[str, ...]:
    """Return complementary exact LTM classes only for deterministic historical recall."""
    if classify_historical_recall(request) == "none":
        return ()
    text = request.casefold()
    classes: list[str] = []
    if any(word in text for word in ("identity", "role", "profile", "asset")):
        classes.extend(("asset_identity", "asset_role"))
    if any(word in text for word in ("classification", "detection", "classifier")):
        classes.append("detection_classification")
    return tuple(dict.fromkeys(classes))


def derive_request_constraints(request: str) -> RequestConstraints:
    """Resolve live-evidence and memory authority without an LLM."""
    no_live = bool(NO_LIVE_EVIDENCE_REQUEST.search(request))
    recall_classification = classify_historical_recall(request)
    broad_recall = is_broad_conversation_recall(request)
    recall = recall_classification != "none" or broad_recall
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
        reasons.append(
            "deterministic_memory_recall:thread"
            if broad_recall
            else f"deterministic_memory_recall:{recall_classification}"
        )
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

def derive_turn_policy(
    request: str,
    constraints: RequestConstraints,
    entities: EntityResolution,
    routing_state: SessionRoutingState,
) -> TurnPolicy:
    """Resolve operation, target authority, router need, and episode mutation once."""
    resolved = tuple(item.value for item in entities.entities)
    source = entities.entities[0].source if entities.entities else ""
    active = tuple(routing_state.active_entities)

    def target_for(values: tuple[str, ...], entity_source: str) -> str:
        if len(values) == 2:
            return "active_pair"
        if entity_source == "message":
            return "explicit_entity"
        if entity_source == "ui":
            return "ui_entity"
        if len(values) == 2:
            return "active_pair"
        if values:
            return "active_entity"
        return "none"

    if entities.reference_suppressed:
        return TurnPolicy(
            operation="topic_detach",
            target="none",
            requires_domain_router=True,
            episode_transition="detach",
            reason_codes=("explicit_topic_detachment",),
        )

    broad_recall = is_broad_conversation_recall(request)
    if broad_recall:
        return TurnPolicy(
            operation="memory_write" if constraints.memory_write else "memory_recall",
            target="conversation",
            target_entities=(),
            requires_domain_router=False,
            episode_transition="keep",
            operational_state_mutation_allowed=False,
            reason_codes=("deterministic_conversation_memory_recall",),
        )

    if constraints.memory_only or not constraints.allow_live:
        values = resolved or active
        return TurnPolicy(
            operation="memory_write" if constraints.memory_write else "memory_recall",
            target=(target_for(values, source) if resolved else "conversation"),  # type: ignore[arg-type]
            target_entities=values,
            requires_domain_router=False,
            episode_transition="keep",
            operational_state_mutation_allowed=False,
            reason_codes=("deterministic_conversation_memory_recall",) if broad_recall else constraints.reason_codes,
        )

    if constraints.require_current:
        values = resolved or active
        source_target = target_for(resolved, source) if resolved else (
            "active_pair" if len(active) == 2 else "active_entity" if active else "none"
        )
        explicit_switch = bool(
            resolved
            and source in {"message", "ui"}
            and tuple(resolved) != tuple(active)
        )
        return TurnPolicy(
            operation=(
                "compare_previous_current"
                if PREVIOUS_CURRENT_COMPARISON_REQUEST.search(request)
                else "current_verification"
            ),
            target=source_target,  # type: ignore[arg-type]
            target_entities=values,
            requires_domain_router=True,
            episode_transition="switch" if explicit_switch else "keep",
            reason_codes=(*constraints.reason_codes, "state_aware_current_verification"),
        )

    if resolved:
        transition = (
            "switch"
            if source in {"message", "ui"} and tuple(resolved) != tuple(active)
            else "keep"
        )
        return TurnPolicy(
            operation="follow_up" if source == "conversation" else "new_task",
            target=target_for(resolved, source),  # type: ignore[arg-type]
            target_entities=resolved,
            requires_domain_router=True,
            episode_transition=transition,
            reason_codes=("resolved_target_authority",),
        )
    return TurnPolicy(
        operation="new_task",
        target="none",
        requires_domain_router=True,
        episode_transition="keep",
        reason_codes=("no_operational_target_established",),
    )


def materialize_turn_policy_target(
    entities: EntityResolution,
    policy: TurnPolicy,
) -> EntityResolution:
    """Materialize only a target already authorized by deterministic turn policy."""
    if entities.entities or not policy.target_entities or policy.target not in {"active_entity", "active_pair"}:
        return entities
    materialized = [
        ResolvedEntity(type="ip", value=value, source="conversation")
        for value in policy.target_entities
    ]
    return EntityResolution(
        status="resolved",
        entities=materialized,
        primary_entity=materialized[0] if len(materialized) == 1 else None,
        entity_mode="single" if len(materialized) == 1 else "multiple",
        candidate_count=len(materialized),
        explicit_candidate_count=entities.explicit_candidate_count,
        valid_entity_count=len(materialized),
        reference_detected=True,
        reference_type="state_aware_turn_policy",
        reference_suppressed=False,
        subnet_constraints=entities.subnet_constraints,
        unsupported_constraints=entities.unsupported_constraints,
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
    turn_policy: TurnPolicy | None = None,
    task_envelope: TaskEnvelope | None = None,
) -> TaskSpec:
    request_lower = request.lower()
    constraints = constraints or derive_request_constraints(request)
    evidence_mode = evidence_mode_from_request(request)
    if turn_policy is not None and turn_policy.operation == "memory_recall" and evidence_mode == "normal":
        evidence_mode = "no_live_refresh"
    allow_capabilities = constraints.allow_live
    route = enforce_task_envelope(route, task_envelope)
    entities = task_envelope.ordered_entities if task_envelope and task_envelope.ordered_entities else tuple(
        dict.fromkeys(getattr(route, "materialized_entities", ()) or ())
    )
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
    known_routine = bool(entities) and set(capabilities) <= {
        "asset.get_profile",
        "asset.get_detection",
        "graph.get_summary",
        "graph.get_neighbors",
        "graph.compare_assets",
        "graph.find_path",
        "graph.get_relationship",
        "knowledge.search",
    }
    routine_direct = known_routine and (
        bool(
            task_envelope
            and task_envelope.comparison_required
            and getattr(route, "decision_source", "") == "deterministic_fallback"
        )
        or not bool(task_envelope and task_envelope.comparison_required)
        and not bool(MULTI_STEP_WORDING.search(request))
    )
    multi_step = not routine_direct and not focused_identity_verification and (
        len(capabilities) >= 3
        or "security_or_anomaly" in signals
        or bool(MULTI_STEP_WORDING.search(request))
    )
    return TaskSpec(
        request=request,
        intent=(
            "memory_recall"
            if evidence_mode == "memory_only" or (turn_policy is not None and turn_policy.operation == "memory_recall")
            else str(getattr(route, "intent", "unclear"))
        ),
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
