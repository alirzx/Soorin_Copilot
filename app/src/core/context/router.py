"""Deterministic fallback routing used only when the semantic router is unusable."""

from __future__ import annotations

import logging
import re
import time

from src.core.context.models import EntityBinding, EntityResolution, GraphDirection, GraphScope, IntentDecision, IntentName, RouteDecision, compact_preview
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)

GRAPH_WORDS = re.compile(
    r"\b(?:graph|topolog(?:y|ies)|network|connections?|communicat(?:e|es|ion)|neighbors?|peers?|inbound|outbound|"
    r"incoming|outgoing|destinations?|reaches?|depends?\s+on|linked|relationships?|talks\s+to|interacts?\s+with|path|route)\b",
    re.IGNORECASE,
)
FULL_WORDS = re.compile(r"\b(?:all|every|full|complete|entire)\b", re.IGNORECASE)
FULL_DIRECT_WORDS = re.compile(
    r"\b(?:all(?:\s+of)?\s+(?:its|their)?\s*(?:direct\s+)?(?:connections?|neighbors?|peers?|relationships?)|"
    r"every\s+(?:direct\s+)?(?:connection|neighbor|peer|relationship)|"
    r"(?:full|complete)\s+(?:direct\s+|inbound\s+and\s+outbound\s+)?(?:connections?|neighbors?|peers?|neighborhoods?)(?:\s+(?:list|set))?|"
    r"all\s+(?:inbound|outbound|bidirectional)(?:,?\s+(?:inbound|outbound|bidirectional)|\s+and\s+(?:inbound|outbound|bidirectional))*\s+(?:connections?|peers?)|"
    r"enumerate\s+(?:all|every)\s+(?:direct\s+)?(?:connection|neighbor|peer)|"
    r"show\s+(?:the\s+)?(?:full|complete)\s+neighborhood|list\s+all\s+connected\s+assets?|"
    r"based\s+on\s+all(?:\s+of)?\s+(?:its|their)\s+connections?)\b",
    re.IGNORECASE,
)
EXHAUSTIVE_QUANTIFIER_WORDS = re.compile(r"\b(?:all|every|full|complete|entire|enumerate)\b", re.IGNORECASE)
DIRECT_NEIGHBOR_NOUNS = re.compile(r"\b(?:connections?|neighbors?|neighborhoods?|peers?|direct\s+relationships?|connected\s+assets?)\b", re.IGNORECASE)
INBOUND_WORDS = re.compile(r"\b(?:inbound|incoming|sources?|connects?\s+to\s+this|toward|systems?\s+depend(?:s|ing)?\s+on)\b", re.IGNORECASE)
OUTBOUND_WORDS = re.compile(r"\b(?:outbound|outgoing|destinations?|reaches|sends?\s+to|systems?\s+(?:does\s+it\s+)?reach)\b", re.IGNORECASE)
PATH_WORDS = re.compile(r"\b(?:path|shortest\s+path|route|reachability|chain|intermediate)\b", re.IGNORECASE)
RELATIONSHIP_WORDS = re.compile(r"\b(?:directly\s+connected|adjacent|relationship|edge\s+between)\b", re.IGNORECASE)
DIRECT_RELATIONSHIP_WORDS = re.compile(r"\b(?:directly\s+connected|direct\s+connection|adjacent|edge\s+between|a\s*->\s*b|b\s*->\s*a)\b", re.IGNORECASE)
COMPARISON_WORDS = re.compile(
    r"\b(?:compare|comparison|different|difference|differences|versus|vs\.?|"
    r"both\s+assets|both\s+ips|positions?|shared\s+peers?|common\s+peers?)\b",
    re.IGNORECASE,
)
TWO_HOP_WORDS = re.compile(
    r"\b(?:two[-\s]hops?|2\s+hops?|neighbors?\s+of\s+neighbors?|second[-\s]degree(?:\s+connections?|\s+impact)?|"
    r"indirect\s+connections?|surrounding\s+network|expand\s+the\s+network|connections?\s+through\s+(?:its\s+|direct\s+)?neighbors?)\b",
    re.IGNORECASE,
)
ASSET_WORDS = re.compile(r"\b(?:tell|show|explain|investigate|analy[sz]e|what\s+about|how\s+about|what\s+.*know|all\s+you\s+know)\b", re.IGNORECASE)
FOLLOWUP_WORDS = re.compile(r"\b(?:go\s+deeper|continue|more\s+details|what\s+else|expand)\b", re.IGNORECASE)
ENTITY_REFERENCE_WORDS = re.compile(r"\b(?:this\s+(?:asset|device|ip|host)|that\s+(?:asset|device|ip|host)|it|its|them|their)\b", re.IGNORECASE)
DETECTION_EVIDENCE_WORDS = re.compile(
    r"\b(?:classified|classifications?|detect(?:ion|ed)?|prediction|confidence|evidence|rules?|matched\s+rules?|conflicts?|supporting\s+signals?)\b",
    re.IGNORECASE,
)
ASSET_PROFILE_WORDS = re.compile(
    r"\b(?:asset\s+profiles?|profiles?|inventory|owner|assigned\s+user|who\s+uses|host\s*name|hostname|operating\s+system|os|"
    r"asset\s+type|device\s+type|risk(?:\s+(?:score|level|trend))?|alerts?|services?|authentication|kerberos|ldap|ntlm|smb|"
    r"domain\s+join(?:ed)?|mac(?:\s+(?:address|vendor))?|open\s+ports?|kdc|identit(?:y|ies))\b",
    re.IGNORECASE,
)
TOPOLOGY_SUMMARY_WORDS = re.compile(
    r"\b(?:summari[sz]e|summary|overview|high[-\s]level)\b.{0,40}\b(?:graph|topology|connections?)\b|"
    r"\b(?:graph|topology)\b.{0,40}\b(?:summary|overview)\b",
    re.IGNORECASE,
)
SECURITY_ANALYSIS_WORDS = re.compile(r"\b(?:anomal(?:y|ies|ous)|suspicious|unusual|abnormal|security|threat|compromise[ds]?)\b", re.IGNORECASE)
COMPREHENSIVE_WORDS = re.compile(
    r"\b(?:all\s+(?:available\s+)?evidence|complete\s+(?:investigation|analysis|evidence|asset\s+report)|full\s+(?:investigation|asset\s+assessment)|"
    r"deep\s+analysis|comprehensive\s+(?:investigation|asset\s+report|report)|everything\s+known|identity,?\s+classification,?\s+and\s+(?:behavior|communications?))\b",
    re.IGNORECASE,
)
BEHAVIOR_PROFILE_WORDS = re.compile(r"\b(?:behavio[u]?r|topology|communications?)\b.{0,60}\bprofile\b|\bprofile\b.{0,60}\b(?:behavio[u]?r|topology|communications?)\b", re.IGNORECASE)
PROFILE_DETECTION_WORDS = re.compile(r"\bprofile\b.{0,60}\b(?:classification|detection)\b|\b(?:classification|detection)\b.{0,60}\bprofile\b", re.IGNORECASE)
OPERATIONAL_INTENTS = {"asset_investigation", "graph_neighbors", "graph_relationships", "graph_path", "graph_followup"}


