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
    "unrelated",
    "unclear",
]
IntentDecisionSource = Literal["deterministic", "glm", "fallback", "disabled"]


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
    use_graph: bool
    is_followup: bool = False
    target_reference: str = "none"
    confidence: float = 0.0
    reason: str = ""
    decision_source: IntentDecisionSource = "deterministic"
    router_called: bool = False
    latency_ms: int = 0
    error_reason: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    use_graph: bool
    reason: str
    target_entity: ResolvedEntity | None = None
    matched_signals: list[str] = field(default_factory=list)
    graph_intent_detected: bool = False
    asset_investigation_detected: bool = False
    followup_detected: bool = False
    intent: IntentName = "unclear"
    intent_confidence: float = 0.0
    decision_source: IntentDecisionSource = "deterministic"
    glm_router_called: bool = False
    glm_router_latency_ms: int = 0
    glm_router_error: str | None = None
    should_call_intent_router: bool = False


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
