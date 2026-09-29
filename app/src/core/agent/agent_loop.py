"""Deterministic Evidence Ledger updates and adaptive progress gates."""

from __future__ import annotations

from dataclasses import replace
import time
from typing import Any

from src.core.agent.adaptive_evidence import evidence_reference_for_result
from src.core.agent.contracts import (
    AgentCapabilityRequest,
    AgentLoopBudget,
    AgentObservation,
    AuthorityRequirement,
    EvidenceGap,
    EvidenceLedger,
    EvidenceReference,
    PlanStep,
    StopReason,
    TaskSpec,
    ToolResult,
)


MAX_LEDGER_REFERENCES = 24
MAX_REFERENCES_PER_CONTEXT_IDENTITY = 2
MAX_LEDGER_ACTION_FINGERPRINTS = 24
MAX_LEDGER_EVIDENCE_FINGERPRINTS = 24
MAX_STRUCTURED_CANDIDATES = 20
MAX_NO_PROGRESS_TURNS = 2


def initialize_ledger(task: TaskSpec, gap_plan: Any) -> EvidenceLedger:
    grouped: dict[tuple[str, tuple[str, ...]], list[Any]] = {}
    decisions = tuple(getattr(gap_plan, "decisions", ()) or ())
    for requirement in tuple(getattr(getattr(gap_plan, "requirements", None), "requirements", ()) or ()):
        grouped.setdefault((requirement.capability, requirement.entities), []).append(requirement)
    gaps: list[EvidenceGap] = []
    for index, ((capability, entities), requirements) in enumerate(grouped.items(), start=1):
        matching = [item for item in decisions if item.requirement in requirements]
        satisfied = bool(matching) and all(item.decision == "memory_sufficient" for item in matching)
        freshness = {item.freshness_class for item in requirements}
        temporal = (
            "current" if "current_verification" in freshness
            else "historical" if freshness == {"historical"}
            else "either"
        )
        gaps.append(EvidenceGap(
            gap_id=f"gap-{index}-{capability.replace('.', '-')}",
            dimension="+".join(item.evidence_class for item in requirements)[:160],
            importance="required" if any(item.required for item in requirements) else "optional",
            status="satisfied" if satisfied else "open",
            temporal_requirement=temporal,  # type: ignore[arg-type]
            authorized_capabilities=(capability,),
            authority_requirement=_authority(capability, temporal),
            entities=entities,
        ))
    return EvidenceLedger(
        authorized_entities=task.entities,
        gaps=tuple(gaps),
        capability_coverage=tuple(
            (gap.authorized_capabilities[0], "memory")
            for gap in gaps
            if gap.status == "satisfied"
        ),
    )


