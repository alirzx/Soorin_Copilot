"""Adapters between canonical evidence and the established context composer."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from src.core.agent.contracts import EvidencePack, ToolResult
from src.core.agent.context_identity import identity_for_tool_result
from src.core.context.models import (
    AssetProfileProviderResult,
    CopilotContextPackage,
    DetectionProviderResult,
    EntityResolution,
    GraphProviderResult,
    approx_tokens,
)
from src.core.rag.models import KnowledgeSearchResult


_NO_DEDICATED_ANOMALY_EVIDENCE = (
    "No dedicated anomaly provider evidence is available; only bounded graph structural analysis is supplied."
)


def context_package_from_evidence(
    pack: EvidencePack,
    entities: EntityResolution,
) -> CopilotContextPackage:
    """Rebuild the legacy composer input exclusively from reviewed ToolResults."""
    graph: GraphProviderResult | None = None
    graphs: list[GraphProviderResult] = []
    detections: list[DetectionProviderResult] = []
    profiles: list[AssetProfileProviderResult] = []
    detection_groups: dict[str, list[tuple[ToolResult, DetectionProviderResult]]] = {}
    profile_groups: dict[str, list[tuple[ToolResult, AssetProfileProviderResult]]] = {}
    knowledge: KnowledgeSearchResult | None = None
    knowledge_results: list[KnowledgeSearchResult] = []
    provenance = []
    for result in pack.tool_results:
        provider_result = result.provider_result
        if isinstance(provider_result, GraphProviderResult):
            identity = result.context_identity or identity_for_tool_result(result)
            provider_result.context["context_identity"] = identity
            provider_result.context["source_capability"] = result.source_capability
            graphs.append(provider_result)
            graph = provider_result
        elif isinstance(provider_result, DetectionProviderResult):
            detection_groups.setdefault(provider_result.ip, []).append((result, provider_result))
        elif isinstance(provider_result, AssetProfileProviderResult):
            profile_groups.setdefault(provider_result.ip, []).append((result, provider_result))
        elif isinstance(provider_result, KnowledgeSearchResult):
            knowledge_results.append(provider_result)
        item_provenance = getattr(provider_result, "provenance", None)
        if item_provenance:
            provenance.append(item_provenance)
    if knowledge_results:
        knowledge = _merge_knowledge_results(knowledge_results)
    dedicated_anomaly_evidence = any(
        result.source_capability == "asset.get_detection"
        and "anomaly_risk" in result.selected_views
        and result.status in {"ok", "partial"}
        and result.source_payload_complete
        and result.projection_usable
        and result.usable_fact_count > 0
        for result in pack.tool_results
    )
    if dedicated_anomaly_evidence:
        for graph_result in graphs:
            graph_result.context["limitations"] = [
                item
                for item in graph_result.context.get("limitations", ())
                if item != _NO_DEDICATED_ANOMALY_EVIDENCE
            ]
            graph_result.limitations[:] = [
                item for item in graph_result.limitations if item != _NO_DEDICATED_ANOMALY_EVIDENCE
            ]
    detections.extend(_merge_product_context(items) for items in detection_groups.values())
    profiles.extend(_merge_product_context(items) for items in profile_groups.values())
    return CopilotContextPackage(
        entities=entities,
        graph=graph,
        graphs=graphs,
        detections=detections,
        asset_profiles=profiles,
        knowledge=knowledge,
        provenance=provenance,
        limitations=list(pack.limitations),
    )


def _merge_product_context(items: list[tuple[ToolResult, Any]]) -> Any:
    """Serialize one deduplicated complete payload for one provider/entity."""
    first_result, provider_result = items[0]
    payload = first_result.raw_payload
    if payload is None:
        return provider_result
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return replace(
        provider_result,
        raw_payload=payload,
        serialized_json=serialized,
        raw_payload_present=True,
        full_payload_fetched=all(result.source_payload_complete for result, _ in items),
        full_payload_included=True,
        context_truncated=False,
        context_truncation_reason=None,
        raw_json_chars=len(serialized),
        raw_json_bytes=len(serialized.encode("utf-8")),
        raw_json_approx_tokens=approx_tokens(serialized),
        raw_top_level_key_count=len(payload) if isinstance(payload, dict) else 0,
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
        key = result.context_identity or identity_for_tool_result(result)
        legacy_key = result.source_capability
        if legacy_key == "asset.get_profile" and result.entities:
            key = f"asset_profile:{result.entities[0]}"
        elif legacy_key == "asset.get_detection" and result.entities:
            key = f"detection:{result.entities[0]}"
        elif legacy_key == "knowledge.search" and key not in inclusion:
            key = "knowledge"
        elif legacy_key.startswith("graph.") and key not in inclusion:
            key = "graph"
        included, reason = inclusion.get(key, (False, "missing_context_inclusion_decision"))
        is_product = legacy_key in {"asset.get_profile", "asset.get_detection"}
        graph_context = (
            result.provider_result.context
            if legacy_key.startswith("graph.")
            and getattr(result.provider_result, "context", None) is not None
            else {}
        )
        if (
            included
            and is_product
            and (
                not result.source_payload_complete
                or not result.projection_usable
                or result.projection_truncated
                or result.projection_omitted_count != 0
            )
        ):
            included = False
            reason = "product_full_payload_contract_invalid"
        limitations = result.limitations
        if not included and reason:
            limitations = tuple(dict.fromkeys((*limitations, f"Model context omitted this evidence: {reason}.")))
        updated.append(
            replace(
                result,
                context_identity=result.context_identity or identity_for_tool_result(result),
                context_included=included,
                context_inclusion_reason=reason,
                context_representation=(
                    "excluded"
                    if not included
                    else "full_minified"
                    if is_product
                    else str(graph_context.get("context_mode") or "included")
                ),
                context_token_estimate=(
                    int(graph_context.get("model_context_token_estimate") or 0)
                    if legacy_key.startswith("graph.")
                    else result.view_token_estimate
                    if is_product
                    else result.context_token_estimate
                ),
                context_token_cap=(
                    int(graph_context.get("model_context_token_cap") or 0)
                    if legacy_key.startswith("graph.")
                    else result.context_token_cap
                ),
                limitations=limitations,
            )
        )
    return updated


def safe_review_summary(pack: EvidencePack) -> dict[str, Any]:
    """Compact review metadata suitable for final model context."""
    return {
        "plan_id": pack.plan_id,
        "goal": pack.goal,
        "entities": list(pack.resolved_entities),
        "provider_coverage": pack.provider_coverage,
        "result_coverage": pack.result_coverage,
        "graph_completeness": pack.graph_completeness,
        "review_outcome": pack.review_outcome,
        "missing_evidence": list(pack.missing_evidence[:6]),
        "contradictions": [item[:200] for item in pack.contradictions[:6]],
        "limitations": [item[:200] for item in pack.limitations[:8]],
    }
