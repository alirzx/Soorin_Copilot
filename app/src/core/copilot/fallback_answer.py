"""Deterministic user-facing summary when final model synthesis is unavailable."""

from __future__ import annotations

from src.core.context.models import AssetProfileProviderResult, DetectionProviderResult, GraphProviderResult


def _graph_summary(graph: GraphProviderResult) -> str:
    context = graph.context or {}
    target = str(context.get("target_ip") or (graph.target_entity.value if graph.target_entity else "the requested asset"))
    inbound = int(context.get("inbound_total", 0) or 0)
    outbound = int(context.get("outbound_total", 0) or 0)
    bidirectional = int(context.get("bidirectional_total", 0) or 0)
    retrieval_complete = bool(context.get("retrieval_complete", not context.get("retrieval_truncated", False)))
    serialized_complete = bool(context.get("serialized_context_complete_for_retrieved_subset", not context.get("context_truncated", False)))
    complete_for_user = bool(context.get("complete_for_user_request", retrieval_complete and serialized_complete))
    scope = str(context.get("requested_scope") or context.get("scope") or "node_summary")
    retrieval_text = "Graph retrieval was partial; counts may exceed the returned peer subset." if not retrieval_complete else "Graph retrieval completed within its configured limits."
    context_text = "Only part of the retrieved subset fit the response context." if not serialized_complete else "The retrieved subset was fully represented in response context."
    completeness_text = "The requested scope is incomplete." if not complete_for_user else "The requested scope was satisfied."
    if inbound > outbound:
        interpretation = "More observed relationships point toward this asset than away from it."
    elif outbound > inbound:
        interpretation = "More observed relationships point away from this asset than toward it."
    else:
        interpretation = "Observed inbound and outbound relationship counts are balanced."
    return (
        f"{target} has {inbound} observed inbound relationships, {outbound} observed outbound relationships, "
        f"and {bidirectional} bidirectional relationships for scope {scope}. {retrieval_text} {context_text} {completeness_text} "
        f"{interpretation} These communication relationships do not by themselves prove formal service dependency. "
        "A useful next step is to review the relevant peers with service, ownership, and time-window evidence."
    )


def _product_evidence_notice(provider: str, ip: str) -> str:
    label = "Asset Profile" if provider == "asset_profile" else "Asset-detection"
    return (
        f"Complete {label} JSON was retrieved for {ip}, but automated synthesis was unavailable. "
        "The raw evidence was preserved without generating a partial field-based interpretation."
    )


def build_evidence_fallback_answer(
    graph: GraphProviderResult | None,
    detections: list[DetectionProviderResult] | None,
    asset_profiles: list[AssetProfileProviderResult] | None = None,
) -> str | None:
    """Build a bounded answer only when typed evidence or definitive absence exists."""
    graph_available = bool(graph and graph.status == "available")
    detections = detections or []
    asset_profiles = asset_profiles or []
    detection_available = [item for item in detections if item.status == "available" and item.raw_payload is not None]
    profile_available = [item for item in asset_profiles if item.status == "available" and item.raw_payload is not None]
    graph_not_found = bool(graph and graph.status == "not_found")
    detection_not_found = any(item.status == "not_found" for item in detections)
    profile_not_found = any(item.status == "not_found" for item in asset_profiles)

    if not graph_available and not detection_available and not profile_available:
        if graph_not_found or detection_not_found or profile_not_found:
            return (
                "Automated analysis was temporarily unavailable. No matching graph or asset-detection evidence was found "
                "in the requested sources, so the asset cannot be characterized from current product evidence."
            )
        return None

    parts = ["Automated analysis was temporarily unavailable. This summary is based directly on available structured evidence."]
    for detection in detection_available:
        parts.append(_product_evidence_notice("detection", detection.ip))
    if not detection_available and detection_not_found:
        parts.append("No asset-detection record was found for the requested IP, so its identity remains unconfirmed.")
    for profile in profile_available:
        parts.append(_product_evidence_notice("asset_profile", profile.ip))
    if profile_not_found:
        parts.append("No Asset Profile record was found for at least one requested IP.")
    if graph_available and graph:
        parts.append(_graph_summary(graph))
    elif graph_not_found:
        parts.append("The requested IP was not found in the currently loaded observed communication graph.")
    if graph_available and (detection_available or profile_available):
        parts.append(
            "Evidence agreement cannot be established deterministically from these summary fields; classification and communication structure describe different aspects of the asset."
        )
    return "\n\n".join(part for part in parts if part)
