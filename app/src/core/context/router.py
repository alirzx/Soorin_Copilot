"""Deterministic graph context routing for baseline Copilot."""

from __future__ import annotations

import logging
import re
import time

from src.core.context.models import EntityResolution, IntentDecision, IntentName, RouteDecision, compact_preview
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)

GRAPH_SIGNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "graph": re.compile(r"\bgraph\b", re.IGNORECASE),
    "topology": re.compile(r"\btopolog(?:y|ies)\b", re.IGNORECASE),
    "communication": re.compile(r"\b(?:connections?|connected|communicat(?:e|es|ed|ing|ion|ions))\b", re.IGNORECASE),
    "neighbor_peer": re.compile(r"\b(?:neighbors?|peers?)\b", re.IGNORECASE),
    "direction": re.compile(r"\b(?:inbound|outbound|sends?\s+to|receives?\s+from)\b", re.IGNORECASE),
    "path": re.compile(r"\b(?:path|route|reaches?)\b", re.IGNORECASE),
    "relationship": re.compile(r"\b(?:relationships?|related|linked|talks\s+to|interacts?\s+with)\b", re.IGNORECASE),
    "surrounding_network": re.compile(r"\b(?:network\s+around|surrounding\s+network|surrounding)\b", re.IGNORECASE),
}
GRAPH_FOLLOWUP_PATTERNS: dict[str, re.Pattern[str]] = {
    "continue": re.compile(r"\b(?:continue|continue\s+with\s+this)\b", re.IGNORECASE),
    "deeper": re.compile(r"\b(?:go\s+deeper|more\s+details|explain\s+more|tell\s+me\s+more)\b", re.IGNORECASE),
    "what_else": re.compile(r"\bwhat\s+else\b", re.IGNORECASE),
    "analyze_further": re.compile(r"\b(?:analy[sz]e\s+further|expand\s+the\s+analysis|complete\s+the\s+analysis)\b", re.IGNORECASE),
}
ASSET_INFO_PATTERNS: dict[str, re.Pattern[str]] = {
    "tell_show_explain": re.compile(r"\b(?:tell|show|explain)\b", re.IGNORECASE),
    "knowledge_request": re.compile(
        r"\b(?:what\s+(?:do\s+you|do\s+we|can\s+you|can\s+we)\s+know|what\s+information\s+is\s+available|do\s+you\s+have\s+(?:any\s+)?(?:info|information|anything\s+useful))\b",
        re.IGNORECASE,
    ),
    "all_available": re.compile(r"\b(?:give\s+me\s+all|all\s+you\s+know|everything\s+you\s+know|whatever\s+we\s+know)\b", re.IGNORECASE),
    "investigate": re.compile(r"\b(?:analy[sz]e|investigate)\b", re.IGNORECASE),
    "what_about": re.compile(r"\b(?:what\s+about|how\s+about)\b", re.IGNORECASE),
}
ENTITY_REFERENCE_PATTERN = re.compile(
    r"\b(?:asset|ip|host|node|this|it|this\s+one|that\s+host|that\s+node|that\s+ip)\b|(?:\d{1,3}\.){3}\d{1,3}",
    re.IGNORECASE,
)


