"""Small semantic-router adapter for production structured-query hardening."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.core.context.models import EntityResolution, IntentDecision
from src.core.context.structured_hardening import normalize_structured_query_for_language
from src.core.context.structured_routing import SemanticIntentRouter as BaseSemanticIntentRouter
from src.core.memory.routing_state import SessionRoutingState


_STRUCTURED_HARDENING_PROMPT = """

## Structured selector hardening
For Asset-set search, `classification_summary` is also an allowed exact string selector.
Text selector matching is semantic case-insensitive equality: preserve the user's value meaning and do not use capitalization as a differentiator. This applies to asset_name, status, suggested_type, classification_summary, vendor, product, role, roles, tag, sub_tag, and enrichment_status. IP remains canonical IP equality; confidence/unknown scores use numeric ranges; last_detection_at uses timestamp ranges.
Distinguish strict wording from inclusive wording: "above/greater than/more than/over X" means strictly greater than X; "below/less than/under X" means strictly less than X; "at least X" and "at most X" are inclusive.
For "highest/top" or "lowest/bottom" requests, emit the appropriate sort field and direction. The runtime may retrieve an additional leading row to determine whether the top value is tied; do not treat that bounded tie-check as a failure.
""".strip()


class SemanticIntentRouter(BaseSemanticIntentRouter):
    """Normalize validated Phase-4 queries without changing legacy routes."""

    def __init__(self, settings: Any, llm_client: Any) -> None:
        super().__init__(settings, llm_client)
        self.system_prompt = f"{self.system_prompt}\n\n{_STRUCTURED_HARDENING_PROMPT}"

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