def update_ledger(
    ledger: EvidenceLedger,
    task: TaskSpec,
    results: tuple[ToolResult, ...],
    *,
    action_fingerprints: tuple[str, ...] = (),
    action_steps: tuple[PlanStep, ...] = (),
) -> EvidenceLedger:
    attempted = tuple(dict.fromkeys((*ledger.attempted_capabilities, *(item.source_capability for item in results))))
    candidates = list(ledger.structured_candidates)
    coverage = list(ledger.capability_coverage)
    facts = list(ledger.important_facts)
    contradictions = list(ledger.contradictions)
    limitations = list(ledger.coverage_limitations)
    failures = list(ledger.failures)
    step_by_id = {step.id: step for step in action_steps}
    fingerprint_by_id = {
        step.id: fingerprint
        for step, fingerprint in zip(action_steps, action_fingerprints)
    }
    new_references: list[EvidenceReference] = []
    for result in results:
        coverage.append((result.source_capability, result.status))
        entity_label = ",".join(result.entities[:2]) or "set"
        facts.append(
            f"{result.source_capability}:{entity_label}:status={result.status}:"
            f"freshness={result.freshness}:complete={result.completeness}:usable={str(result.projection_usable).lower()}"
        )
        contradictions.extend(
            f"{result.source_capability}:contradiction:{index}"
            for index, _item in enumerate(result.contradictions[:8], start=1)
        )
        limitations.extend(f"{result.source_capability}:{item}"[:240] for item in result.limitations[:8])
        if result.status in {"unavailable", "not_configured", "invalid"}:
            failures.append(f"{result.source_capability}:{result.status}")
        if result.structured_asset_set is not None:
            for row in tuple(getattr(result.structured_asset_set, "rows", ()) or ())[:20]:
                identity = str(row.get("ip") or "").strip()
                if identity and identity not in candidates and len(candidates) < MAX_STRUCTURED_CANDIDATES:
                    candidates.append(identity)
        step = step_by_id.get(result.step_id)
        new_references.append(evidence_reference_for_result(
            result,
            ledger.gaps,
            step=step,
            action_fingerprint=fingerprint_by_id.get(result.step_id, ""),
        ))

    references = _merge_references(ledger.evidence_references, tuple(new_references))
    evidence_fingerprints = tuple(dict.fromkeys(
        (*ledger.evidence_fingerprints, *(item.semantic_fingerprint for item in new_references))
    ))[-MAX_LEDGER_EVIDENCE_FINGERPRINTS:]
    gaps = [_updated_gap(gap, tuple(new_references), attempted) for gap in ledger.gaps]
    if candidates and task.post_search_requirements is not None:
        gaps.extend(_post_search_gaps(task, tuple(candidates), tuple(gaps)))
    return EvidenceLedger(
        authorized_entities=ledger.authorized_entities,
        structured_candidates=tuple(candidates),
        attempted_capabilities=attempted,
        capability_coverage=tuple(coverage[-40:]),
        important_facts=tuple(dict.fromkeys(facts[-24:])),
        contradictions=tuple(dict.fromkeys(contradictions[-16:])),
        gaps=tuple(gaps),
        coverage_limitations=tuple(dict.fromkeys(limitations[-16:])),
        failures=tuple(dict.fromkeys(failures[-16:])),
        action_fingerprints=tuple(dict.fromkeys(
            (*ledger.action_fingerprints, *action_fingerprints)
        ))[-MAX_LEDGER_ACTION_FINGERPRINTS:],
        evidence_fingerprints=evidence_fingerprints,
        evidence_references=references,
    )


def _merge_references(
    existing: tuple[EvidenceReference, ...],
    incoming: tuple[EvidenceReference, ...],
) -> tuple[EvidenceReference, ...]:
    references = list(existing)
    for reference in incoming:
        if any(item.reference_id == reference.reference_id for item in references):
            continue
        same_context = [
            index for index, item in enumerate(references)
            if item.context_identity == reference.context_identity
        ]
        while len(same_context) >= MAX_REFERENCES_PER_CONTEXT_IDENTITY:
            references.pop(same_context[0])
            same_context = [
                index for index, item in enumerate(references)
                if item.context_identity == reference.context_identity
            ]
        references.append(reference)
    return tuple(references[-MAX_LEDGER_REFERENCES:])


def build_observation(
    *,
    turn: int,
    requests: tuple[AgentCapabilityRequest, ...],
    results: tuple[ToolResult, ...],
    previous: EvidenceLedger,
    current: EvidenceLedger,
    context_input_tokens_before: int = 0,
    context_input_tokens_after: int = 0,
) -> AgentObservation:
    previous_status = {item.gap_id: item.status for item in previous.gaps}
    new_coverage = tuple(
        item.gap_id for item in current.gaps
        if item.status == "satisfied" and previous_status.get(item.gap_id) != "satisfied"
    )
    new_contradictions = tuple(
        item for item in current.contradictions if item not in previous.contradictions
    )
    resolved_contradictions = tuple(
        item for item in previous.contradictions if item not in current.contradictions
    )
    previous_ids = {item.reference_id for item in previous.evidence_references}
    previous_by_context = {
        item.context_identity: item.semantic_fingerprint
        for item in previous.evidence_references
    }
    new_references = tuple(
        item.reference_id
        for item in current.evidence_references
        if item.material
        and item.reference_id not in previous_ids
        and item.context_identity not in previous_by_context
    )
    changed_references = tuple(
        item.reference_id
        for item in current.evidence_references
        if item.material
        and item.reference_id not in previous_ids
        and item.context_identity in previous_by_context
        and previous_by_context[item.context_identity] != item.semantic_fingerprint
    )
    material_progress = bool(
        new_coverage
        or new_references
        or changed_references
        or new_contradictions
        or resolved_contradictions
        or current.structured_candidates != previous.structured_candidates
    )
    return AgentObservation(
        turn=turn,
        capability_requests=requests,
        result_references=(*new_references, *changed_references),
        status_summary=tuple((item.source_capability, item.status) for item in results),
        new_coverage=new_coverage,
        new_contradictions=new_contradictions,
        remaining_gap_ids=tuple(item.gap_id for item in current.gaps if item.status == "open"),
        material_progress=material_progress,
        tool_call_count=len(results),
        new_evidence_references=new_references,
        changed_evidence_references=changed_references,
        resolved_contradictions=resolved_contradictions,
        budget_delta=(("capability_calls", len(results)),),
        evidence_reference_count=len(current.evidence_references),
        context_input_tokens_before=context_input_tokens_before,
        context_input_tokens_after=context_input_tokens_after,
    )


