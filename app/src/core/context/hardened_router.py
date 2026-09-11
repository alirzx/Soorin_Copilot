"""Small semantic-router adapter for production structured-query hardening."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.core.context.models import EntityResolution, IntentDecision
from src.core.context.structured_hardening import normalize_structured_query_for_language
from src.core.context.structured_routing import SemanticIntentRouter as BaseSemanticIntentRouter
from src.core.memory.routing_state import SessionRoutingState


class SemanticIntentRouter(BaseSemanticIntentRouter):
    """Normalize validated Phase-4 queries without changing legacy routes."""

    def _decision_from_content(
        self,
        content: str,
        finish_reason: str | None,
        entities: EntityResolution,
        routing_context: dict[str, Any],
        routing_state: SessionRoutingState | None,
        ui_context: dict[str, Any] | None,
        request_id: str,
    ) -> IntentDecision:
        decision = super()._decision_from_content(
            content,
            finish_reason,
            entities,
            routing_context,
            routing_state,
            ui_context,
            request_id,
        )
        if decision.structured_query is None:
            return decision
        return replace(
            decision,
            structured_query=normalize_structured_query_for_language(
                decision.structured_query,
                str(routing_context.get("message") or ""),
            ),
        )
