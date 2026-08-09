"""Deterministic evidence requirements, memory sufficiency, and view selection."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from src.core.agent.contracts import EvidenceFact, ExecutionPlan, PlanStep, TaskSpec, ToolResult
from src.core.context.product_views import select_product_views
from src.core.memory.long_term import MAX_MEMORY_STATEMENT_CHARS
from src.core.observability.metrics import get_metrics


logger = logging.getLogger(__name__)

EvidenceClass = Literal[
    "asset_identity", "asset_role", "asset_risk", "asset_identity_protocols",
    "asset_network_activity", "detection_classification", "detection_explanation",
    "detection_contradictions", "detection_similarity", "detection_cluster",
    "detection_raw_behavior", "graph_summary", "graph_neighbors",
    "graph_relationship", "graph_path", "knowledge_background",
]
MemoryDecision = Literal[
    "memory_sufficient", "memory_sufficient_verification_required",
    "live_evidence_required", "contradictory_memory", "memory_unavailable",
]

VOLATILE_EVIDENCE = frozenset({
    "asset_risk", "asset_network_activity", "detection_classification",
    "detection_explanation", "detection_contradictions", "detection_similarity",
    "detection_cluster", "detection_raw_behavior", "graph_summary", "graph_neighbors",
    "graph_relationship", "graph_path",
})
HISTORICAL_WORDS = frozenset({"previously", "historical", "formerly", "was", "past"})


@dataclass(frozen=True)
class EvidenceRequirement:
    evidence_class: EvidenceClass
    capability: str
    entities: tuple[str, ...]
    freshness_class: str
    exhaustive: bool = False
    required: bool = True


@dataclass(frozen=True)
class EvidenceRequirementSet:
    requirements: tuple[EvidenceRequirement, ...]

    def for_capability(self, capability: str, entities: tuple[str, ...] = ()) -> tuple[EvidenceRequirement, ...]:
        return tuple(
            item for item in self.requirements
            if item.capability == capability and (not entities or set(item.entities).intersection(entities))
        )


@dataclass(frozen=True)
class MemoryCoverage:
    requirement: EvidenceRequirement
    memory_ids: tuple[str, ...]
    coverage: bool
    entity_binding: bool
    authority: bool
    freshness: bool
    completeness: bool
    contradiction_free: bool


@dataclass(frozen=True)
class MemorySufficiencyDecision:
    requirement: EvidenceRequirement
    decision: MemoryDecision
    reason_code: str
    memory_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceGap:
    requirement: EvidenceRequirement
    action: Literal["skip", "verify", "live"]
    reason_code: str


@dataclass(frozen=True)
class ViewSelectionDecision:
    capability: str
    entities: tuple[str, ...]
    views: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class EvidenceGapPlan:
    requirements: EvidenceRequirementSet
    decisions: tuple[MemorySufficiencyDecision, ...]
    gaps: tuple[EvidenceGap, ...]
    view_selections: tuple[ViewSelectionDecision, ...] = ()
    skipped_capabilities: tuple[str, ...] = ()


def evidence_refs_from_validated_result(
    requirements: EvidenceRequirementSet,
    result: ToolResult,
    *,
    existing_refs: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Tag only evidence classes supported by one complete validated result."""
    if (
        result.provider == "long_term_memory"
        or result.status != "ok"
        or result.completeness != "complete"
        or result.truncated
        or result.contradictions
        or not result.context_included
        or not result.source_payload_complete
        or not result.projection_usable
    ):
        return tuple(dict.fromkeys(existing_refs))
    matching = tuple(
        requirement
        for requirement in requirements.requirements
        if requirement.capability == result.source_capability
        and requirement.entities == result.entities
    )
    refs = [*existing_refs]
    refs.extend(f"evidence_class_{item.evidence_class}" for item in matching)
    if (
        any(item.exhaustive for item in matching)
        and not result.projection_truncated
        and result.projection_omitted_count == 0
    ):
        refs.append("complete")
    return tuple(dict.fromkeys(refs))


