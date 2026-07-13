"""Typed context package models shared across Copilot context providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from src.core.detection.models import AssetDetectionEvidence


EntitySource = Literal["message", "ui", "conversation"]
EntityType = Literal["ip"]
ProviderStatus = Literal["available", "not_found", "unavailable", "skipped"]
ResolutionStatus = Literal["resolved", "none", "ambiguous", "invalid"]
EntityMode = Literal["none", "single", "multiple", "ambiguous", "invalid"]
IntentName = Literal[
    "general_knowledge",
    "asset_investigation",
    "graph_neighbors",
    "graph_relationships",
    "graph_path",
    "graph_followup",
    "unclear",
]
IntentDecisionSource = Literal["deterministic", "glm", "fallback", "disabled"]
GraphScope = Literal["none", "node_summary", "one_hop", "full_neighbors", "two_hop", "path", "multi_entity_comparison"]
GraphDirection = Literal["none", "inbound", "outbound", "both"]
RelationshipMode = Literal["none", "direct", "compare"]
DetectionDetail = Literal["summary", "compact_full"]
EntityBinding = Literal["explicit", "ui", "active_single", "active_pair", "none"]


def compact_preview(text: str, limit: int = 120) -> str:
    return text.strip().replace("\n", " ")[:limit]


def approx_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class ResolvedEntity:
    type: EntityType
    value: str
    source: EntitySource


@dataclass(frozen=True)
class EntityResolution:
    status: ResolutionStatus
    entities: list[ResolvedEntity] = field(default_factory=list)
    primary_entity: ResolvedEntity | None = None
    entity_mode: EntityMode = "none"
    candidate_count: int = 0
    explicit_candidate_count: int = 0
    valid_entity_count: int = 0
    reference_detected: bool = False
    reference_type: str | None = None
    reference_suppressed: bool = False
    suppression_reason: str | None = None


@dataclass(frozen=True)
class IntentDecision:
    intent: IntentName
    scope: GraphScope
    direction: GraphDirection
    depth: int
    requires_graph: bool
    requires_detection: bool = False
    detection_detail: DetectionDetail = "summary"
    entity_binding: EntityBinding = "none"
    requested_entity_binding: str = "none"
    binding_source: str = ""
    binding_available: bool = False
    binding_normalized: bool = False
    binding_normalization_reason: str | None = None
    materialized_entity_count: int = 0
    materialized_entities: tuple[str, ...] = ()
    requires_multiple_entities: bool = False
    relationship_mode: RelationshipMode = "none"
    is_followup: bool = False
    classification_confidence: float = 0.0
    reason: str = ""
    decision_source: IntentDecisionSource = "deterministic"
    router_called: bool = False
    latency_ms: int = 0
    retry_count: int = 0
    finish_reason: str | None = None
    content_present: bool = False
    error_reason: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    route_normalized: bool = False
    route_normalization_reason: str | None = None

    @property
    def use_graph(self) -> bool:
        return self.requires_graph

    @property
    def use_detection(self) -> bool:
        return self.requires_detection

    @property
    def confidence(self) -> float:
        return self.classification_confidence


@dataclass(frozen=True)
class RouteDecision:
    use_graph: bool
    reason: str
    use_detection: bool = False
    detection_detail: DetectionDetail = "summary"
    entity_binding: EntityBinding = "none"
    requested_entity_binding: str = "none"
    resolved_entity_binding: EntityBinding = "none"
    binding_source: str = ""
    binding_available: bool = False
    binding_normalized: bool = False
    binding_normalization_reason: str | None = None
    materialized_entity_count: int = 0
    materialized_entities: tuple[str, ...] = ()
    target_entity: ResolvedEntity | None = None
    target_entities: list[ResolvedEntity] = field(default_factory=list)
    matched_signals: list[str] = field(default_factory=list)
    graph_intent_detected: bool = False
    asset_investigation_detected: bool = False
    followup_detected: bool = False
    intent: IntentName = "unclear"
    scope: GraphScope = "none"
    direction: GraphDirection = "none"
    depth: int = 0
    requires_multiple_entities: bool = False
    relationship_mode: RelationshipMode = "none"
    intent_confidence: float = 0.0
    decision_source: IntentDecisionSource = "deterministic"
    glm_router_called: bool = False
    glm_router_latency_ms: int = 0
    glm_router_retry_count: int = 0
    glm_router_finish_reason: str | None = None
    glm_router_content_present: bool = False
    glm_router_error: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    route_normalized: bool = False
    route_normalization_reason: str | None = None


@dataclass(frozen=True)
class ProviderProvenance:
    source: str
    status: ProviderStatus


@dataclass(frozen=True)
class GraphProviderResult:
    provider: Literal["graph"]
    status: ProviderStatus
    target_entity: ResolvedEntity | None = None
    context: dict[str, Any] = field(default_factory=dict)
    provenance: ProviderProvenance | None = None
    limitations: list[str] = field(default_factory=list)
    error_reason: str | None = None
    latency_ms: int = 0


@dataclass(frozen=True)
class DetectionProviderResult:
    provider: Literal["detection"]
    status: ProviderStatus
    detail: DetectionDetail
    ip: str = ""
    evidence: AssetDetectionEvidence | None = None
    rendered_context: str = ""
    provenance: ProviderProvenance | None = None
    cache_hit: bool = False
    cache_age_seconds: int | None = None
    stale: bool = False
    latency_ms: int = 0
    error_type: str | None = None
    safe_error: str | None = None


@dataclass(frozen=True)
class CopilotContextPackage:
    entities: EntityResolution
    graph: GraphProviderResult | None = None
    detection: DetectionProviderResult | None = None
    provenance: list[ProviderProvenance] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def has_model_context(self) -> bool:
        return bool(
            (self.graph and self.graph.status in {"available", "not_found"})
            or (self.detection and self.detection.status in {"available", "not_found"})
        )
