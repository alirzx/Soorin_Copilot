"""Deterministic fallback routing used only when the GLM router is unusable."""

from __future__ import annotations

import logging
import re
import time

from src.core.context.models import EntityResolution, GraphDirection, GraphScope, IntentDecision, IntentName, RouteDecision, compact_preview
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)

GRAPH_WORDS = re.compile(r"\b(?:graph|topolog(?:y|ies)|connections?|communicat(?:e|es|ion)|neighbors?|peers?|linked|talks\s+to|interacts?\s+with)\b", re.IGNORECASE)
FULL_WORDS = re.compile(r"\b(?:all|every|full|complete|entire)\b", re.IGNORECASE)
INBOUND_WORDS = re.compile(r"\b(?:inbound|incoming|sources?|connects?\s+to\s+this|toward)\b", re.IGNORECASE)
OUTBOUND_WORDS = re.compile(r"\b(?:outbound|outgoing|destinations?|reaches|sends?\s+to)\b", re.IGNORECASE)
PATH_WORDS = re.compile(r"\b(?:path|shortest\s+path|route|reachability|chain|intermediate)\b", re.IGNORECASE)
RELATIONSHIP_WORDS = re.compile(r"\b(?:directly\s+connected|adjacent|relationship|edge\s+between)\b", re.IGNORECASE)
DIRECT_RELATIONSHIP_WORDS = re.compile(r"\b(?:directly\s+connected|direct\s+connection|adjacent|edge\s+between|a\s*->\s*b|b\s*->\s*a)\b", re.IGNORECASE)
COMPARISON_WORDS = re.compile(r"\b(?:compare|comparison|both\s+assets|both\s+ips|positions?|shared\s+peers?|common\s+peers?|broader\s+outbound|relationship)\b", re.IGNORECASE)
TWO_HOP_WORDS = re.compile(r"\b(?:two\s+hops?|2\s+hops?|surrounding\s+network|expand\s+the\s+network|wider)\b", re.IGNORECASE)
ASSET_WORDS = re.compile(r"\b(?:tell|show|explain|investigate|analy[sz]e|what\s+about|how\s+about|what\s+.*know|all\s+you\s+know)\b", re.IGNORECASE)
FOLLOWUP_WORDS = re.compile(r"\b(?:go\s+deeper|continue|more\s+details|what\s+else|expand)\b", re.IGNORECASE)


def _direction(message: str, default: GraphDirection = "both") -> GraphDirection:
    inbound = bool(INBOUND_WORDS.search(message or ""))
    outbound = bool(OUTBOUND_WORDS.search(message or ""))
    if inbound and not outbound:
        return "inbound"
    if outbound and not inbound:
        return "outbound"
    return default