def structured_memory_statement(result: ToolResult, evidence_refs: tuple[str, ...]) -> str | None:
    """Serialize bounded structured model-facing evidence, never assistant prose."""
    evidence_classes = tuple(
        ref.removeprefix("evidence_class_")
        for ref in evidence_refs
        if ref.startswith("evidence_class_")
    )
    if not evidence_classes or result.view_payload is None:
        return None
    statement = json.dumps(
        {
            "source_capability": result.source_capability,
            "entities": list(result.entities),
            "evidence_classes": list(evidence_classes),
            "evidence": result.view_payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return statement if len(statement) <= MAX_MEMORY_STATEMENT_CHARS else None


class EvidenceRequirementPolicy:
    """Derive typed evidence needs from the already validated semantic task."""

    _GRAPH_CLASSES = {
        "graph.get_summary": "graph_summary",
        "graph.get_neighbors": "graph_neighbors",
        "graph.get_relationship": "graph_relationship",
        "graph.compare_assets": "graph_relationship",
        "graph.find_path": "graph_path",
    }

    def derive(self, task: TaskSpec) -> EvidenceRequirementSet:
        requirements: list[EvidenceRequirement] = []
        text = task.request.casefold()
        exhaustive = task.detail_level == "deep" or any(word in text for word in ("exhaustive", "raw", "full payload"))
        historical = any(re.search(rf"\b{re.escape(word)}\b", text) for word in HISTORICAL_WORDS)
        for capability in (*task.required_capabilities, *task.optional_capabilities):
            required = capability in task.required_capabilities
            if capability == "asset.get_profile":
                views = select_product_views("asset_profile", task.request, task.detail_level)
                mapping = {
                    "overview": ("asset_identity", "asset_role"),
                    "identity": ("asset_identity", "asset_role", "asset_identity_protocols"),
                    "security": ("asset_risk",),
                    "network": ("asset_network_activity",),
                    "activity": ("asset_network_activity",),
                    "full": ("asset_identity", "asset_role", "asset_risk", "asset_identity_protocols", "asset_network_activity"),
                }
                classes = tuple(dict.fromkeys(item for view in views for item in mapping[view]))
            elif capability == "asset.get_detection":
                views = select_product_views("detection", task.request, task.detail_level)
                mapping = {
                    "overview": ("detection_classification",),
                    "evidence": ("detection_explanation", "detection_contradictions"),
                    "similarity": ("detection_similarity",),
                    "cluster": ("detection_cluster",),
                    "full": ("detection_raw_behavior",),
                }
                classes = tuple(dict.fromkeys(item for view in views for item in mapping[view]))
            elif capability == "knowledge.search":
                classes = ("knowledge_background",)
            else:
                graph_class = self._GRAPH_CLASSES.get(capability)
                classes = (graph_class,) if graph_class else ()
            for evidence_class in classes:
                freshness = (
                    "historical" if historical else
                    "current_verification" if evidence_class in VOLATILE_EVIDENCE else
                    "revision_based"
                )
                requirements.append(EvidenceRequirement(
                    evidence_class=evidence_class,  # type: ignore[arg-type]
                    capability=capability,
                    entities=task.entities,
                    freshness_class=freshness,
                    exhaustive=exhaustive,
                    required=required,
                ))
        return EvidenceRequirementSet(tuple(requirements))


class MemorySufficiencyGate:
    """Apply all six safety checks without an additional model call."""

    def evaluate(
        self,
        requirements: EvidenceRequirementSet,
        memories: tuple[Any, ...],
    ) -> tuple[MemorySufficiencyDecision, ...]:
        return tuple(self._evaluate_one(requirement, memories) for requirement in requirements.requirements)

    def _evaluate_one(self, requirement: EvidenceRequirement, retrieved: tuple[Any, ...]) -> MemorySufficiencyDecision:
        if not retrieved:
            return MemorySufficiencyDecision(requirement, "memory_unavailable", "memory_unavailable")
        requested = set(requirement.entities)
        entity_bound = [
            item for item in retrieved
            if requested and requested.issubset(set(item.memory.entity_ids))
        ]
        if not entity_bound:
            return MemorySufficiencyDecision(requirement, "live_evidence_required", "memory_entity_mismatch")
        covered = [item for item in entity_bound if self._covers(item.memory, requirement.evidence_class)]
        if not covered:
            return MemorySufficiencyDecision(requirement, "live_evidence_required", "memory_partial")
        authoritative = [
            item for item in covered
            if item.memory.authoritative or (
                requirement.freshness_class == "historical"
                and item.memory.epistemic_status == "historical"
                and item.memory.status == "active"
            )
        ]
        if not authoritative:
            return MemorySufficiencyDecision(requirement, "live_evidence_required", "memory_authority_insufficient")
        memory_ids = tuple(item.memory.memory_id for item in authoritative)
        explicit_conflict = any(
            ref == "contradiction" or ref.startswith("contradicts_")
            for item in authoritative
            for ref in item.memory.evidence_refs
        )
        if explicit_conflict:
            return MemorySufficiencyDecision(requirement, "contradictory_memory", "memory_conflict_live_refresh", memory_ids)
        if requirement.exhaustive and not all("complete" in item.memory.evidence_refs for item in authoritative):
            return MemorySufficiencyDecision(requirement, "live_evidence_required", "memory_incomplete_live_refresh", memory_ids)
        if requirement.freshness_class == "current_verification":
            return MemorySufficiencyDecision(
                requirement,
                "memory_sufficient_verification_required",
                "memory_stale_live_refresh",
                memory_ids,
            )
        if any(item.freshness in {"expired", "inactive"} for item in authoritative):
            return MemorySufficiencyDecision(requirement, "live_evidence_required", "memory_stale_live_refresh", memory_ids)
        reason = "memory_reused_historical" if requirement.freshness_class == "historical" else "memory_reused_authoritative"
        return MemorySufficiencyDecision(requirement, "memory_sufficient", reason, memory_ids)

    def _covers(self, memory: Any, evidence_class: str) -> bool:
        explicit = f"evidence_class_{evidence_class}"
        return explicit in memory.evidence_refs


class ViewSelector:
    """Map unresolved evidence classes to the smallest approved Product views."""

    _PROFILE = {
        "asset_identity": "overview", "asset_role": "overview", "asset_risk": "security",
        "asset_identity_protocols": "identity", "asset_network_activity": "network",
    }
    _DETECTION = {
        "detection_classification": "overview", "detection_explanation": "evidence",
        "detection_contradictions": "evidence", "detection_similarity": "similarity",
        "detection_cluster": "cluster", "detection_raw_behavior": "full",
    }

    def select(self, requirement_items: tuple[EvidenceRequirement, ...]) -> tuple[str, ...]:
        if not requirement_items:
            return ()
        capability = requirement_items[0].capability
        mapping = self._PROFILE if capability == "asset.get_profile" else self._DETECTION
        views = tuple(dict.fromkeys(mapping[item.evidence_class] for item in requirement_items if item.evidence_class in mapping))
        return ("full",) if "full" in views else views


def build_gap_plan(
    requirements: EvidenceRequirementSet,
    decisions: tuple[MemorySufficiencyDecision, ...],
) -> EvidenceGapPlan:
    gaps = tuple(EvidenceGap(
        requirement=item.requirement,
        action="skip" if item.decision == "memory_sufficient" else "verify" if item.decision == "memory_sufficient_verification_required" else "live",
        reason_code=item.reason_code,
    ) for item in decisions)
    return EvidenceGapPlan(requirements=requirements, decisions=decisions, gaps=gaps)


def apply_gap_plan(
    plan: ExecutionPlan,
    gap_plan: EvidenceGapPlan,
) -> tuple[ExecutionPlan, EvidenceGapPlan, list[ToolResult]]:
    """Remove only fully memory-satisfied calls and normalize remaining Product views."""
    selector = ViewSelector()
    steps: list[PlanStep] = []
    skipped_results: list[ToolResult] = []
    selections: list[ViewSelectionDecision] = []
    skipped_capabilities: list[str] = []
    for step in plan.steps:
        entities = tuple(step.arguments.get("entities") or plan.task.entities)
        matching = gap_plan.requirements.for_capability(step.capability, entities)
        matching_gaps = tuple(item for item in gap_plan.gaps if item.requirement in matching)
        if matching_gaps and all(item.action == "skip" for item in matching_gaps):
            skipped_capabilities.append(step.capability)
            decisions = tuple(item for item in gap_plan.decisions if item.requirement in matching)
            memory_ids = tuple(dict.fromkeys(memory_id for item in decisions for memory_id in item.memory_ids))
            skipped_results.append(ToolResult(
                status="ok",
                entities=entities,
                source_capability=step.capability,
                retrieved_at="memory",
                freshness="current",
                completeness="complete",
                facts=(EvidenceFact(step.capability, "Validated memory satisfied this evidence requirement", {"memory_ids": list(memory_ids)}),),
                limitations=("Current live retrieval was skipped by the deterministic memory sufficiency policy.",),
                provider="long_term_memory",
                evidence_type="validated_memory",
                context_included=True,
                context_representation="memory_reuse",
                source_payload_complete=True,
                projection_usable=True,
                selected_views=(),
                step_id=step.id,
            ))
            continue
        if step.capability in {"asset.get_profile", "asset.get_detection"}:
            views = selector.select(matching)
            if views:
                arguments = {**step.arguments, "views": list(views)}
                step = replace(step, arguments=arguments)
                selections.append(ViewSelectionDecision(step.capability, entities, views, "minimum_unresolved_evidence_views"))
        steps.append(step)
    updated_gap = replace(
        gap_plan,
        view_selections=tuple(selections),
        skipped_capabilities=tuple(dict.fromkeys(skipped_capabilities)),
    )
    return replace(plan, steps=tuple(steps)), updated_gap, skipped_results


def log_gap_plan(request_id: str, gap_plan: EvidenceGapPlan) -> None:
    logger.info(
        "event=evidence_requirements_derived request_id=%s required_count=%s evidence_classes=%s",
        request_id,
        len(gap_plan.requirements.requirements),
        ",".join(item.evidence_class for item in gap_plan.requirements.requirements) or "none",
    )
    logger.info(
        "event=memory_sufficiency_evaluated request_id=%s decision_count=%s decisions=%s",
        request_id,
        len(gap_plan.decisions),
        ",".join(item.decision for item in gap_plan.decisions) or "none",
    )
    logger.info(
        "event=evidence_gap_plan_created request_id=%s skip_count=%s live_count=%s verify_count=%s",
        request_id,
        sum(item.action == "skip" for item in gap_plan.gaps),
        sum(item.action == "live" for item in gap_plan.gaps),
        sum(item.action == "verify" for item in gap_plan.gaps),
    )
    metrics = get_metrics()
    decision_map = {
        "memory_sufficient": "reuse",
        "memory_sufficient_verification_required": "verify",
        "contradictory_memory": "conflict",
    }
    for decision in gap_plan.decisions:
        metrics.observe_memory_decision(decision_map.get(decision.decision, "live"))
    for gap in gap_plan.gaps:
        metrics.observe_memory_action(gap.action, gap.requirement.capability)
        if gap.action == "skip":
            continue
        logger.info(
            "event=tool_refresh_required request_id=%s capability=%s evidence_class=%s action=%s reason=%s",
            request_id,
            gap.requirement.capability,
            gap.requirement.evidence_class,
            gap.action,
            gap.reason_code,
        )
