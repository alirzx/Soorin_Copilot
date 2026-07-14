"""LLM-primary semantic route classification with deterministic safety validation."""

from __future__ import annotations

import json
import logging
import ipaddress
import re
import time
from pathlib import Path
from typing import Any

from src.config.settings import Settings
from src.core.context.models import (
    DetectionDetail,
    EntityBinding,
    EntityResolution,
    GraphDirection,
    GraphScope,
    IntentDecision,
    IntentName,
    ResolvedEntity,
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
ALLOWED_DETECTION_DETAILS: set[str] = {"summary", "compact_full"}
ALLOWED_ENTITY_BINDINGS: set[str] = {"explicit", "ui", "active_single", "active_pair", "none"}
DETECTION_COMPACT_WORDS = re.compile(
    r"\b(?:classified|classification|detect(?:ion|ed)?|evidence|rules?|matched\s+rules?|conflicts?|full\s+details?|all\s+available\s+detection|why\s+was)\b",
    re.IGNORECASE,
)


def _valid_ipv4(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if ip.version != 4:
        return None
    return str(ip)


def _empty_resolution(entities: EntityResolution, *, binding: str = "none") -> EntityResolution:
    return EntityResolution(
        status="none",
        entities=[],
        primary_entity=None,
        entity_mode="none",
        candidate_count=0,
        explicit_candidate_count=entities.explicit_candidate_count,
        valid_entity_count=0,
        reference_detected=entities.reference_detected,
        reference_type=entities.reference_type,
        reference_suppressed=entities.reference_suppressed,
        suppression_reason=entities.suppression_reason or binding,
    )


def _resolution_from_entities(
    entities: EntityResolution,
    resolved: list[ResolvedEntity],
) -> EntityResolution:
    mode = "single" if len(resolved) == 1 else "multiple" if len(resolved) > 1 else "none"
    return EntityResolution(
        status="resolved" if resolved else "none",
        entities=resolved,
        primary_entity=resolved[0] if len(resolved) == 1 else None,
        entity_mode=mode,  # type: ignore[arg-type]
        candidate_count=len(resolved),
        explicit_candidate_count=entities.explicit_candidate_count,
        valid_entity_count=len(resolved),
        reference_detected=entities.reference_detected,
        reference_type=entities.reference_type,
        reference_suppressed=entities.reference_suppressed,
        suppression_reason=entities.suppression_reason,
    )


def _infer_entity_binding(
    entities: EntityResolution,
    routing_state: SessionRoutingState | None,
    *,
    ui_context: dict[str, Any] | None = None,
    requires_multiple_entities: bool = False,
    allow_context: bool = True,
) -> EntityBinding:
    if not allow_context:
        return "none"
    if any(entity.source == "message" for entity in entities.entities):
        return "explicit"
    if _valid_ipv4(str((ui_context or {}).get("selected_ip") or "")) or any(entity.source == "ui" for entity in entities.entities):
        return "ui"
    if any(entity.source == "ui" for entity in entities.entities):
        return "ui"
    if any(entity.source == "conversation" for entity in entities.entities):
        return "active_pair" if len(entities.entities) == 2 else "active_single"
    if routing_state and requires_multiple_entities and len(routing_state.active_entities) >= 2:
        return "active_pair"
    if routing_state and not requires_multiple_entities and routing_state.active_ip:
        return "active_single"
    if routing_state and len(routing_state.active_entities) >= 2:
        return "active_pair"
    return "none"


def materialize_entity_binding(
    entity_binding: EntityBinding,
    entities: EntityResolution,
    routing_state: SessionRoutingState | None = None,
    *,
    ui_context: dict[str, Any] | None = None,
) -> tuple[EntityResolution, str]:
    """Materialize one router-selected binding from already-known candidates."""
    if entity_binding == "none":
        return _empty_resolution(entities), "none"

    if entity_binding == "explicit":
        explicit = [entity for entity in entities.entities if entity.source == "message"]
        if not explicit:
            raise ValueError("entity_requirement_failed")
        return _resolution_from_entities(entities, explicit), "message"

    if entity_binding == "ui":
        ui_ip = _valid_ipv4(str((ui_context or {}).get("selected_ip") or ""))
        if ui_ip:
            resolved = [ResolvedEntity(type="ip", value=ui_ip, source="ui")]
            return _resolution_from_entities(entities, resolved), "ui"
        ui_entities = [entity for entity in entities.entities if entity.source == "ui"]
        if not ui_entities:
            raise ValueError("entity_requirement_failed")
        return _resolution_from_entities(entities, ui_entities[:1]), "ui"

    if entity_binding == "active_single":
        active_ip = _valid_ipv4(routing_state.active_ip if routing_state else None)
        if not active_ip:
            raise ValueError("entity_requirement_failed")
        resolved = [ResolvedEntity(type="ip", value=active_ip, source="conversation")]
        return _resolution_from_entities(entities, resolved), "conversation"

    if entity_binding == "active_pair":
        active_entities = [
            ip
            for raw in (routing_state.active_entities if routing_state else ())
            if (ip := _valid_ipv4(raw))
        ]
        if len(active_entities) < 2:
            raise ValueError("entity_requirement_failed")
        resolved = [
            ResolvedEntity(type="ip", value=ip, source="conversation")
            for ip in active_entities[:2]
        ]
        return _resolution_from_entities(entities, resolved), "conversation"

    raise ValueError("unsupported_enum:entity_binding")


def resolution_from_materialized_decision(
    decision: IntentDecision,
    original_entities: EntityResolution,
) -> EntityResolution:
    source = "conversation"
    if decision.binding_source == "message":
        source = "message"
    elif decision.binding_source == "ui":
        source = "ui"
    resolved = [
        ResolvedEntity(type="ip", value=value, source=source)  # type: ignore[arg-type]
        for value in decision.materialized_entities
    ]
    return _resolution_from_entities(original_entities, resolved) if resolved else _empty_resolution(original_entities)
PURE_GRAPH_WORDS = re.compile(
    r"\b(?:connections?|neighbors?|peers?|inbound|outbound|topolog(?:y|ies)|graph|path|route|reachability)\b",
    re.IGNORECASE,
)
IDENTITY_WORDS = re.compile(
    r"\b(?:what\s+is|tell\s+me\s+about|summary|asset|device|role|identity|classified|classification|detected\s+role|behaviou?r)\b",
    re.IGNORECASE,
)

ROUTER_SYSTEM_PROMPT_FALLBACK = (
    "You classify Soorin Copilot routing only. Return exactly one JSON object. "
    "Do not answer the user. Use only supplied deterministic entities. "
    "Choose entity_binding from explicit, ui, active_single, active_pair, none. "
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
    explicit_entities = [entity for entity in entities.entities if entity.source == "message"]
    return {
        "message": compact_preview(message, limit=360),
        "entity_status": entities.status,
        "entity_mode": entities.entity_mode,
        "entity_types": sorted({entity.type for entity in entities.entities}),
        "entity_count": len(entities.entities),
        "explicit_entity_count": len(explicit_entities),
        "resolved_entity_count": len(entities.entities),
        "resolved_entity_source": primary.source if primary else "",
        "resolved_entities": [
            {"type": entity.type, "value": entity.value, "source": entity.source}
            for entity in entities.entities[:2]
        ],
        "active_entity_present": bool(routing_state.active_ip),
        "active_pair_present": len(routing_state.active_entities) >= 2,
        "active_entities_present": bool(routing_state.active_entities),
        "active_entity_count": len(routing_state.active_entities),
        "ui_entity_present": bool((ui_context or {}).get("selected_ip")),
        "ui_selected_entity_present": bool((ui_context or {}).get("selected_ip")),
        "previous_provider": routing_state.last_provider,
        "previous_intent": routing_state.previous_intent,
        "previous_scope": routing_state.previous_scope,
        "previous_direction": routing_state.previous_direction,
        "previous_depth": routing_state.previous_depth,
        "previous_requires_detection": routing_state.previous_requires_detection,
        "previous_detection_detail": routing_state.previous_detection_detail or "",
        "explicit_topic_detachment": entities.reference_suppressed,
    }


def validate_router_payload(
    payload: dict[str, Any],
    entities: EntityResolution,
    *,
    min_confidence: float,
    message: str = "",
    routing_state: SessionRoutingState | None = None,
    ui_context: dict[str, Any] | None = None,
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

    def normalize(reason: str, *, prefer: bool = False) -> None:
        nonlocal route_normalized, route_normalization_reason
        route_normalized = True
        if prefer or not route_normalization_reason:
            route_normalization_reason = reason

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
    raw_detection_detail = str(payload.get("detection_detail") or "summary")
    detection_detail: DetectionDetail = "summary"
    if raw_detection_detail in ALLOWED_DETECTION_DETAILS:
        detection_detail = raw_detection_detail  # type: ignore[assignment]
    else:
        normalize("invalid_detection_detail_defaulted")
    requires_detection = bool(payload.get("requires_detection", False))
    message_text = message or ""
    compact_detection_requested = bool(DETECTION_COMPACT_WORDS.search(message_text))
    graph_word_present = bool(PURE_GRAPH_WORDS.search(message_text))
    identity_word_present = bool(IDENTITY_WORDS.search(message_text))

    requested_entity_binding = str(payload.get("entity_binding") or "")
    binding_normalized = False
    binding_normalization_reason = None
    explicit_entity_count = len([entity for entity in entities.entities if entity.source == "message"])
    ui_ip = _valid_ipv4(str((ui_context or {}).get("selected_ip") or ""))
    contextual_binding_allowed = intent not in {"general_knowledge", "unclear"} and not entities.reference_suppressed
    if requested_entity_binding:
        if requested_entity_binding not in ALLOWED_ENTITY_BINDINGS:
            raise ValueError("unsupported_enum:entity_binding")
        entity_binding: EntityBinding = requested_entity_binding  # type: ignore[assignment]
    else:
        entity_binding = _infer_entity_binding(
            entities,
            routing_state,
            ui_context=ui_context,
            requires_multiple_entities=requires_multiple,
            allow_context=contextual_binding_allowed,
        )
        requested_entity_binding = "missing"
        binding_normalized = True
        binding_normalization_reason = "missing_entity_binding_inferred"
    if explicit_entity_count and entity_binding != "explicit":
        entity_binding = "explicit"
        binding_normalized = True
        binding_normalization_reason = "explicit_entity_takes_authority"
    elif entities.reference_suppressed and entity_binding in {"ui", "active_single", "active_pair"}:
        entity_binding = "none"
        binding_normalized = True
        binding_normalization_reason = "topic_detachment_forbids_active_binding"
    elif ui_ip and not explicit_entity_count and entity_binding != "ui":
        entity_binding = "ui"
        binding_normalized = True
        binding_normalization_reason = "ui_entity_takes_authority"
    elif intent in {"general_knowledge", "unclear"} and entity_binding != "none":
        entity_binding = "none"
        binding_normalized = True
        binding_normalization_reason = "general_requires_no_entity_binding"
    if ui_ip and not explicit_entity_count and not entities.reference_suppressed and intent in {"general_knowledge", "unclear"}:
        intent = "asset_investigation"
        scope = "node_summary"
        direction = "both"
        depth = 0
        requires_graph = True
        requires_detection = True
        detection_detail = "summary"
        requires_multiple = False
        entity_binding = "ui"
        binding_normalized = True
        binding_normalization_reason = "ui_entity_takes_authority"
        normalize("ui_subject_requires_asset_route", prefer=True)

    materialized_entities, binding_source = materialize_entity_binding(
        entity_binding,
        entities,
        routing_state,
        ui_context=ui_context,
    )
    entity_count = len(materialized_entities.entities)
    if intent == "asset_investigation" and entity_count == 1:
        requires_detection = True
        if compact_detection_requested and not (graph_word_present and identity_word_present):
            if scope != "none" or direction != "none" or depth != 0 or requires_graph or detection_detail != "compact_full":
                normalize("detection_compact_full_requested", prefer=True)
            scope = "none"
            direction = "none"
            depth = 0
            requires_graph = False
            detection_detail = "compact_full"
            requires_multiple = False
        else:
            if (
                scope != "node_summary"
                or direction != "both"
                or depth != 0
                or not requires_graph
                or requires_multiple
                or not requires_detection
            ):
                normalize("node_summary_requires_graph", prefer=True)
            scope = "node_summary"
            direction = "both"
            depth = 0
            requires_graph = True
            detection_detail = "summary"
            requires_multiple = False
    if intent == "graph_neighbors" and (requires_multiple or entity_count == 2):
        intent = "graph_relationships"
        scope = "multi_entity_comparison"
        direction = "both"
        depth = 1
        requires_graph = True
        requires_multiple = True
        normalize("multi_entity_neighbors_to_relationship")
    if intent in {"graph_neighbors", "graph_path", "graph_relationships"} and entity_count == 1:
        if compact_detection_requested:
            requires_detection = True
            detection_detail = "compact_full"
            if not identity_word_present:
                scope = "none"
                direction = "none"
                depth = 0
                requires_graph = False
                intent = "asset_investigation"
                normalize("detection_compact_full_requested", prefer=True)
        elif graph_word_present and not identity_word_present:
            if requires_detection:
                normalize("pure_graph_skips_detection")
            requires_detection = False
            detection_detail = "summary"
    if entity_count != 1 or materialized_entities.entity_mode == "multiple":
        if requires_detection:
            normalize("detection_requires_single_entity")
        requires_detection = False
        detection_detail = "summary"
    if intent in {"general_knowledge", "unclear"}:
        if requires_detection:
            normalize("general_skips_detection")
        requires_detection = False
        detection_detail = "summary"
    if not requires_detection:
        detection_detail = "summary"
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
    if requires_graph and not materialized_entities.entities:
        raise ValueError("entity_requirement_failed")
    if entity_count > 2 and requires_graph:
        raise ValueError("entity_requirement_failed:too_many_entities")

    return IntentDecision(
        intent=intent,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        depth=depth,
        requires_graph=requires_graph,
        requires_detection=requires_detection,
        detection_detail=detection_detail,
        entity_binding=entity_binding,
        requested_entity_binding=requested_entity_binding,
        binding_source=binding_source,
        binding_available=True,
        binding_normalized=binding_normalized,
        binding_normalization_reason=binding_normalization_reason,
        materialized_entity_count=entity_count,
        materialized_entities=tuple(entity.value for entity in materialized_entities.entities),
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
        return self._classify_with_context(
            routing_context,
            entities,
            routing_state=routing_state,
            ui_context=ui_context,
            request_id=request_id,
        )

    def _classify_with_context(
        self,
        routing_context: dict[str, Any],
        entities: EntityResolution,
        *,
        routing_state: SessionRoutingState | None = None,
        ui_context: dict[str, Any] | None = None,
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
                    transient_retries=0,
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
                    message=str(routing_context.get("message") or ""),
                    routing_state=routing_state,
                    ui_context=ui_context,
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
                    "event=intent_router_complete request_id=%s decision_source=glm intent=%s scope=%s direction=%s depth=%s requires_graph=%s requires_detection=%s detection_detail=%s entity_binding=%s binding_source=%s binding_available=%s binding_normalized=%s binding_normalization_reason=%s materialized_entity_count=%s route_normalized=%s route_normalization_reason=%s confidence=%s retry_count=%s latency_ms=%s finish_reason=%s content_present=%s",
                    request_id,
                    decision.intent,
                    decision.scope,
                    decision.direction,
                    decision.depth,
                    decision.requires_graph,
                    decision.requires_detection,
                    decision.detection_detail,
                    decision.entity_binding,
                    decision.binding_source,
                    decision.binding_available,
                    decision.binding_normalized,
                    decision.binding_normalization_reason or "",
                    decision.materialized_entity_count,
                    decision.route_normalized,
                    decision.route_normalization_reason or "",
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