def _direction(message: str, default: GraphDirection = "both") -> GraphDirection:
    if FULL_DIRECT_WORDS.search(message or ""):
        return "both"
    inbound = bool(INBOUND_WORDS.search(message or ""))
    outbound = bool(OUTBOUND_WORDS.search(message or ""))
    if inbound and not outbound:
        return "inbound"
    if outbound and not inbound:
        return "outbound"
    return default


def is_exhaustive_connection_request(message: str) -> bool:
    """Return true only for explicit complete direct-neighbor enumeration wording."""
    text = message or ""
    return bool(
        FULL_DIRECT_WORDS.search(text)
        or (EXHAUSTIVE_QUANTIFIER_WORDS.search(text) and DIRECT_NEIGHBOR_NOUNS.search(text))
    )


class DeterministicFallbackRouter:
    """Small safe fallback; the configured LLM remains the primary router."""

    def route(
        self,
        message: str,
        entities: EntityResolution,
        routing_state: SessionRoutingState | None = None,
        *,
        fallback_reason: str = "",
        request_id: str = "",
        constraints: Any = None,
        turn_policy: Any = None,
    ) -> RouteDecision:
        started = time.perf_counter()
        entity_count = len(entities.entities)
        target = entities.entities[0] if entities.entities else entities.primary_entity
        last_providers = set(routing_state.last_providers if routing_state else ())
        if routing_state and routing_state.last_provider:
            last_providers.add(routing_state.last_provider)
        previous_intent = routing_state.previous_intent if routing_state else None
        previous_scope = routing_state.previous_scope if routing_state else None
        topic_detached = bool(entities.reference_suppressed)
        entity_binding: EntityBinding = "none"
        if target and entity_count == 1:
            entity_binding = "active_single" if target.source == "conversation" else "explicit" if target.source == "message" else "ui"
        elif entity_count == 2:
            entity_binding = "active_pair" if all(item.source == "conversation" for item in entities.entities) else "explicit"

        intent: IntentName = "general_knowledge"
        scope: GraphScope = "none"
        direction: GraphDirection = "none"
        depth = 0
        use_graph = False
        use_detection = False
        use_asset_profile = False
        requires_multiple = entity_count == 2
        reason = "fallback_general_knowledge"
        signal_group = "general_knowledge"
        previous_route_used = False

        exhaustive_connections = is_exhaustive_connection_request(message)
        graph_signal = bool(GRAPH_WORDS.search(message or "")) or exhaustive_connections
        detection_signal = bool(DETECTION_EVIDENCE_WORDS.search(message or ""))
        profile_signal = bool(ASSET_PROFILE_WORDS.search(message or ""))
        comprehensive_signal = bool(COMPREHENSIVE_WORDS.search(message or ""))
        security_signal = bool(SECURITY_ANALYSIS_WORDS.search(message or ""))
        entity_followup_signal = bool(entities.reference_detected or ENTITY_REFERENCE_WORDS.search(message or "") or FOLLOWUP_WORDS.search(message or ""))

        if topic_detached:
            reason, signal_group, requires_multiple = "fallback_topic_detachment", "topic_detachment", False
        elif entity_count > 2:
            intent, reason, signal_group, requires_multiple = "unclear", "fallback_too_many_entities", "too_many_entities", False
        elif entity_count == 2 and PATH_WORDS.search(message or ""):
            intent, scope, direction, depth = "graph_path", "path", "both", 0
            use_graph, reason, signal_group = True, "fallback_path", "graph_path"
        elif entity_count == 2 and (
            DIRECT_RELATIONSHIP_WORDS.search(message or "")
            or RELATIONSHIP_WORDS.search(message or "")
        ):
            intent = "graph_relationships"
            scope, direction, depth = "one_hop", "both", 1
            use_graph, reason, signal_group = True, "fallback_relationship", "graph_relationship"
        elif entity_count == 2 and (COMPARISON_WORDS.search(message or "") or graph_signal):
            intent, scope, direction, depth = "graph_relationships", "multi_entity_comparison", "both", 1
            use_graph, reason, signal_group = True, "fallback_comparison", "graph_comparison"
            if COMPARISON_WORDS.search(message or ""):
                use_detection = True
                use_asset_profile = True
        elif entity_count == 2 and (profile_signal or detection_signal or comprehensive_signal):
            intent, scope, direction, depth = "asset_investigation", "none", "none", 0
            reason, signal_group = "fallback_multi_asset_product_evidence", "multi_asset_product_evidence"
        elif target and entity_count == 1 and TWO_HOP_WORDS.search(message or ""):
            intent, scope, direction, depth = "graph_neighbors", "two_hop", _direction(message), 2
            use_graph, reason, signal_group = True, "fallback_two_hop", "graph_topology"
        elif target and entity_count == 1 and graph_signal:
            intent = "graph_neighbors"
            scope = "node_summary" if TOPOLOGY_SUMMARY_WORDS.search(message or "") and not exhaustive_connections else "full_neighbors" if exhaustive_connections else "one_hop"
            direction, depth = _direction(message), 0 if scope == "node_summary" else 1
            use_graph, reason, signal_group = True, "fallback_graph_neighbors", "graph_topology"
        elif target and entity_count == 1 and (profile_signal or detection_signal or comprehensive_signal or security_signal or ASSET_WORDS.search(message or "")):
            intent, reason, signal_group = "asset_investigation", "fallback_asset_investigation", "asset_evidence"
        elif (
            target
            and entity_followup_signal
            and previous_intent in OPERATIONAL_INTENTS
            and (last_providers or previous_scope not in {None, "none"})
        ):
            previous_route_used = True
            intent = previous_intent  # type: ignore[assignment]
            if entity_count == 2 and previous_intent in {"graph_relationships", "graph_path"}:
                scope = "path" if previous_intent == "graph_path" else "multi_entity_comparison"
                direction, depth, use_graph = "both", 0 if scope == "path" else 1, "graph" in last_providers
            elif entity_count == 1:
                scope = previous_scope if previous_scope in {"none", "node_summary", "one_hop", "full_neighbors", "two_hop"} else "none"  # type: ignore[assignment]
                depth = 2 if scope == "two_hop" else 1 if scope in {"one_hop", "full_neighbors"} else 0
                direction = _direction(message, routing_state.previous_direction if routing_state and routing_state.previous_direction in {"inbound", "outbound", "both"} else "both")  # type: ignore[arg-type]
                use_graph = "graph" in last_providers or previous_scope in {
                    "node_summary", "one_hop", "full_neighbors", "two_hop"
                }
            use_detection = bool(routing_state and routing_state.previous_requires_detection)
            use_asset_profile = bool(routing_state and routing_state.previous_requires_asset_profile)
            reason, signal_group = "fallback_previous_operational_route", "previous_operational_route"

        if (
            bool(getattr(constraints, "require_current", False))
            and target
            and not topic_detached
            and getattr(turn_policy, "episode_transition", "switch") == "keep"
            and previous_intent in OPERATIONAL_INTENTS
        ):
            intent = previous_intent  # type: ignore[assignment]
            scope = previous_scope if previous_scope in {"none", "node_summary", "one_hop", "full_neighbors", "two_hop"} else "none"  # type: ignore[assignment]
            use_graph = use_graph or "graph" in last_providers or scope != "none"
            use_detection = (
                use_detection or "detection" in last_providers
                or bool(routing_state and routing_state.previous_requires_detection)
            )
            use_asset_profile = (
                use_asset_profile or "asset_profile" in last_providers
                or bool(routing_state and routing_state.previous_requires_asset_profile)
            )

        if not topic_detached and 0 < entity_count <= 2:
            if comprehensive_signal or security_signal:
                use_graph = True
                use_detection = True
                use_asset_profile = True
                if entity_count == 1 and scope == "none":
                    intent, scope, direction, depth = "asset_investigation", "node_summary", "both", 0
                elif entity_count == 2 and scope == "none":
                    intent, scope, direction, depth = "graph_relationships", "multi_entity_comparison", "both", 1
                reason, signal_group = "fallback_complete_evidence", "all_product_and_graph_evidence"
            else:
                use_detection = use_detection or detection_signal
                use_asset_profile = use_asset_profile or profile_signal
                if PROFILE_DETECTION_WORDS.search(message or ""):
                    use_detection = use_asset_profile = True
                if BEHAVIOR_PROFILE_WORDS.search(message or ""):
                    use_graph = use_asset_profile = True
            if not use_graph and not use_detection and not use_asset_profile and target and ASSET_WORDS.search(message or ""):
                use_asset_profile = True
            if entity_count == 1 and (use_detection or use_asset_profile) and intent == "general_knowledge":
                intent = "asset_investigation"
            if entity_count == 1 and use_graph and (use_detection or use_asset_profile):
                intent = "asset_investigation"
            if entity_count == 2 and (use_detection or use_asset_profile):
                requires_multiple = True

        if exhaustive_connections and entity_count == 1 and not topic_detached:
            intent = "asset_investigation" if (use_detection or use_asset_profile) else "graph_neighbors"
            scope, direction, depth = "full_neighbors", _direction(message), 1
            use_graph = True
            reason, signal_group = "fallback_exhaustive_connections", "exhaustive_connections"
        elif exhaustive_connections and entity_count == 2 and not topic_detached:
            intent, scope, direction, depth = "graph_relationships", "multi_entity_comparison", "both", 1
            use_graph, requires_multiple = True, True
            reason, signal_group = "fallback_exhaustive_pair_comparison", "exhaustive_pair_comparison"

        decision = RouteDecision(
            use_graph=use_graph,
            reason=reason,
            use_detection=use_detection,
            use_asset_profile=use_asset_profile,
            entity_binding=entity_binding,
            requested_entity_binding=entity_binding,
            resolved_entity_binding=entity_binding,
            binding_source=target.source if target else ("conversation" if entity_binding == "active_pair" else "none"),
            binding_available=bool(entity_count),
            materialized_entity_count=entity_count,
            materialized_entities=tuple(item.value for item in entities.entities),
            target_entity=target if entity_count == 1 else None,
            target_entities=entities.entities,
            matched_signals=[signal_group, *(["security_or_anomaly"] if security_signal else [])],
            graph_intent_detected=use_graph and intent.startswith("graph_"),
            asset_investigation_detected=intent == "asset_investigation",
            followup_detected=bool(entity_followup_signal and target and target.source == "conversation") or previous_route_used,
            intent=intent,
            scope=scope,
            direction=direction,
            depth=depth,
            requires_multiple_entities=requires_multiple,
            relationship_mode="compare" if scope == "multi_entity_comparison" else "direct" if intent == "graph_relationships" else "none",
            intent_confidence=1.0 if (use_graph or use_detection or use_asset_profile) else 0.8,
            decision_source="deterministic_fallback",
            exhaustive_connections_requested=exhaustive_connections,
            fallback_used=True,
            fallback_reason=fallback_reason,
        )
        logger.info(
            "event=deterministic_fallback_route_decided request_id=%s fallback_reason=%s signal_group=%s resolved_entity_count=%s previous_route_used=%s topic_detachment=%s selected_providers=%s intent=%s scope=%s direction=%s depth=%s latency_ms=%s message_preview=%r",
            request_id,
            fallback_reason,
            signal_group,
            entity_count,
            previous_route_used,
            topic_detached,
            ",".join(name for name, enabled in (("graph", use_graph), ("detection", use_detection), ("asset_profile", use_asset_profile)) if enabled) or "none",
            intent,
            scope,
            direction,
            depth,
            int((time.perf_counter() - started) * 1000),
            compact_preview(message),
        )
        return decision