def evaluate_progress(
    ledger: EvidenceLedger,
    budget: AgentLoopBudget,
    *,
    consecutive_no_progress: int = 0,
) -> StopReason | None:
    required = tuple(item for item in ledger.gaps if item.importance == "required")
    open_required = tuple(item for item in required if item.status == "open")
    if required and not open_required:
        if any(item.status in {"unavailable", "blocked"} for item in required):
            return "answer_with_limitations"
        return "evidence_sufficient"
    if not ledger.gaps:
        return "goal_satisfied"
    if consecutive_no_progress >= MAX_NO_PROGRESS_TURNS:
        return "no_useful_action"
    if time.monotonic() >= budget.deadline_monotonic:
        return "budget_exhausted"
    if budget.technical_failures >= budget.max_technical_failures:
        return "technical_failure_ceiling"
    if budget.remaining_turns <= 0:
        return "budget_exhausted"
    if budget.remaining_llm_calls <= 1:
        return "llm_budget_exhausted"
    if budget.remaining_capability_calls <= 0:
        return "tool_budget_exhausted"
    return None


def _updated_gap(
    gap: EvidenceGap,
    references: tuple[EvidenceReference, ...],
    attempted: tuple[str, ...],
) -> EvidenceGap:
    if gap.status != "open":
        return gap
    matching = tuple(
        item for item in references
        if item.source_capability in gap.authorized_capabilities
    )
    acceptable = tuple(item for item in matching if gap.gap_id in item.covered_gap_ids)
    if acceptable:
        return replace(gap, status="satisfied")
    if matching and all(capability in attempted for capability in gap.authorized_capabilities):
        return replace(gap, status="unavailable")
    return gap


def _post_search_gaps(
    task: TaskSpec,
    candidates: tuple[str, ...],
    existing: tuple[EvidenceGap, ...],
) -> tuple[EvidenceGap, ...]:
    requirements = task.post_search_requirements
    if requirements is None:
        return ()
    existing_dimensions = {item.dimension for item in existing}
    definitions: list[tuple[str, tuple[str, ...], AuthorityRequirement]] = []
    for capability in requirements.entity_capabilities:
        capability_name = capability.rsplit(".", 1)[-1].removeprefix("get_")
        definitions.append((f"post_search_{capability_name}", (capability,), "product_current"))
    if requirements.requires_focal_graph:
        definitions.append(("post_search_graph", ("graph.get_summary", "graph.compare_assets"), "graph_projection"))
    if requirements.requires_knowledge:
        definitions.append(("post_search_knowledge", ("knowledge.search",), "knowledge_reference"))
    return tuple(
        EvidenceGap(
            gap_id=f"gap-post-{index}-{dimension}",
            dimension=dimension,
            importance="required",
            status="open",
            temporal_requirement="either" if authority == "knowledge_reference" else "current",
            authorized_capabilities=capabilities,
            authority_requirement=authority,
            entities=(),
        )
        for index, (dimension, capabilities, authority) in enumerate(definitions, start=1)
        if dimension not in existing_dimensions and candidates
    )


def _authority(capability: str, temporal: str) -> AuthorityRequirement:
    if capability.startswith("asset."):
        return "product_current"
    if capability.startswith("graph."):
        return "graph_projection"
    if capability == "knowledge.search":
        return "knowledge_reference"
    if temporal == "historical":
        return "memory_historical"
    return "authorized_source"
