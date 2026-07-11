"""Small GLM-backed intent classification for context routing."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from src.config.settings import Settings
from src.core.context.models import EntityResolution, IntentDecision, IntentName, compact_preview
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)

ALLOWED_INTENTS: set[str] = {
    "general_knowledge",
    "asset_investigation",
    "graph_neighbors",
    "graph_relationships",
    "graph_path",
    "graph_followup",
    "unrelated",
    "unclear",
}
GRAPH_INTENTS = {
    "asset_investigation",
    "graph_neighbors",
    "graph_relationships",
    "graph_path",
    "graph_followup",
}

ROUTER_SYSTEM_PROMPT = """You classify Soorin Cyber Copilot routing intent.
Return only compact JSON. Do not answer the user.
Never invent IPs or entities. Use only the deterministic entity fields supplied.
Allowed intent values: general_knowledge, asset_investigation, graph_neighbors, graph_relationships, graph_path, graph_followup, unrelated, unclear.
Schema:
{"intent":"asset_investigation","use_graph":true,"is_followup":false,"target_reference":"resolved_entity","confidence":0.0,"reason":"short reason"}
"""


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    if not stripped.startswith("{"):
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise ValueError("missing_json_object")
        stripped = match.group(0)
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("json_not_object")
    return payload


class GLMIntentRouter:
    """Classify intent only; deterministic entity resolution remains authoritative."""

    def __init__(self, settings: Settings, llm_client: LLMClient) -> None:
        self.settings = settings
        self.llm_client = llm_client

    def disabled_decision(self, reason: str = "disabled") -> IntentDecision:
        return IntentDecision(
            intent="unclear",
            use_graph=False,
            confidence=0.0,
            reason=reason,
            decision_source="disabled",
            router_called=False,
            error_reason=reason,
        )

    def classify(
        self,
        message: str,
        entities: EntityResolution,
        routing_state: SessionRoutingState,
        *,
        ui_context: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> IntentDecision:
        if not self.settings.intent_router_enabled:
            return self.disabled_decision()

        started = time.perf_counter()
        routing_input = {
            "message": compact_preview(message, limit=280),
            "entity_status": entities.status,
            "primary_entity": {
                "type": entities.primary_entity.type,
                "value": entities.primary_entity.value,
                "source": entities.primary_entity.source,
            }
            if entities.primary_entity
            else None,
            "explicit_candidate_count": entities.explicit_candidate_count,
            "valid_entity_count": entities.valid_entity_count,
            "reference_detected": entities.reference_detected,
            "reference_type": entities.reference_type,
            "reference_suppressed": entities.reference_suppressed,
            "ui_selected_ip_present": bool((ui_context or {}).get("selected_ip")),
            "active_ip_present": bool(routing_state.active_ip),
            "last_provider": routing_state.last_provider,
            "allowed_intents": sorted(ALLOWED_INTENTS),
        }
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(routing_input, sort_keys=True)},
        ]
        logger.info(
            "event=intent_router_start request_id=%s entity_status=%s primary_entity_source=%s message_preview=%r",
            request_id,
            entities.status,
            entities.primary_entity.source if entities.primary_entity else "",
            compact_preview(message),
        )

        try:
            result = self.llm_client.chat(
                messages,
                request_id=request_id,
                max_tokens=300,
                temperature=0.0,
                top_p=1.0,
                timeout_seconds=self.settings.intent_router_timeout_seconds,
                purpose="intent_router",
            )
            payload = _json_from_text(result.text)
            intent = str(payload.get("intent") or "unclear")
            if intent not in ALLOWED_INTENTS:
                raise ValueError(f"unsupported_intent:{intent}")
            confidence = float(payload.get("confidence") or 0.0)
            if confidence < self.settings.intent_router_min_confidence:
                raise ValueError(f"low_confidence:{confidence}")
            use_graph = bool(payload.get("use_graph")) and intent in GRAPH_INTENTS and bool(entities.primary_entity)
            decision = IntentDecision(
                intent=intent,  # type: ignore[arg-type]
                use_graph=use_graph,
                is_followup=bool(payload.get("is_followup")),
                target_reference=str(payload.get("target_reference") or "none")[:80],
                confidence=min(1.0, max(0.0, confidence)),
                reason=str(payload.get("reason") or "")[:180],
                decision_source="glm",
                router_called=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except (LLMError, ValueError, TypeError, json.JSONDecodeError) as exc:
            decision = IntentDecision(
                intent="unclear",
                use_graph=False,
                confidence=0.0,
                reason="Intent router failed; falling back to deterministic routing.",
                decision_source="fallback",
                router_called=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_reason=str(exc)[:120],
            )

        logger.info(
            "event=intent_router_complete request_id=%s decision_source=%s intent=%s use_graph=%s confidence=%s latency_ms=%s error_reason=%s",
            request_id,
            decision.decision_source,
            decision.intent,
            decision.use_graph,
            decision.confidence,
            decision.latency_ms,
            decision.error_reason or "",
        )
        return decision
