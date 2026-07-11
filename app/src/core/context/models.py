"""Typed context package models shared across Copilot context providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


EntitySource = Literal["message", "ui", "conversation"]
EntityType = Literal["ip"]
ProviderStatus = Literal["available", "not_found", "unavailable", "skipped"]
ResolutionStatus = Literal["resolved", "none", "ambiguous"]
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
GraphScope = Literal["none", "node_summary", "one_hop", "full_neighbors", "two_hop", "path"]
GraphDirection = Literal["none", "inbound", "outbound", "both"]


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
    requires_multiple_entities: bool = False
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

    @property
    def use_graph(self) -> bool:
        return self.requires_graph

    @property
    def confidence(self) -> float:
        return self.classification_confidence


@dataclass(frozen=True)
class RouteDecision:
    use_graph: bool
    reason: str
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
class CopilotContextPackage:
    entities: EntityResolution
    graph: GraphProviderResult | None = None
    provenance: list[ProviderProvenance] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def has_model_context(self) -> bool:
        return bool(self.graph and self.graph.status in {"available", "not_found"})