GraphContextRouter = DeterministicFallbackRouter


def normalize_intent_route(decision: IntentDecision, entities: EntityResolution) -> RouteDecision:
    """Convert a validated semantic decision into one executable provider route."""
    target_entities = entities.entities
    count = len(target_entities)
    supported = count in {1, 2}
    requires_graph = bool(decision.requires_graph and supported)
    requires_detection = bool(decision.requires_detection and supported)
    requires_asset_profile = bool(decision.requires_asset_profile and supported)
    requires_knowledge = bool(decision.requires_knowledge)
    target_entity = target_entities[0] if count == 1 else None
    if count > 2 or count == 0:
        requires_graph = requires_detection = requires_asset_profile = False
    return RouteDecision(
        use_graph=requires_graph,
        reason=decision.route_normalization_reason or ("semantic_route" if decision.decision_source.startswith("semantic_router") else decision.fallback_reason or "router_no_provider"),
        use_detection=requires_detection,
        use_asset_profile=requires_asset_profile,
        use_knowledge=requires_knowledge,
        entity_binding=decision.entity_binding,
        requested_entity_binding=decision.requested_entity_binding,
        resolved_entity_binding=decision.entity_binding,
        binding_source=decision.binding_source,
        binding_available=decision.binding_available,
        binding_normalized=decision.binding_normalized,
        binding_normalization_reason=decision.binding_normalization_reason,
        materialized_entity_count=count,
        materialized_entities=tuple(item.value for item in target_entities),
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
        exhaustive_connections_requested=decision.exhaustive_connections_requested,
        semantic_router_called=decision.router_called,
        semantic_router_latency_ms=decision.latency_ms,
        semantic_router_retry_count=decision.retry_count,
        semantic_router_finish_reason=decision.finish_reason,
        semantic_router_content_present=decision.content_present,
        semantic_router_error=decision.error_reason,
        fallback_used=decision.fallback_used,
        fallback_reason=decision.fallback_reason,
        route_normalized=decision.route_normalized,
        route_normalization_reason=decision.route_normalization_reason,
    )
