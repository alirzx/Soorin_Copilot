"""Adapters between canonical evidence and the established context composer."""

from __future__ import annotations

from dataclasses import replace
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
    provenance = []
    for result in pack.tool_results:
        provider_result = result.provider_result
        if isinstance(provider_result, GraphProviderResult):
            graph = provider_result
        elif isinstance(provider_result, DetectionProviderResult):
            detections.append(provider_result)
        elif isinstance(provider_result, AssetProfileProviderResult):
            profiles.append(provider_result)
        elif isinstance(provider_result, KnowledgeSearchResult):
            knowledge = provider_result
        item_provenance = getattr(provider_result, "provenance", None)
        if item_provenance:
            provenance.append(item_provenance)
    return CopilotContextPackage(
        entities=entities,
        graph=graph,
        detections=detections,
        asset_profiles=profiles,
        knowledge=knowledge,
        provenance=provenance,
        limitations=list(pack.limitations),
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
