"""Adapters between canonical evidence and the established context composer."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from src.core.agent.contracts import EvidencePack, ToolResult
from src.core.context.models import (
    AssetProfileProviderResult,
    CopilotContextPackage,
    DetectionProviderResult,
    EntityResolution,
    GraphProviderResult,
)
from src.core.rag.models import KnowledgeSearchResult


def context_package_from_evidence(
    pack: EvidencePack,
    entities: EntityResolution,
) -> CopilotContextPackage:
    """Rebuild the legacy composer input exclusively from reviewed ToolResults."""
    graph: GraphProviderResult | None = None
    detections: list[DetectionProviderResult] = []
    profiles: list[AssetProfileProviderResult] = []
    knowledge: KnowledgeSearchResult | None = None
    knowledge_results: list[KnowledgeSearchResult] = []
    provenance = []
    for result in pack.tool_results:
        provider_result = result.provider_result
        if isinstance(provider_result, GraphProviderResult):
            graph = provider_result
        elif isinstance(provider_result, DetectionProviderResult):
            if result.view_payload is not None:
                serialized = json.dumps(result.view_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                provider_result = replace(
                    provider_result,
                    raw_payload=result.view_payload,
                    serialized_json=serialized,
                    full_payload_included=False,
                    context_truncated=result.omitted_section_count > 0,
                    context_truncation_reason="question_specific_view" if result.omitted_section_count else None,
                )
            detections.append(provider_result)
        elif isinstance(provider_result, AssetProfileProviderResult):
            if result.view_payload is not None:
                serialized = json.dumps(result.view_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                provider_result = replace(
                    provider_result,
                    raw_payload=result.view_payload,
                    serialized_json=serialized,
                    full_payload_included=False,
                    context_truncated=result.omitted_section_count > 0,
                    context_truncation_reason="question_specific_view" if result.omitted_section_count else None,
                )
            profiles.append(provider_result)
        elif isinstance(provider_result, KnowledgeSearchResult):
            knowledge_results.append(provider_result)
        item_provenance = getattr(provider_result, "provenance", None)
        if item_provenance:
            provenance.append(item_provenance)
    if knowledge_results:
        knowledge = _merge_knowledge_results(knowledge_results)
    return CopilotContextPackage(
        entities=entities,
        graph=graph,
        detections=detections,
        asset_profiles=profiles,
        knowledge=knowledge,
        provenance=provenance,
        limitations=list(pack.limitations),
    )


def _merge_knowledge_results(results: list[KnowledgeSearchResult]) -> KnowledgeSearchResult:
    """Aggregate at most two validated searches and deduplicate chunks/citations."""
    primary = results[0]
    chunks = {}
    citations = {}
    for item in results:
        for chunk in item.chunks:
            current = chunks.get(chunk.chunk_id)
            if current is None or chunk.score > current.score:
                chunks[chunk.chunk_id] = chunk
        for citation in item.citations:
            citations[citation.chunk_id] = citation
    ordered = tuple(sorted(chunks.values(), key=lambda item: (-item.score, item.chunk_id)))
    statuses = {item.status for item in results}
    status = "ok" if ordered and statuses <= {"ok", "empty"} else "partial" if ordered else primary.status
    return replace(
        primary,
        status=status,
        query="; ".join(item.query for item in results),
        chunks=ordered,
        citations=tuple(citations[key] for key in sorted(citations)),
        limitations=tuple(dict.fromkeys(limitation for item in results for limitation in item.limitations)),
        total_candidates=sum(item.total_candidates or 0 for item in results),
        included_count=len(ordered),
        truncated=any(item.truncated for item in results),
    )


def apply_context_inclusion(
    results: list[ToolResult],
    inclusion: dict[str, tuple[bool, str | None]],
) -> list[ToolResult]:
    """Reflect actual composer inclusion in canonical tool metadata."""
    updated: list[ToolResult] = []
    for result in results:
        key = result.source_capability
        if key == "asset.get_profile" and result.entities:
            key = f"asset_profile:{result.entities[0]}"
        elif key == "asset.get_detection" and result.entities:
            key = f"detection:{result.entities[0]}"
        elif key.startswith("graph."):
            key = "graph"
        elif key == "knowledge.search":
            key = "knowledge"
        included, reason = inclusion.get(key, (result.context_included, None))
        limitations = result.limitations
        if not included and reason:
            limitations = tuple(dict.fromkeys((*limitations, f"Model context omitted this evidence: {reason}.")))
        updated.append(replace(result, context_included=included, limitations=limitations))
    return updated


def safe_review_summary(pack: EvidencePack) -> dict[str, Any]:
    """Compact review metadata suitable for final model context."""
    return {
        "plan_id": pack.plan_id,
        "goal": pack.goal,
        "entities": list(pack.resolved_entities),
        "provider_coverage": pack.provider_coverage,
        "graph_completeness": pack.graph_completeness,
        "review_outcome": pack.review_outcome,
        "missing_evidence": list(pack.missing_evidence[:6]),
        "contradictions": [item[:200] for item in pack.contradictions[:6]],
        "limitations": [item[:200] for item in pack.limitations[:8]],
    }
