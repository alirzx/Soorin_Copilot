"""Deterministic fallback routing used only when the GLM router is unusable."""

from __future__ import annotations

import logging
import re
import time

from src.core.context.models import EntityBinding, EntityResolution, GraphDirection, GraphScope, IntentDecision, IntentName, RouteDecision, compact_preview
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)

GRAPH_WORDS = re.compile(
    r"\b(?:graph|topolog(?:y|ies)|network|connections?|communicat(?:e|es|ion)|"
    r"neighbors?|peers?|inbound|outbound|linked|relationships?|talks\s+to|interacts?\s+with)\b",
    re.IGNORECASE,
)
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
ENTITY_REFERENCE_WORDS = re.compile(
    r"\b(?:this\s+(?:asset|device|ip|host)|that\s+(?:asset|device|ip|host)|it|its|them|their)\b",
    re.IGNORECASE,
)
DETECTION_COMPACT_WORDS = re.compile(
    r"\b(?:classified|classification|detect(?:ion|ed)?|evidence|rules?|matched\s+rules?|conflicts?|full\s+details?|all\s+available\s+detection|why\s+was)\b",
    re.IGNORECASE,
)
IDENTITY_WORDS = re.compile(r"\b(?:what\s+is|tell\s+me\s+about|summary|asset|device|role|identity|behaviou?r|agree)\b", re.IGNORECASE)
SECURITY_ANALYSIS_WORDS = re.compile(
    r"\b(?:anomal(?:y|ies|ous)|suspicious|unusual|abnormal|security|risk|threat|compromise[ds]?)\b",
    re.IGNORECASE,
)
OPERATIONAL_INTENTS = {
    "asset_investigation",
    "graph_neighbors",
    "graph_relationships",
    "graph_path",
    "graph_followup",
}


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
        previous_intent = routing_state.previous_intent if routing_state else None
        previous_scope = routing_state.previous_scope if routing_state else None
        previous_route_used = False
        topic_detached = bool(entities.reference_suppressed)

        intent: IntentName = "general_knowledge"
        scope: GraphScope = "none"
        direction: GraphDirection = "none"
        depth = 0
        use_graph = False
        use_detection = False
        detection_detail = "summary"
        requires_multiple = False
        reason = "fallback_general_knowledge"
        signal_group = "general_knowledge"
        entity_binding: EntityBinding = "none"
        if target and entity_count == 1:
            entity_binding = "active_single" if target.source == "conversation" else "explicit" if target.source == "message" else "ui"
        elif entity_count == 2:
            entity_binding = "active_pair" if all(entity.source == "conversation" for entity in entities.entities) else "explicit"

        graph_signal = bool(GRAPH_WORDS.search(message or ""))
        detection_signal = bool(DETECTION_COMPACT_WORDS.search(message or ""))
        identity_signal = bool(IDENTITY_WORDS.search(message or ""))
        security_signal = bool(SECURITY_ANALYSIS_WORDS.search(message or ""))
        entity_followup_signal = bool(
            entities.reference_detected
            or ENTITY_REFERENCE_WORDS.search(message or "")
            or FOLLOWUP_WORDS.search(message or "")
        )

        if topic_detached:
            reason = "fallback_topic_detachment"
            signal_group = "topic_detachment"
        elif entity_count > 2:
            intent, scope, direction, depth = "unclear", "none", "none", 0
            use_graph, reason = False, "fallback_too_many_entities"
            signal_group = "too_many_entities"
        elif PATH_WORDS.search(message or "") and entity_count == 2:
            intent, scope, direction, depth = "graph_path", "path", "both", 0
            use_graph, requires_multiple, reason = True, True, "fallback_path"
            signal_group = "graph_path"
        elif COMPARISON_WORDS.search(message or "") and entity_count == 2 and not DIRECT_RELATIONSHIP_WORDS.search(message or ""):
            intent, scope, direction, depth = "graph_relationships", "multi_entity_comparison", "both", 1
            use_graph, requires_multiple, reason = True, True, "fallback_comparison"
            signal_group = "graph_comparison"
        elif RELATIONSHIP_WORDS.search(message or "") and entity_count == 2:
            intent, scope, direction, depth = "graph_relationships", "one_hop", "both", 1
            use_graph, requires_multiple, reason = True, True, "fallback_relationship"
            signal_group = "graph_relationship"
        elif target and entity_count == 1 and TWO_HOP_WORDS.search(message or ""):
            intent, scope, direction, depth = "graph_neighbors", "two_hop", _direction(message), 2
            use_graph, reason = True, "fallback_two_hop"
            signal_group = "graph_topology"
        elif target and entity_count == 1 and graph_signal and detection_signal:
            intent, scope, direction, depth = "asset_investigation", "node_summary", "both", 0
            use_graph, use_detection, detection_detail = True, True, "summary"
            reason = "fallback_combined_graph_detection"
            signal_group = "combined_provider_request"
        elif target and entity_count == 1 and graph_signal:
            intent = "graph_neighbors"
            scope = "full_neighbors" if FULL_WORDS.search(message or "") else "one_hop"
            direction = _direction(message)
            depth = 1
            use_graph, reason = True, "fallback_graph_neighbors"
            signal_group = "graph_topology"
        elif target and entity_count == 1 and detection_signal:
            intent, scope, direction, depth = "asset_investigation", "none", "none", 0
            use_detection, detection_detail, reason = True, "compact_full", "fallback_detection_compact_full"
            signal_group = "asset_detection"
        elif target and entity_count == 1 and (identity_signal or ASSET_WORDS.search(message or "")) and not security_signal:
            intent, scope, direction, depth = "asset_investigation", "node_summary", "both", 0
            use_graph, use_detection, detection_detail, reason = True, True, "summary", "fallback_asset_investigation"
            signal_group = "asset_identity"
        elif target and entity_count == 1 and security_signal:
            intent, scope, direction, depth = "asset_investigation", "node_summary", "both", 0
            use_graph, reason = True, "fallback_security_graph_baseline"
            signal_group = "security_or_anomaly"
        elif target and entity_followup_signal and previous_intent in OPERATIONAL_INTENTS and last_provider in {"graph", "detection"}:
            previous_route_used = True
            if entity_count == 2 and previous_intent in {"graph_relationships", "graph_path"}:
                intent = previous_intent  # type: ignore[assignment]
                scope = "path" if previous_intent == "graph_path" else "multi_entity_comparison"
                direction, depth = "both", 0 if scope == "path" else 1
                use_graph, requires_multiple = True, True
            elif entity_count == 1:
                intent = previous_intent if previous_intent in {"asset_investigation", "graph_neighbors", "graph_followup"} else "asset_investigation"  # type: ignore[assignment]
                scope = previous_scope if previous_scope in {"node_summary", "one_hop", "full_neighbors", "two_hop"} else "node_summary"  # type: ignore[assignment]
                depth = 2 if scope == "two_hop" else 1 if scope in {"one_hop", "full_neighbors"} else 0
                direction = _direction(
                    message,
                    routing_state.previous_direction
                    if routing_state and routing_state.previous_direction in {"inbound", "outbound", "both"}
                    else "both",
                )  # type: ignore[arg-type]
                use_graph = bool(last_provider == "graph" or scope != "none")
                use_detection = bool(
                    intent == "asset_investigation"
                    and routing_state
                    and routing_state.previous_requires_detection
                )
                detection_detail = (
                    routing_state.previous_detection_detail
                    if use_detection and routing_state and routing_state.previous_detection_detail in {"summary", "compact_full"}
                    else "summary"
                )  # type: ignore[assignment]
            reason = "fallback_previous_operational_route"
            signal_group = "previous_operational_route"

        decision = RouteDecision(
            use_graph=use_graph,
            reason=reason,
            use_detection=use_detection if entity_count == 1 else False,
            detection_detail=detection_detail if use_detection and entity_count == 1 else "summary",  # type: ignore[arg-type]
            entity_binding=entity_binding,
            requested_entity_binding=entity_binding,
            resolved_entity_binding=entity_binding,
            binding_source=target.source if target else ("conversation" if entity_binding == "active_pair" else "none"),
            binding_available=bool(entity_count),
            materialized_entity_count=entity_count,
            materialized_entities=tuple(entity.value for entity in entities.entities),
            target_entity=target,
            target_entities=entities.entities,
            matched_signals=[
                signal_group,
                *(["security_or_anomaly"] if security_signal and signal_group != "security_or_anomaly" else []),
            ],
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
            "event=deterministic_fallback_route_decided request_id=%s fallback_reason=%s signal_group=%s resolved_entity_count=%s previous_route_used=%s previous_intent=%s previous_scope=%s topic_detachment=%s selected_providers=%s intent=%s scope=%s direction=%s depth=%s detection_detail=%s latency_ms=%s message_preview=%r",
            request_id,
            fallback_reason,
            signal_group,
            entity_count,
            previous_route_used,
            previous_intent or "",
            previous_scope or "",
            topic_detached,
            ",".join(
                provider
                for provider, enabled in (("graph", decision.use_graph), ("detection", decision.use_detection))
                if enabled
            ) or "none",
            decision.intent,
            decision.scope,
            decision.direction,
            decision.depth,
            decision.detection_detail,
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
    requires_detection = bool(decision.requires_detection and len(target_entities) == 1 and target_entity)
    detection_detail = decision.detection_detail if requires_detection else "summary"
    if decision.requires_multiple_entities:
        requires_graph = len(target_entities) == 2
        requires_detection = False
        detection_detail = "summary"
        target_entity = None
    if decision.intent in {"graph_relationships", "graph_path"}:
        requires_graph = len(target_entities) == 2
        requires_detection = False
        detection_detail = "summary"
        target_entity = None
    if decision.intent in {"graph_neighbors", "asset_investigation"}:
        requires_graph = bool(decision.requires_graph and len(target_entities) == 1)
        requires_detection = bool(decision.requires_detection and len(target_entities) == 1)
        detection_detail = decision.detection_detail if requires_detection else "summary"
        target_entity = target_entities[0] if target_entities else None
    if len(target_entities) > 2:
        requires_graph = False
        requires_detection = False
        detection_detail = "summary"
    return RouteDecision(
        use_graph=requires_graph,
        reason=decision.route_normalization_reason or ("glm_semantic_route" if decision.decision_source == "glm" else decision.fallback_reason or "router_no_graph"),
        use_detection=requires_detection,
        detection_detail=detection_detail,
        entity_binding=decision.entity_binding,
        requested_entity_binding=decision.requested_entity_binding,
        resolved_entity_binding=decision.entity_binding,
        binding_source=decision.binding_source,
        binding_available=decision.binding_available,
        binding_normalized=decision.binding_normalized,
        binding_normalization_reason=decision.binding_normalization_reason,
        materialized_entity_count=len(target_entities),
        materialized_entities=tuple(entity.value for entity in target_entities),
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