class DeterministicFallbackRouter:
    """Small safe fallback; GLM owns normal semantic routing."""

    def route(
        self,
        message: str,
        entities: EntityResolution,
        routing_state: SessionRoutingState | None = None,
        *,
        fallback_reason: str = "",
        request_id: str = "",
    ) -> RouteDecision:
        started = time.perf_counter()
        entity_count = len(entities.entities)
        target = entities.entities[0] if entities.entities else entities.primary_entity
        last_provider = routing_state.last_provider if routing_state else None
        previous_scope = routing_state.previous_scope if routing_state else None

        intent: IntentName = "general_knowledge"
        scope: GraphScope = "none"
        direction: GraphDirection = "none"
        depth = 0
        use_graph = False
        requires_multiple = False
        reason = "fallback_general_knowledge"

        if entities.reference_suppressed:
            reason = "fallback_topic_detachment"
        elif entity_count > 2:
            intent, scope, direction, depth = "unclear", "none", "none", 0
            use_graph, reason = False, "fallback_too_many_entities"
        elif PATH_WORDS.search(message or "") and entity_count == 2:
            intent, scope, direction, depth = "graph_path", "path", "both", 0
            use_graph, requires_multiple, reason = True, True, "fallback_path"
        elif COMPARISON_WORDS.search(message or "") and entity_count == 2 and not DIRECT_RELATIONSHIP_WORDS.search(message or ""):
            intent, scope, direction, depth = "graph_relationships", "multi_entity_comparison", "both", 1
            use_graph, requires_multiple, reason = True, True, "fallback_comparison"
        elif RELATIONSHIP_WORDS.search(message or "") and entity_count == 2:
            intent, scope, direction, depth = "graph_relationships", "one_hop", "both", 1
            use_graph, requires_multiple, reason = True, True, "fallback_relationship"
        elif target and entity_count == 1 and TWO_HOP_WORDS.search(message or ""):
            intent, scope, direction, depth = "graph_neighbors", "two_hop", _direction(message), 2
            use_graph, reason = True, "fallback_two_hop"
        elif target and entity_count == 1 and GRAPH_WORDS.search(message or ""):
            intent = "graph_neighbors"
            scope = "full_neighbors" if FULL_WORDS.search(message or "") else "one_hop"
            direction = _direction(message)
            depth = 1
            use_graph, reason = True, "fallback_graph_neighbors"
        elif target and entity_count == 1 and ASSET_WORDS.search(message or ""):
            intent, scope, direction, depth = "asset_investigation", "node_summary", "both", 0
            use_graph, reason = True, "fallback_asset_investigation"
        elif target and FOLLOWUP_WORDS.search(message or "") and last_provider == "graph":
            intent = "graph_followup"
            if previous_scope == "node_summary":
                scope, depth = "one_hop", 1
            elif previous_scope == "one_hop":
                scope, depth = "two_hop", 2
            else:
                scope, depth = "one_hop", 1
            direction = _direction(message, (routing_state.previous_direction if routing_state and routing_state.previous_direction in {"inbound", "outbound", "both"} else "both"))  # type: ignore[arg-type]
            use_graph, reason = True, "fallback_graph_followup"

        decision = RouteDecision(
            use_graph=use_graph,
            reason=reason,
            target_entity=target,
            target_entities=entities.entities,
            matched_signals=[],
            graph_intent_detected=use_graph and intent.startswith("graph_"),
            asset_investigation_detected=intent == "asset_investigation",
            followup_detected=intent == "graph_followup",
            intent=intent,
            scope=scope,
            direction=direction,
            depth=depth,
            requires_multiple_entities=requires_multiple,
            relationship_mode="compare" if scope == "multi_entity_comparison" else "direct" if intent == "graph_relationships" else "none",
            intent_confidence=1.0 if use_graph else 0.8,
            decision_source="fallback",
            fallback_used=True,
            fallback_reason=fallback_reason,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=deterministic_fallback_route_decided request_id=%s use_graph=%s reason=%s intent=%s scope=%s direction=%s depth=%s entity_count=%s fallback_reason=%s latency_ms=%s message_preview=%r",
            request_id,
            decision.use_graph,
            decision.reason,
            decision.intent,
            decision.scope,
            decision.direction,
            decision.depth,
            entity_count,
            fallback_reason,
            latency_ms,
            compact_preview(message),
        )
        return decision


GraphContextRouter = DeterministicFallbackRouter


def normalize_intent_route(decision: IntentDecision, entities: EntityResolution) -> RouteDecision:
    """Convert a validated router decision into an executable safe route."""
    target_entities = entities.entities
    target_entity = target_entities[0] if len(target_entities) == 1 else entities.primary_entity
    requires_graph = bool(decision.requires_graph and target_entity)
    if decision.requires_multiple_entities:
        requires_graph = len(target_entities) == 2
        target_entity = None
    if decision.intent in {"graph_relationships", "graph_path"}:
        requires_graph = len(target_entities) == 2
        target_entity = None
    if decision.intent in {"graph_neighbors", "asset_investigation"}:
        requires_graph = bool(decision.requires_graph and len(target_entities) == 1)
        target_entity = target_entities[0] if target_entities else None
    if len(target_entities) > 2:
        requires_graph = False
    return RouteDecision(
        use_graph=requires_graph,
        reason=decision.route_normalization_reason or ("glm_semantic_route" if decision.decision_source == "glm" else decision.fallback_reason or "router_no_graph"),
        target_entity=target_entity,
        target_entities=target_entities,
        matched_signals=[decision.intent, decision.scope, decision.direction],
        graph_intent_detected=decision.intent.startswith("graph_"),
        asset_investigation_detected=decision.intent == "asset_investigation",
        followup_detected=decision.is_followup,
        intent=decision.intent,
        scope=decision.scope,
        direction=decision.direction,
        depth=decision.depth,
        requires_multiple_entities=decision.requires_multiple_entities,
        relationship_mode=decision.relationship_mode,
        intent_confidence=decision.classification_confidence,
        decision_source=decision.decision_source,
        glm_router_called=decision.router_called,
        glm_router_latency_ms=decision.latency_ms,
        glm_router_retry_count=decision.retry_count,
        glm_router_finish_reason=decision.finish_reason,
        glm_router_content_present=decision.content_present,
        glm_router_error=decision.error_reason,
        fallback_used=decision.fallback_used,
        fallback_reason=decision.fallback_reason,
        route_normalized=decision.route_normalized,
        route_normalization_reason=decision.route_normalization_reason,
    )
