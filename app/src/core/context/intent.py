"""LLM-primary semantic route classification with deterministic safety validation."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from src.config.settings import Settings
from src.core.context.models import (
    EntityResolution,
    GraphDirection,
    GraphScope,
    IntentDecision,
    IntentName,
    compact_preview,
)
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
    "unclear",
}
ALLOWED_SCOPES: set[str] = {"none", "node_summary", "one_hop", "full_neighbors", "two_hop", "path"}
ALLOWED_DIRECTIONS: set[str] = {"none", "inbound", "outbound", "both"}

ROUTER_SYSTEM_PROMPT = """You are the Soorin Copilot semantic graph router.
Return exactly one JSON object. No markdown. No explanation outside JSON.
Do not answer the user. Do not invent, extract, replace, or override entities.
Use only deterministic entity fields supplied by the backend.
Use only allowed enums. Return a final concrete scope. Never return inherit.
Respect previous routing state. Distinguish direct adjacency from path.
Distinguish full direct neighbors from bounded summaries.
Distinguish general knowledge from selected-entity context.
Treat explicit topic detachment as graph-irrelevant for the current turn.
Never request depth greater than 2. Avoid long reasoning. Return JSON immediately.

Allowed intents: general_knowledge, asset_investigation, graph_neighbors, graph_relationships, graph_path, graph_followup, unclear.
Allowed scopes: none, node_summary, one_hop, full_neighbors, two_hop, path.
Allowed directions: none, inbound, outbound, both.

Semantics:
General asset investigation -> asset_investigation, node_summary, depth 0.
Direct neighbors -> graph_neighbors, one_hop, depth 1.
All/every/full/complete direct neighbors -> graph_neighbors, full_neighbors, depth 1.
Wider surrounding topology or two hops -> graph_neighbors, two_hop, depth 2.
Direct edge or immediate adjacency between two entities -> graph_relationships, one_hop, depth 1.
Route, shortest path, reachability, chain, or intermediate nodes -> graph_path, path, depth 0.
Conceptual question -> general_knowledge, none, requires_graph=false.
Vague request without usable context -> unclear, none, requires_graph=false.

Direction:
incoming, connects to this, sources, send toward -> inbound.
outgoing, destinations, reaches, sends to -> outbound.
communicates with, surrounding, connected with -> both.

