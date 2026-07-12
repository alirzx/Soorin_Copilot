"""LLM-primary semantic route classification with deterministic safety validation."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
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
ALLOWED_SCOPES: set[str] = {"none", "node_summary", "one_hop", "full_neighbors", "two_hop", "path", "multi_entity_comparison"}
ALLOWED_DIRECTIONS: set[str] = {"none", "inbound", "outbound", "both"}

ROUTER_SYSTEM_PROMPT_FALLBACK = (
    "You classify Soorin Copilot routing only. Return exactly one JSON object. "
    "Do not answer the user. Use only supplied deterministic entities. "
    "Allowed scopes include none, node_summary, one_hop, full_neighbors, two_hop, path, "
    "and multi_entity_comparison. Never request depth greater than 2."
)


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
        "entity_mode": entities.entity_mode,
        "entity_types": sorted({entity.type for entity in entities.entities}),
        "entity_count": len(entities.entities),
        "resolved_entity_count": len(entities.entities),
        "resolved_entity_source": primary.source if primary else "",
        "resolved_entities": [
            {"type": entity.type, "value": entity.value, "source": entity.source}
            for entity in entities.entities[:2]
        ],
        "active_entity_present": bool(routing_state.active_ip),
        "active_entities_present": bool(routing_state.active_entities),
        "active_entity_count": len(routing_state.active_entities),
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
    entity_count = len(entities.entities)
    route_normalized = False
    route_normalization_reason = None
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
    if intent == "asset_investigation" and entity_count == 1:
        if (
            scope != "node_summary"
            or direction != "both"
            or depth != 0
            or not requires_graph
            or requires_multiple
        ):
            route_normalized = True
            route_normalization_reason = "node_summary_requires_graph"
        scope = "node_summary"
        direction = "both"
        depth = 0
        requires_graph = True
        requires_multiple = False
    if intent == "graph_neighbors" and (requires_multiple or entity_count == 2):
        intent = "graph_relationships"
        scope = "multi_entity_comparison"
        direction = "both"
        depth = 1
        requires_graph = True
        requires_multiple = True
        route_normalized = True
        route_normalization_reason = "multi_entity_neighbors_to_relationship"
    if scope == "full_neighbors" and depth != 1:
        raise ValueError("schema_validation_failed:full_neighbors_depth")
    if scope == "two_hop" and depth != 2:
        raise ValueError("schema_validation_failed:two_hop_depth")
    if scope == "path" and depth != 0:
        raise ValueError("schema_validation_failed:path_depth")
    if scope == "multi_entity_comparison" and depth != 1:
        raise ValueError("schema_validation_failed:comparison_depth")
    if intent == "general_knowledge" and scope != "none":
        raise ValueError("schema_validation_failed:general_scope")
    if intent == "general_knowledge" and requires_graph:
        raise ValueError("schema_validation_failed:general_requires_graph")
    if intent == "graph_path" and scope != "path":
        raise ValueError("schema_validation_failed:path_scope")
    if intent == "graph_path" and not requires_multiple:
        raise ValueError("schema_validation_failed:path_requires_multiple")
    if intent == "graph_path" and entity_count != 2:
        raise ValueError("entity_requirement_failed")
    if intent == "graph_neighbors" and requires_multiple:
        raise ValueError("schema_validation_failed:neighbors_multi_entity")
    if intent == "graph_neighbors" and entity_count != 1:
        raise ValueError("entity_requirement_failed")
    if intent == "asset_investigation" and entity_count != 1:
        raise ValueError("entity_requirement_failed")
    if intent == "graph_relationships" and entity_count != 2:
        raise ValueError("entity_requirement_failed")
    if intent == "graph_relationships" and not requires_multiple:
        raise ValueError("schema_validation_failed:relationship_requires_multiple")
    if intent == "graph_relationships" and scope not in {"one_hop", "multi_entity_comparison"}:
        raise ValueError("schema_validation_failed:relationship_scope")
    if requires_multiple and entity_count != 2:
        raise ValueError("entity_requirement_failed")
    if requires_graph and not entities.entities:
        raise ValueError("entity_requirement_failed")
    if entity_count > 2 and requires_graph:
        raise ValueError("entity_requirement_failed:too_many_entities")

    return IntentDecision(
        intent=intent,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        depth=depth,
        requires_graph=requires_graph,
        requires_multiple_entities=requires_multiple,
        relationship_mode="compare" if scope == "multi_entity_comparison" else "direct" if intent == "graph_relationships" else "none",
        is_followup=bool(payload["is_followup"]),
        classification_confidence=confidence,
        reason=str(payload.get("reason") or "")[:220],
        decision_source="glm",
        router_called=True,
        content_present=True,
        route_normalized=route_normalized,
        route_normalization_reason=route_normalization_reason,
    )


class GLMIntentRouter:
    """Call the answer provider once to classify routing, never to select entities."""

    def __init__(self, settings: Settings, llm_client: LLMClient) -> None:
        self.settings = settings
        self.llm_client = llm_client
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        prompt_path = Path(self.settings.intent_router_system_prompt_path)
        if not prompt_path.is_absolute():
            prompt_path = Path.cwd() / prompt_path
        try:
            prompt = prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning(
                "event=intent_router_prompt_missing path=%s fallback=true chars=%s",
                self.settings.intent_router_system_prompt_path,
                len(ROUTER_SYSTEM_PROMPT_FALLBACK),
            )
            return ROUTER_SYSTEM_PROMPT_FALLBACK
        if not prompt:
            logger.warning(
                "event=intent_router_prompt_empty path=%s fallback=true chars=%s",
                self.settings.intent_router_system_prompt_path,
                len(ROUTER_SYSTEM_PROMPT_FALLBACK),
            )
            return ROUTER_SYSTEM_PROMPT_FALLBACK
        logger.info(
            "event=intent_router_prompt_loaded path=%s chars=%s",
            self.settings.intent_router_system_prompt_path,
            len(prompt),
        )
        return prompt

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
                {"role": "system", "content": self.system_prompt},
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
