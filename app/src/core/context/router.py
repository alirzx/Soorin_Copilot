"""Deterministic graph context routing for baseline Copilot."""

from __future__ import annotations

import logging
import re

from src.core.context.models import EntityResolution, RouteDecision, compact_preview


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


class GraphContextRouter:
    """Select graph evidence only when the message asks for relationships."""

    def route(
        self,
        message: str,
        entities: EntityResolution,
        *,
        request_id: str = "",
    ) -> RouteDecision:
        logger.info(
            "event=graph_router_start request_id=%s entity_status=%s message_preview=%r",
            request_id,
            entities.status,
            compact_preview(message),
        )
        matched = [name for name, pattern in GRAPH_SIGNAL_PATTERNS.items() if pattern.search(message or "")]

        if entities.status == "ambiguous":
            decision = RouteDecision(
                use_graph=False,
                reason="ambiguous_entities",
                target_entity=None,
                matched_signals=matched,
            )
        elif not entities.primary_entity:
            decision = RouteDecision(
                use_graph=False,
                reason="no_resolved_entity",
                target_entity=None,
                matched_signals=matched,
            )
        elif not matched:
            decision = RouteDecision(
                use_graph=False,
                reason="no_graph_intent_signal",
                target_entity=entities.primary_entity,
                matched_signals=[],
            )
        else:
            decision = RouteDecision(
                use_graph=True,
                reason="graph_intent_with_entity",
                target_entity=entities.primary_entity,
                matched_signals=matched,
            )

        logger.info(
            "event=graph_router_decision request_id=%s use_graph=%s reason=%s target_ip=%s matched_count=%s matched_preview=%s",
            request_id,
            decision.use_graph,
            decision.reason,
            decision.target_entity.value if decision.target_entity else "",
            len(decision.matched_signals),
            ",".join(decision.matched_signals[:5]),
        )
        return decision