Schema:
{"intent":"graph_neighbors","scope":"full_neighbors","direction":"inbound","depth":1,"requires_graph":true,"requires_multiple_entities":false,"is_followup":false,"classification_confidence":0.95,"reason":"short reason"}
"""


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    if not stripped.startswith("{"):
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise ValueError("malformed_json")
        stripped = match.group(0)
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("malformed_json")
    return payload


def build_routing_context(
    message: str,
    entities: EntityResolution,
    routing_state: SessionRoutingState,
    *,
    ui_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    primary = entities.primary_entity
    return {
        "message": compact_preview(message, limit=360),
        "entity_status": entities.status,
        "entity_types": sorted({entity.type for entity in entities.entities}),
        "entity_count": len(entities.entities),
        "resolved_entity_count": len(entities.entities),
        "resolved_entity_source": primary.source if primary else "",
        "resolved_entities": [
            {"type": entity.type, "value": entity.value, "source": entity.source}
            for entity in entities.entities[:2]
        ],
        "active_entity_present": bool(routing_state.active_ip),
        "ui_selected_entity_present": bool((ui_context or {}).get("selected_ip")),
        "previous_provider": routing_state.last_provider,
        "previous_intent": routing_state.previous_intent,
        "previous_scope": routing_state.previous_scope,
        "previous_direction": routing_state.previous_direction,
        "previous_depth": routing_state.previous_depth,
        "explicit_topic_detachment": entities.reference_suppressed,
    }


def validate_router_payload(
    payload: dict[str, Any],
    entities: EntityResolution,
    *,
    min_confidence: float,
) -> IntentDecision:
    required = {
        "intent",
        "scope",
        "direction",
        "depth",
        "requires_graph",
        "requires_multiple_entities",
        "is_followup",
        "classification_confidence",
        "reason",
    }
    if missing := sorted(required.difference(payload)):
        raise ValueError(f"schema_validation_failed:missing={','.join(missing)}")

    intent = str(payload["intent"])
    scope = str(payload["scope"])
    direction = str(payload["direction"])
    if intent not in ALLOWED_INTENTS:
        raise ValueError("unsupported_enum:intent")
    if scope not in ALLOWED_SCOPES or scope == "inherit":
        raise ValueError("unsupported_enum:scope")
    if direction not in ALLOWED_DIRECTIONS or direction == "inherit":
        raise ValueError("unsupported_enum:direction")

    try:
        confidence = float(payload["classification_confidence"])
    except (TypeError, ValueError) as exc:
        raise ValueError("schema_validation_failed:classification_confidence") from exc
    if confidence < 0 or confidence > 1:
        raise ValueError("schema_validation_failed:classification_confidence_range")
    if confidence < min_confidence:
        raise ValueError("low_confidence")

    try:
        depth = int(payload["depth"])
    except (TypeError, ValueError) as exc:
        raise ValueError("schema_validation_failed:depth") from exc
    if depth < 0 or depth > 2:
        raise ValueError("schema_validation_failed:depth_range")

    requires_graph = bool(payload["requires_graph"])
    requires_multiple = bool(payload["requires_multiple_entities"])
    if scope == "full_neighbors" and depth != 1:
        raise ValueError("schema_validation_failed:full_neighbors_depth")
    if scope == "two_hop" and depth != 2:
        raise ValueError("schema_validation_failed:two_hop_depth")
    if scope == "path" and depth != 0:
        raise ValueError("schema_validation_failed:path_depth")
    if intent == "general_knowledge" and scope != "none":
        raise ValueError("schema_validation_failed:general_scope")
    if intent == "graph_path" and scope != "path":
        raise ValueError("schema_validation_failed:path_scope")
    if intent == "graph_relationships" and len(entities.entities) != 2:
        raise ValueError("entity_requirement_failed")
    if requires_multiple and len(entities.entities) != 2:
        raise ValueError("entity_requirement_failed")
    if requires_graph and not entities.entities:
        raise ValueError("entity_requirement_failed")

    return IntentDecision(
        intent=intent,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        depth=depth,
        requires_graph=requires_graph,
        requires_multiple_entities=requires_multiple,
        is_followup=bool(payload["is_followup"]),
        classification_confidence=confidence,
        reason=str(payload.get("reason") or "")[:220],
        decision_source="glm",
        router_called=True,
        content_present=True,
    )


class GLMIntentRouter:
    """Call the answer provider once to classify routing, never to select entities."""

    def __init__(self, settings: Settings, llm_client: LLMClient) -> None:
        self.settings = settings
        self.llm_client = llm_client

    def disabled_decision(self, reason: str = "router_disabled") -> IntentDecision:
        return IntentDecision(
            intent="unclear",
            scope="none",
            direction="none",
            depth=0,
            requires_graph=False,
            classification_confidence=0.0,
            reason=reason,
            decision_source="disabled",
            router_called=False,
            error_reason=reason,
            fallback_used=True,
            fallback_reason=reason,
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

        routing_context = build_routing_context(message, entities, routing_state, ui_context=ui_context)
        return self._classify_with_context(routing_context, entities, request_id=request_id)

    def _classify_with_context(
        self,
        routing_context: dict[str, Any],
        entities: EntityResolution,
        *,
        request_id: str = "",
    ) -> IntentDecision:
        started = time.perf_counter()
        retry_count = 0
        last_error = ""
        finish_reason = None
        content_present = False
        max_tokens = self.settings.intent_router_max_tokens
        attempts = 2 if self.settings.intent_router_retry_enabled else 1

        for attempt in range(attempts):
            messages = [
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(routing_context, sort_keys=True)},
            ]
            if attempt:
                messages[0]["content"] += "\nRetry: return JSON immediately. No reasoning text. No markdown."
            logger.info(
                "event=intent_router_start request_id=%s attempt=%s entity_status=%s entity_count=%s previous_scope=%s router_temperature=%s router_top_p=%s router_max_tokens=%s router_retry_max_tokens=%s",
                request_id,
                attempt + 1,
                routing_context.get("entity_status"),
                routing_context.get("entity_count"),
                routing_context.get("previous_scope") or "",
                self.settings.intent_router_temperature,
                self.settings.intent_router_top_p,
                max_tokens,
                self.settings.intent_router_retry_max_tokens,
            )
            try:
                result = self.llm_client.chat(
                    messages,
                    request_id=request_id,
                    max_tokens=max_tokens,
                    temperature=self.settings.intent_router_temperature,
                    top_p=self.settings.intent_router_top_p,
                    timeout_seconds=self.settings.intent_router_timeout_seconds,
                    purpose="intent_router",
                )
            except LLMError as exc:
                last_error = "provider_error"
                latency_ms = int((time.perf_counter() - started) * 1000)
                return self._failure("provider_error", latency_ms, retry_count, finish_reason, content_present, str(exc))

            finish_reason = result.finish_reason
            content = (result.text or "").strip()
            content_present = bool(content)
            try:
                if not content:
                    raise ValueError("missing_content")
                if finish_reason == "length":
                    raise ValueError("finish_reason_length")
                payload = _json_from_text(content)
                decision = validate_router_payload(
                    payload,
                    entities,
                    min_confidence=self.settings.intent_router_min_confidence,
                )
                decision = IntentDecision(
                    **{
                        **decision.__dict__,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                        "retry_count": retry_count,
                        "finish_reason": finish_reason,
                        "content_present": content_present,
                    }
                )
                logger.info(
                    "event=intent_router_complete request_id=%s decision_source=glm intent=%s scope=%s direction=%s depth=%s requires_graph=%s confidence=%s retry_count=%s latency_ms=%s finish_reason=%s content_present=%s",
                    request_id,
                    decision.intent,
                    decision.scope,
                    decision.direction,
                    decision.depth,
                    decision.requires_graph,
                    decision.classification_confidence,
                    retry_count,
                    decision.latency_ms,
                    finish_reason or "",
                    content_present,
                )
                return decision
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                retryable = last_error in {"missing_content", "finish_reason_length", "malformed_json"} or "malformed_json" in last_error
                if attempt == 0 and retryable and attempts > 1:
                    retry_count = 1
                    max_tokens = self.settings.intent_router_retry_max_tokens
                    continue
                break

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._failure(last_error or "schema_validation_failed", latency_ms, retry_count, finish_reason, content_present)

    def _failure(
        self,
        reason: str,
        latency_ms: int,
        retry_count: int,
        finish_reason: str | None,
        content_present: bool,
        detail: str = "",
    ) -> IntentDecision:
        logger.warning(
            "event=intent_router_failed reason=%s retry_count=%s latency_ms=%s finish_reason=%s content_present=%s detail=%s",
            reason,
            retry_count,
            latency_ms,
            finish_reason or "",
            content_present,
            detail[:120],
        )
        return IntentDecision(
            intent="unclear",
            scope="none",
            direction="none",
            depth=0,
            requires_graph=False,
            classification_confidence=0.0,
            reason="Router failed; deterministic fallback required.",
            decision_source="fallback",
            router_called=True,
            latency_ms=latency_ms,
            retry_count=retry_count,
            finish_reason=finish_reason,
            content_present=content_present,
            error_reason=reason,
            fallback_used=True,
            fallback_reason=reason,
        )
