"""Deterministic graph context routing for baseline Copilot."""

from __future__ import annotations

import logging
import re
import time

from src.core.context.models import EntityResolution, RouteDecision, compact_preview
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)

GRAPH_SIGNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "graph": re.compile(r"\bgraph\b", re.IGNORECASE),
    "topology": re.compile(r"\btopolog(?:y|ies)\b", re.IGNORECASE),
    "connection": re.compile(r"\bconnections?\b", re.IGNORECASE),
    "connected": re.compile(r"\bconnected\b", re.IGNORECASE),
    "communicate": re.compile(r"\bcommunicat(?:e|es|ed|ing|ion|ions)\b", re.IGNORECASE),
    "neighbor": re.compile(r"\bneighbors?\b", re.IGNORECASE),
    "peer": re.compile(r"\bpeers?\b", re.IGNORECASE),
    "inbound": re.compile(r"\binbound\b", re.IGNORECASE),
    "outbound": re.compile(r"\boutbound\b", re.IGNORECASE),
    "path": re.compile(r"\bpath\b", re.IGNORECASE),
    "relationship": re.compile(r"\brelationship\b|\brelated\b", re.IGNORECASE),
    "network around": re.compile(r"\bnetwork\s+around\b|\bsurrounding\b", re.IGNORECASE),
}
GRAPH_FOLLOWUP_PATTERNS: dict[str, re.Pattern[str]] = {
    "tell_me_more": re.compile(r"\btell\s+me\s+more\b", re.IGNORECASE),
    "explain_more": re.compile(r"\bexplain\s+more\b", re.IGNORECASE),
    "continue": re.compile(r"\bcontinue(?:\s+the\s+analysis)?\b", re.IGNORECASE),
    "more_about": re.compile(r"\bmore\s+about\s+(?:this|the|same)\s+(?:asset|ip|host|node)\b", re.IGNORECASE),
    "what_else": re.compile(r"\bwhat\s+else\b", re.IGNORECASE),
    "analyze_more": re.compile(r"\banaly[sz]e\s+more\b", re.IGNORECASE),
    "complete_analysis": re.compile(r"\bcomplete\s+the\s+analysis\b", re.IGNORECASE),
}
ASSET_INVESTIGATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "tell_about_entity": re.compile(
        r"\btell\s+me\s+(?:all\s+|everything\s+)?(?:you\s+know\s+)?about\s+(?:this|the|same)?\s*(?:asset|ip|host|node)\b",
        re.IGNORECASE,
    ),
    "what_is_entity": re.compile(r"\bwhat\s+is\s+(?:this|the|same)?\s*(?:asset|ip|host|node)\b", re.IGNORECASE),
    "what_know_entity": re.compile(
        r"\bwhat\s+(?:do\s+we|can\s+we)\s+know\s+about\s+(?:this|the|same)?\s*(?:asset|ip|host|node)\b",
        re.IGNORECASE,
    ),
    "investigate_entity": re.compile(
        r"\b(?:analy[sz]e|investigate|explain)\s+(?:this|the|same)?\s*(?:asset|ip|host|node)\b",
        re.IGNORECASE,
    ),
    "tell_about_ip_literal": re.compile(
        r"\btell\s+me\s+(?:all\s+|everything\s+)?(?:you\s+know\s+)?about\s+(?:\d{1,3}\.){3}\d{1,3}\b",
        re.IGNORECASE,
    ),
    "what_know_ip_literal": re.compile(
        r"\bwhat\s+(?:do\s+we|can\s+we)\s+know\s+about\s+(?:\d{1,3}\.){3}\d{1,3}\b",
        re.IGNORECASE,
    ),
    "investigate_ip_literal": re.compile(
        r"\b(?:analy[sz]e|investigate)\s+(?:\d{1,3}\.){3}\d{1,3}\b",
        re.IGNORECASE,
    ),
}


class GraphContextRouter:
    """Select graph evidence only when the message asks for relationships."""

    def route(
        self,
        message: str,
        entities: EntityResolution,
        routing_state: SessionRoutingState | None = None,
        *,
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
        asset_matches = [
            name for name, pattern in ASSET_INVESTIGATION_PATTERNS.items() if pattern.search(message or "")
        ]
        graph_intent_detected = bool(matched)
        asset_investigation_detected = bool(asset_matches)
        followup_detected = bool(followups)
        last_provider = routing_state.last_provider if routing_state else None

        if entities.status == "ambiguous":
            decision = RouteDecision(
                use_graph=False,
                reason="ambiguous_entities",
                target_entity=None,
                matched_signals=matched,
                graph_intent_detected=graph_intent_detected,
                asset_investigation_detected=asset_investigation_detected,
                followup_detected=followup_detected,
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
            )
        elif not graph_intent_detected and not asset_investigation_detected and not followup_detected:
            decision = RouteDecision(
                use_graph=False,
                reason="no_graph_intent_signal",
                target_entity=entities.primary_entity,
                matched_signals=[],
                graph_intent_detected=False,
                asset_investigation_detected=False,
                followup_detected=False,
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
            )
        elif graph_intent_detected:
            decision = RouteDecision(
                use_graph=True,
                reason="graph_intent_with_entity",
                target_entity=entities.primary_entity,
                matched_signals=matched,
                graph_intent_detected=True,
                asset_investigation_detected=asset_investigation_detected,
                followup_detected=followup_detected,
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
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=context_route_decided request_id=%s use_graph=%s reason=%s target_ip=%s target_source=%s graph_intent_detected=%s asset_investigation_detected=%s followup_detected=%s last_provider=%s matched_signal_count=%s matched_signals_preview=%s latency_ms=%s",
            request_id,
            decision.use_graph,
            decision.reason,
            decision.target_entity.value if decision.target_entity else "",
            decision.target_entity.source if decision.target_entity else "",
            decision.graph_intent_detected,
            decision.asset_investigation_detected,
            decision.followup_detected,
            last_provider or "",
            len(decision.matched_signals),
            ",".join(decision.matched_signals[:5]),
            latency_ms,
        )
        return decision
