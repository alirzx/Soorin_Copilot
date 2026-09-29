"""Canonical request-local evidence identity and conservative reuse rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from typing import Any, Iterable, Mapping

from src.core.agent.context_identity import (
    canonical_action_fingerprint,
    context_result_identity,
    identity_for_tool_result,
    knowledge_actions_equivalent,
    knowledge_query_term_hashes,
)
from src.core.agent.contracts import (
    EvidenceGap,
    EvidenceReference,
    PlanStep,
    TaskSpec,
    ToolResult,
)
from src.core.context.compaction import current_evidence_projections
from src.core.graph.structured import semantic_query_identity


EVIDENCE_FINGERPRINT_SCHEMA = "agent-evidence-fingerprint-v1"
EVIDENCE_REFERENCE_SCHEMA = "agent-evidence-reference-v1"
_REUSABLE_STATUSES = frozenset({"ok", "empty", "not_found"})
_FAILED_STATUSES = frozenset({"unavailable", "invalid", "not_configured"})
_DETAIL_RANK = {"brief": 0, "standard": 1, "deep": 2}


@dataclass(frozen=True)
class EquivalentEvidence:
    reference_id: str
    reason: str


def evidence_reference_for_result(
    result: ToolResult,
    gaps: tuple[EvidenceGap, ...],
    *,
    step: PlanStep | None = None,
    action_fingerprint: str = "",
) -> EvidenceReference:
    """Build bounded metadata from normalized evidence without retaining payloads."""
    context = _provider_context(result)
    receipt = result.evidence_receipt
    structured = result.structured_asset_set
    scope = str(getattr(receipt, "scope", "") or context.get("requested_scope") or context.get("scope") or "none")
    direction = str(getattr(receipt, "direction", "") or context.get("direction") or "none")
    depth = int(getattr(receipt, "depth", 0) or context.get("depth") or 0)
    relationship_mode = str(context.get("relationship_mode") or "none")
    active_graph_version = str(
        getattr(structured, "active_graph_version", "")
        or context.get("active_graph_version")
        or ""
    )
    context_identity = str(result.context_identity or "")
    if not context_identity:
        context_identity = identity_for_tool_result(result)
    if result.source_capability.startswith("graph.") and structured is None:
        context_identity = context_result_identity(
            result.source_capability,
            result.entities,
            scope=scope,
            direction=direction,
            depth=depth,
            relationship_mode=relationship_mode,
        )
    limitation_flags = _limitation_flags(result)
    reusable = _is_reusable(result, active_graph_version=active_graph_version)
    temporal = _temporal_class(result)
    authority = _authority_class(result)
    semantic_fingerprint = evidence_semantic_fingerprint(result)
    query_terms = (
        knowledge_query_term_hashes(str(step.arguments.get("query", "")))
        if step is not None and step.capability == "knowledge.search"
        else ()
    )
    action_fingerprint = action_fingerprint or (
        canonical_action_fingerprint(step) if step is not None else ""
    )
    matching = tuple(
        gap for gap in gaps
        if result.source_capability in gap.authorized_capabilities
        and _entity_scope_matches(result.entities, gap.entities)
    )
    evidence_classes = tuple(dict.fromkeys(
        evidence_class
        for gap in matching
        for evidence_class in gap.dimension.split("+")
        if evidence_class
    ))
    base = EvidenceReference(
        reference_id="",
        context_identity=context_identity,
        source_capability=result.source_capability,
        entities=result.entities,
        evidence_classes=evidence_classes,
        covered_gap_ids=(),
        status=result.status,
        freshness=result.freshness,
        completeness=result.completeness,
        authority_class=authority,
        temporal_class=temporal,
        projection_schema_version=str(
            getattr(receipt, "schema_version", "")
            or result.projection_schema_version
            or getattr(structured, "schema_version", "")
            or "evidence-reference-v1"
        ),
        semantic_fingerprint=semantic_fingerprint,
        canonical_action_fingerprint=action_fingerprint,
        structured_query_identity=str(getattr(structured, "query_identity", "") or ""),
        semantic_query_identity=str(
            result.semantic_query_id
            or _structured_semantic_identity(structured)
            or ""
        ),
        active_graph_version=active_graph_version,
        selected_views=tuple(result.selected_views),
        detail=result.detail,
        purpose=result.purpose,
        scope=scope,
        direction=direction,
        depth=depth,
        relationship_mode=relationship_mode,
        query_term_hashes=query_terms,
        material_limitation_flags=limitation_flags,
        source_payload_complete=result.source_payload_complete,
        projection_usable=result.projection_usable,
        reusable=reusable,
        material=reusable or bool(result.contradictions),
    )
    covered = tuple(gap.gap_id for gap in matching if reference_covers_gap(base, gap))
    reference_source = json.dumps(
        {
            "schema": EVIDENCE_REFERENCE_SCHEMA,
            "context_identity": context_identity,
            "semantic_fingerprint": semantic_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    reference_id = f"{EVIDENCE_REFERENCE_SCHEMA}:" + hashlib.sha256(reference_source).hexdigest()
    return replace(base, reference_id=reference_id, covered_gap_ids=covered)


def evidence_semantic_fingerprint(result: ToolResult) -> str:
    """Hash bounded semantic evidence while excluding operational metadata."""
    structured = result.structured_asset_set
    if structured is not None:
        projection: Any = asdict(structured)
        projection.pop("retrieved_at", None)
        projection.pop("limitations", None)
    else:
        current = current_evidence_projections((result,), owner_id="adaptive-request")
        if current:
            projection = [
                {
                    "capability": item.capability,
                    "entities": list(item.entity_ids or (item.entity,)),
                    "view": item.view,
                    "schema_version": item.schema_version,
                    "payload": item.payload,
                    "complete": item.complete,
                    "scope": item.scope,
                    "direction": item.direction,
                    "depth": item.depth,
                }
                for item in current
            ]
        elif result.source_capability == "knowledge.search":
            projection = {
                "query_identity": result.normalized_query_hash,
                "purpose": result.purpose,
                "citation_ids": sorted(
                    str(item.get("chunk_id") or "")
                    for item in result.citations
                    if item.get("chunk_id")
                ),
                "fact_digests": _fact_digests(result),
            }
        else:
            projection = {"fact_digests": _fact_digests(result)}
    payload = {
        "schema": EVIDENCE_FINGERPRINT_SCHEMA,
        "capability": result.source_capability,
        "entities": _canonical_entities(result.source_capability, result.entities),
        "selected_views": sorted(dict.fromkeys(result.selected_views)),
        "projection_schema_version": (
            result.projection_schema_version
            or str(getattr(result.evidence_receipt, "schema_version", "") or "")
            or str(getattr(structured, "schema_version", "") or "")
        ),
        "status": result.status,
        "completeness": result.completeness,
        "truncated": result.truncated,
        "projection_truncated": result.projection_truncated,
        "active_graph_version": _active_graph_version(result),
        "projection": projection,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{EVIDENCE_FINGERPRINT_SCHEMA}:" + hashlib.sha256(canonical).hexdigest()


def reference_covers_gap(reference: EvidenceReference, gap: EvidenceGap) -> bool:
    if reference.source_capability not in gap.authorized_capabilities or not reference.reusable:
        return False
    if not _entity_scope_matches(reference.entities, gap.entities):
        return False
    if gap.temporal_requirement == "current" and reference.temporal_class != "current":
        return False
    if gap.temporal_requirement == "historical" and reference.temporal_class not in {"historical", "current"}:
        return False
    if gap.authority_requirement != "authorized_source" and reference.authority_class != gap.authority_requirement:
        return False
    return True


def equivalent_evidence_for_step(
    step: PlanStep,
    gap: EvidenceGap,
    task: TaskSpec,
    references: tuple[EvidenceReference, ...],
    *,
    action_fingerprint: str | None = None,
) -> EquivalentEvidence | None:
    """Return a proven same-request substitute; never select a new action."""
    fingerprint = action_fingerprint or canonical_action_fingerprint(step)
    requested_entities = tuple(str(item) for item in step.arguments.get("entities", ()))
    for reference in reversed(references):
        if reference.source_capability != step.capability:
            continue
        if not _entity_scope_matches(reference.entities, requested_entities):
            continue
        if reference.canonical_action_fingerprint == fingerprint and reference.status in _FAILED_STATUSES:
            return EquivalentEvidence(reference.reference_id, "repeated_failed_action")
        if not reference_covers_gap(reference, gap):
            continue
        if reference.canonical_action_fingerprint == fingerprint:
            if step.capability.startswith("graph.") and not reference.active_graph_version:
                continue
            return EquivalentEvidence(reference.reference_id, "equivalent_evidence_already_available")
        if step.capability in {"asset.get_profile", "asset.get_detection"}:
            requested_views = set(str(item) for item in step.arguments.get("views", ()))
            available_views = set(reference.selected_views)
            requested_detail = str(step.arguments.get("detail") or "standard")
            if (
                requested_views <= available_views
                and _DETAIL_RANK.get(reference.detail, -1) >= _DETAIL_RANK.get(requested_detail, 1)
            ):
                return EquivalentEvidence(reference.reference_id, "equivalent_evidence_already_available")
        elif step.capability in {"graph.search_assets", "graph.aggregate_assets"}:
            expected_semantic = semantic_query_identity(task.structured_query) if task.structured_query is not None else ""
            if (
                expected_semantic
                and reference.semantic_query_identity == expected_semantic
                and reference.active_graph_version
            ):
                return EquivalentEvidence(reference.reference_id, "equivalent_evidence_already_available")
        elif step.capability == "knowledge.search":
            terms = knowledge_query_term_hashes(str(step.arguments.get("query", "")))
            purpose = str(step.arguments.get("purpose") or "interpret_evidence")
            if knowledge_actions_equivalent(
                purpose,
                terms,
                reference.purpose,
                reference.query_term_hashes,
            ):
                return EquivalentEvidence(reference.reference_id, "equivalent_evidence_already_available")
    return None


def references_semantically_equivalent(
    first: EvidenceReference,
    second: EvidenceReference,
) -> bool:
    if first.context_identity != second.context_identity:
        return False
    if first.source_capability.startswith("graph.") and (
        not first.active_graph_version
        or first.active_graph_version != second.active_graph_version
    ):
        return False
    return first.semantic_fingerprint == second.semantic_fingerprint


def _is_reusable(result: ToolResult, *, active_graph_version: str) -> bool:
    if result.status not in _REUSABLE_STATUSES or result.completeness != "complete":
        return False
    if result.truncated or result.projection_truncated or result.contradictions:
        return False
    if result.source_capability == "knowledge.search":
        return True
    if not result.source_payload_complete or not result.projection_usable:
        return False
    if result.source_capability.startswith("graph.") and not active_graph_version:
        return False
    structured = result.structured_asset_set
    if structured is not None and bool(getattr(structured, "truncated", False)):
        return False
    return True


def _authority_class(result: ToolResult) -> str:
    if result.provider == "long_term_memory":
        return "memory_historical"
    if result.source_capability.startswith("asset."):
        return "product_current"
    if result.source_capability.startswith("graph."):
        return "graph_projection"
    if result.source_capability == "knowledge.search":
        return "knowledge_reference"
    return "authorized_source"


def _temporal_class(result: ToolResult) -> str:
    if result.provider == "long_term_memory" or result.freshness in {"historical", "stale"}:
        return "historical"
    if result.freshness == "current":
        return "current"
    return "either"


def _provider_context(result: ToolResult) -> dict[str, Any]:
    provider_context = getattr(result.provider_result, "context", None)
    if isinstance(provider_context, Mapping):
        return dict(provider_context)
    if isinstance(result.raw_payload, Mapping):
        return dict(result.raw_payload)
    return {}


def _active_graph_version(result: ToolResult) -> str:
    structured = result.structured_asset_set
    if structured is not None:
        return str(getattr(structured, "active_graph_version", "") or "")
    return str(_provider_context(result).get("active_graph_version") or "")


def _structured_semantic_identity(structured: Any) -> str:
    if structured is None:
        return ""
    try:
        return str(structured.semantic_query_id)
    except (TypeError, ValueError):
        return ""


def _limitation_flags(result: ToolResult) -> tuple[str, ...]:
    flags: list[str] = []
    if result.truncated:
        flags.append("truncated")
    if result.projection_truncated:
        flags.append("projection_truncated")
    if not result.source_payload_complete:
        flags.append("source_payload_incomplete")
    if not result.projection_usable:
        flags.append("projection_unusable")
    if result.contradictions:
        flags.append("contradiction")
    if result.status in _FAILED_STATUSES:
        flags.append("provider_failure")
    return tuple(flags)


def _fact_digests(result: ToolResult) -> tuple[str, ...]:
    values: list[str] = []
    for fact in result.facts[:24]:
        canonical = json.dumps(
            {"statement": fact.statement, "value": fact.value, "fact_type": fact.fact_type},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        values.append(hashlib.sha256(canonical).hexdigest())
    return tuple(values)


def _canonical_entities(capability: str, entities: Iterable[str]) -> tuple[str, ...]:
    values = tuple(str(item) for item in entities)
    return tuple(sorted(values)) if capability == "graph.compare_assets" else values


def _entity_scope_matches(actual: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    if not expected:
        return True
    return actual == expected
