"""Typed context package models shared across Copilot context providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from src.core.graph.structured import StructuredQuerySpec
from src.core.rag.models import KnowledgeSearchResult

EntitySource = Literal["message", "ui", "conversation"]
EntityType = Literal["ip"]
ProviderStatus = Literal["available", "not_found", "unavailable", "context_too_large", "skipped"]
ResolutionStatus = Literal["resolved", "none", "ambiguous", "invalid"]
EntityMode = Literal["none", "single", "multiple", "ambiguous", "invalid"]
IntentName = Literal[
    "general_knowledge",
    "asset_investigation",
    "asset_search",
    "asset_aggregate",
    "graph_neighbors",
    "graph_relationships",
    "graph_path",
    "graph_followup",
    "unclear",
]
IntentDecisionSource = Literal["semantic_router", "semantic_router_repair", "deterministic_fallback", "disabled"]
GraphScope = Literal["none", "node_summary", "one_hop", "full_neighbors", "two_hop", "path", "multi_entity_comparison"]
GraphDirection = Literal["none", "inbound", "outbound", "both"]
RelationshipMode = Literal["none", "direct", "compare"]
EntityBinding = Literal["explicit", "ui", "active_single", "active_pair", "none"]
StructuredResultReferenceKind = Literal[
    "none", "set_query", "select_entities", "historical_recall"
]


def compact_preview(text: str, limit: int = 120) -> str:
    if not text:
        return ""
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
    subnet_constraints: tuple[str, ...] = ()
    unsupported_constraints: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructuredResultReferenceDecision:
    """Semantic Router proposal for using the latest bounded result set."""

    kind: StructuredResultReferenceKind = "none"
    ordinals: tuple[int, ...] = ()


@dataclass(frozen=True)
class IntentDecision:
    intent: IntentName
    scope: GraphScope
    direction: GraphDirection
    depth: int
    requires_graph: bool
    requires_detection: bool = False
    requires_asset_profile: bool = False
    requires_knowledge: bool = False
    structured_query: StructuredQuerySpec | None = None
    structured_result_reference: StructuredResultReferenceDecision = field(
        default_factory=StructuredResultReferenceDecision
    )
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
    decision_source: IntentDecisionSource = "deterministic_fallback"
    exhaustive_connections_requested: bool = False
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
    def use_asset_profile(self) -> bool:
        return self.requires_asset_profile

    @property
    def use_knowledge(self) -> bool:
        return self.requires_knowledge

    @property
    def confidence(self) -> float:
        return self.classification_confidence


@dataclass(frozen=True)
class RouteDecision:
    use_graph: bool
    reason: str
    use_detection: bool = False
    use_asset_profile: bool = False
    use_knowledge: bool = False
    structured_query: StructuredQuerySpec | None = None
    structured_result_reference: StructuredResultReferenceDecision = field(
        default_factory=StructuredResultReferenceDecision
    )
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
    decision_source: IntentDecisionSource = "deterministic_fallback"
    exhaustive_connections_requested: bool = False
    semantic_router_called: bool = False
    semantic_router_latency_ms: int = 0
    semantic_router_retry_count: int = 0
    semantic_router_finish_reason: str | None = None
    semantic_router_content_present: bool = False
    semantic_router_error: str | None = None
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
    ip: str = ""
    raw_payload: dict[str, Any] | list[Any] | None = None
    serialized_json: str = ""
    provenance: ProviderProvenance | None = None
    limitations: list[str] = field(default_factory=list)
    cache_hit: bool = False
    cache_age_seconds: int | None = None
    cache_miss_reason: str | None = None
    context_truncated: bool = False
    context_truncation_reason: str | None = None
    raw_json_bytes: int = 0
    raw_json_chars: int = 0
    raw_json_approx_tokens: int = 0
    raw_top_level_key_count: int = 0
    raw_payload_present: bool = False
    full_payload_fetched: bool = False
    full_payload_included: bool = False
    asset_found: bool | None = None
    http_status: int | None = None
    fetched_at: str | None = None
    stale: bool = False
    latency_ms: int = 0
    error_type: str | None = None
    safe_error: str | None = None
    requested_view: str = "full"
    returned_view: str = "full"
    source_endpoint: str = ""


@dataclass(frozen=True)
class AssetProfileProviderResult:
    provider: Literal["asset_profile"]
    status: ProviderStatus
    ip: str = ""
    raw_payload: dict[str, Any] | list[Any] | None = None
    serialized_json: str = ""
    provenance: ProviderProvenance | None = None
    limitations: list[str] = field(default_factory=list)
    cache_hit: bool = False
    cache_age_seconds: int | None = None
    cache_miss_reason: str | None = None
    context_truncated: bool = False
    context_truncation_reason: str | None = None
    raw_json_bytes: int = 0
    raw_json_chars: int = 0
    raw_json_approx_tokens: int = 0
    raw_top_level_key_count: int = 0
    raw_payload_present: bool = False
    full_payload_fetched: bool = False
    full_payload_included: bool = False
    asset_found: bool | None = None
    http_status: int | None = None
    fetched_at: str | None = None
    stale: bool = False
    latency_ms: int = 0
    error_type: str | None = None
    safe_error: str | None = None
    requested_view: str = "full"
    returned_view: str = "full"
    source_endpoint: str = ""


@dataclass(frozen=True)
class CopilotContextPackage:
    entities: EntityResolution
    graph: GraphProviderResult | None = None
    graphs: list[GraphProviderResult] = field(default_factory=list)
    detections: list[DetectionProviderResult] = field(default_factory=list)
    asset_profiles: list[AssetProfileProviderResult] = field(default_factory=list)
    knowledge: KnowledgeSearchResult | None = None
    provenance: list[ProviderProvenance] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def has_model_context(self) -> bool:
        return bool(
            any(item.status in {"available", "not_found"} for item in self.graph_results)
            or any(item.status in {"available", "not_found"} for item in self.detections)
            or any(item.status in {"available", "not_found"} for item in self.asset_profiles)
            or bool(self.knowledge and self.knowledge.status in {"ok", "partial", "empty"})
        )

    @property
    def graph_results(self) -> list[GraphProviderResult]:
        return self.graphs or ([self.graph] if self.graph else [])

    @property
    def detection(self) -> DetectionProviderResult | None:
        return self.detections[0] if self.detections else None

    @property
    def asset_profile(self) -> AssetProfileProviderResult | None:
        return self.asset_profiles[0] if self.asset_profiles else None