class GraphContextRouter:
    """Select graph evidence only when the message asks for relationships."""

    def route(
        self,
        message: str,
        entities: EntityResolution,
        routing_state: SessionRoutingState | None = None,
        *,
        intent_decision: IntentDecision | None = None,
        request_id: str = "",
    ) -> RouteDecision:
        started = time.perf_counter()
        logger.info(
            "event=graph_router_start request_id=%s entity_status=%s message_preview=%r",
            request_id,
            entities.status,
            compact_preview(message),
        )
        matched = [name for name, pattern in GRAPH_SIGNAL_PATTERNS.items() if pattern.search(message or "")]
        followups = [name for name, pattern in GRAPH_FOLLOWUP_PATTERNS.items() if pattern.search(message or "")]
        info_matches = [name for name, pattern in ASSET_INFO_PATTERNS.items() if pattern.search(message or "")]
        has_entity_reference = bool(ENTITY_REFERENCE_PATTERN.search(message or ""))
        asset_matches = info_matches if info_matches and (has_entity_reference or bool(entities.primary_entity)) else []
        graph_intent_detected = bool(matched)
        asset_investigation_detected = bool(asset_matches)
        followup_detected = bool(followups)
        last_provider = routing_state.last_provider if routing_state else None
        intent_name: IntentName = intent_decision.intent if intent_decision else "unclear"
        intent_confidence = intent_decision.confidence if intent_decision else 0.0
        decision_source = intent_decision.decision_source if intent_decision else "deterministic"
        glm_router_called = bool(intent_decision and intent_decision.router_called)
        glm_router_latency_ms = intent_decision.latency_ms if intent_decision else 0
        glm_router_error = intent_decision.error_reason if intent_decision else None

        if entities.status == "ambiguous":
            decision = RouteDecision(
                use_graph=False,
                reason="ambiguous_entities",
                target_entity=None,
                matched_signals=matched,
                graph_intent_detected=graph_intent_detected,
                asset_investigation_detected=asset_investigation_detected,
                followup_detected=followup_detected,
                intent="unclear",
                decision_source="deterministic",
            )
        elif entities.primary_entity and entities.primary_entity.source == "ui":
            decision = RouteDecision(
                use_graph=True,
                reason="ui_selected_graph_context",
                target_entity=entities.primary_entity,
                matched_signals=["ui_selected_entity"],
                graph_intent_detected=graph_intent_detected,
                asset_investigation_detected=asset_investigation_detected,
                followup_detected=followup_detected,
                intent="asset_investigation" if asset_investigation_detected else "general_knowledge",
                intent_confidence=1.0,
                decision_source="deterministic",
            )
        elif graph_intent_detected and not entities.primary_entity:
            decision = RouteDecision(
                use_graph=False,
                reason="graph_intent_without_entity",
                target_entity=None,
                matched_signals=matched,
                graph_intent_detected=True,
                asset_investigation_detected=asset_investigation_detected,
                followup_detected=followup_detected,
                intent="unclear",
                decision_source="deterministic",
            )
        elif not entities.primary_entity:
            decision = RouteDecision(
                use_graph=False,
                reason="no_resolved_entity",
                target_entity=None,
                matched_signals=matched or asset_matches,
                graph_intent_detected=graph_intent_detected,
                asset_investigation_detected=asset_investigation_detected,
                followup_detected=followup_detected,
                intent="unclear",
                decision_source="deterministic",
            )
        elif graph_intent_detected:
            if any(signal == "path" for signal in matched):
                routed_intent: IntentName = "graph_path"
            elif any(signal in {"neighbor_peer", "direction"} for signal in matched):
                routed_intent = "graph_neighbors"
            else:
                routed_intent = "graph_relationships"
            decision = RouteDecision(
                use_graph=True,
                reason="graph_intent_with_entity",
                target_entity=entities.primary_entity,
                matched_signals=matched,
                graph_intent_detected=True,
                asset_investigation_detected=asset_investigation_detected,
                followup_detected=followup_detected,
                intent=routed_intent,
                intent_confidence=1.0,
                decision_source="deterministic",
            )
        elif asset_investigation_detected:
            decision = RouteDecision(
                use_graph=True,
                reason="asset_investigation_with_entity",
                target_entity=entities.primary_entity,
                matched_signals=asset_matches,
                graph_intent_detected=False,
                asset_investigation_detected=True,
                followup_detected=followup_detected,
                intent="asset_investigation",
                intent_confidence=1.0,
                decision_source="deterministic",
            )
        elif followup_detected and last_provider == "graph" and entities.primary_entity:
            decision = RouteDecision(
                use_graph=True,
                reason="graph_followup_with_active_entity",
                target_entity=entities.primary_entity,
                matched_signals=[],
                graph_intent_detected=False,
                asset_investigation_detected=False,
                followup_detected=True,
                intent="graph_followup",
                intent_confidence=1.0,
                decision_source="deterministic",
            )
        elif intent_decision and intent_decision.use_graph and entities.primary_entity:
            decision = RouteDecision(
                use_graph=True,
                reason="glm_intent_with_entity" if intent_decision.decision_source == "glm" else "fallback_intent_with_entity",
                target_entity=entities.primary_entity,
                matched_signals=[intent_decision.intent],
                graph_intent_detected=intent_decision.intent.startswith("graph_"),
                asset_investigation_detected=intent_decision.intent == "asset_investigation",
                followup_detected=intent_decision.is_followup,
                intent=intent_name,
                intent_confidence=intent_confidence,
                decision_source=decision_source,
                glm_router_called=glm_router_called,
                glm_router_latency_ms=glm_router_latency_ms,
                glm_router_error=glm_router_error,
            )
        elif intent_decision:
            decision = RouteDecision(
                use_graph=False,
                reason="intent_router_no_graph",
                target_entity=entities.primary_entity,
                matched_signals=[intent_decision.intent],
                graph_intent_detected=False,
                asset_investigation_detected=False,
                followup_detected=intent_decision.is_followup,
                intent=intent_name,
                intent_confidence=intent_confidence,
                decision_source=decision_source,
                glm_router_called=glm_router_called,
                glm_router_latency_ms=glm_router_latency_ms,
                glm_router_error=glm_router_error,
            )
        else:
            decision = RouteDecision(
                use_graph=False,
                reason="no_graph_intent_signal",
                target_entity=entities.primary_entity,
                matched_signals=[],
                graph_intent_detected=False,
                asset_investigation_detected=asset_investigation_detected,
                followup_detected=followup_detected,
                intent="unclear",
                decision_source="deterministic",
                should_call_intent_router=bool(entities.primary_entity),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=context_route_decided request_id=%s use_graph=%s reason=%s target_ip=%s target_source=%s graph_intent_detected=%s asset_investigation_detected=%s followup_detected=%s intent=%s decision_source=%s glm_router_called=%s last_provider=%s matched_signal_count=%s matched_signals_preview=%s latency_ms=%s",
            request_id,
            decision.use_graph,
            decision.reason,
            decision.target_entity.value if decision.target_entity else "",
            decision.target_entity.source if decision.target_entity else "",
            decision.graph_intent_detected,
            decision.asset_investigation_detected,
            decision.followup_detected,
            decision.intent,
            decision.decision_source,
            decision.glm_router_called,
            last_provider or "",
            len(decision.matched_signals),
            ",".join(decision.matched_signals[:5]),
            latency_ms,
        )
        return decision
