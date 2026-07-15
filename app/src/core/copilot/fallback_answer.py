"""Deterministic user-facing summary when final model synthesis is unavailable."""

from __future__ import annotations

from src.core.context.models import DetectionProviderResult, GraphProviderResult


def _graph_summary(graph: GraphProviderResult) -> str:
    context = graph.context or {}
    target = str(context.get("target_ip") or (graph.target_entity.value if graph.target_entity else "the requested asset"))
    inbound = int(context.get("inbound_total", 0) or 0)
    outbound = int(context.get("outbound_total", 0) or 0)
    bidirectional = int(context.get("bidirectional_total", 0) or 0)
    retrieval_truncated = bool(context.get("retrieval_truncated", context.get("truncated", False)))
    context_truncated = bool(context.get("context_truncated", False))
    scope = str(context.get("scope") or "node_summary")
    retrieval_text = "The requested graph retrieval was bounded or truncated." if retrieval_truncated else "The requested graph retrieval completed within its configured limits."
    context_text = "Only a bounded subset could be included in the response context." if context_truncated else "The available summary was not truncated for response context."
    if inbound > outbound:
        interpretation = "More observed relationships point toward this asset than away from it."
    elif outbound > inbound:
        interpretation = "More observed relationships point away from this asset than toward it."
    else:
        interpretation = "Observed inbound and outbound relationship counts are balanced."
    return (
        f"{target} has {inbound} observed inbound relationships, {outbound} observed outbound relationships, "
        f"and {bidirectional} bidirectional relationships for scope {scope}. {retrieval_text} {context_text} "
        f"{interpretation} These communication relationships do not by themselves prove formal service dependency. "
        "A useful next step is to review the relevant peers with service, ownership, and time-window evidence."
    )


def _detection_summary(detection: DetectionProviderResult) -> str:
    evidence = detection.evidence
    if not evidence:
        return ""
    classification = evidence.classification
    tagging = evidence.tagging
    tag = tagging.stored_tag or tagging.tag or "unknown"
    sub_tag = tagging.stored_sub_tag or tagging.sub_tag or "unknown"
    vendor = classification.vendor or "unknown"
    product = classification.product or "unknown"
    confidence = classification.confidence if classification.confidence is not None else "unknown"
    limitation = evidence.limitations[0] if evidence.limitations else "Classification is limited to the supplied detection evidence."
    return (
        f"Detection evidence for {evidence.ip} reports tag/sub-tag {tag}/{sub_tag}, vendor/product {vendor}/{product}, "
        f"and confidence {confidence}. It includes {len(evidence.matched_rules)} matched rules and "
        f"{len(evidence.conflicts)} recorded conflicts. These are product-derived classification signals, not independently verified identity. "
        f"Limitation: {limitation} A useful next step is to validate the classification against current inventory and ownership records."
    )


def build_evidence_fallback_answer(
    graph: GraphProviderResult | None,
    detection: DetectionProviderResult | None,
) -> str | None:
    """Build a bounded answer only when typed evidence or definitive absence exists."""
    graph_available = bool(graph and graph.status == "available")
    detection_available = bool(detection and detection.status == "available" and detection.evidence)
    graph_not_found = bool(graph and graph.status == "not_found")
    detection_not_found = bool(detection and detection.status == "not_found")

    if not graph_available and not detection_available:
        if graph_not_found or detection_not_found:
            return (
                "Automated analysis was temporarily unavailable. No matching graph or asset-detection evidence was found "
                "in the requested sources, so the asset cannot be characterized from current product evidence."
            )
        return None

    parts = ["Automated analysis was temporarily unavailable. This summary is based directly on available structured evidence."]
    if detection_available and detection:
        parts.append(_detection_summary(detection))
    elif detection_not_found:
        parts.append("No asset-detection record was found for the requested IP, so its identity remains unconfirmed.")
    if graph_available and graph:
        parts.append(_graph_summary(graph))
    elif graph_not_found:
        parts.append("The requested IP was not found in the currently loaded observed communication graph.")
    if graph_available and detection_available:
        parts.append(
            "Evidence agreement cannot be established deterministically from these summary fields; classification and communication structure describe different aspects of the asset."
        )
    return "\n\n".join(part for part in parts if part)
