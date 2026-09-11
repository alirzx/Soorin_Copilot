"""Structured Asset-set routing extension for Phase 4B.1.

This module intentionally wraps the established semantic router instead of
replacing its entity/topology validation. Set queries are a separate semantic
contract: selectors describe an Asset set and never become active entities.
Execution is introduced by Phase 4B.2.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

import src.core.context.intent as _intent_module
from src.core.context.intent import (
    SemanticIntentRouter as _BaseSemanticIntentRouter,
    _extract_first_json_object,
    validate_router_payload,
)
from src.core.context.models import (
    EntityResolution,
    IntentDecision,
    ResolvedEntity,
    RouteDecision,
    StructuredResultReferenceDecision,
)
from src.core.context.router import normalize_intent_route as _base_normalize_intent_route
from src.core.graph.structured import StructuredQueryMode, StructuredQuerySpec
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)
_SET_INTENTS = {"asset_search", "asset_aggregate"}
_STRUCTURED_REPAIR_SYSTEM_PROMPT = (
    "Repair one Soorin routing object. Return JSON only. Required keys: intent, scope, direction, depth, "
    "requires_graph, requires_detection, requires_asset_profile, requires_knowledge, structured_query, "
    "structured_result_reference, "
    "entity_binding, requires_multiple_entities, is_followup, classification_confidence, reason. "
    "Allowed intents: general_knowledge, asset_investigation, asset_search, asset_aggregate, graph_neighbors, "
    "graph_relationships, graph_path, graph_followup, unclear. For asset_search or asset_aggregate, preserve only "
    "allow-listed structured_query fields, use scope/direction none, depth 0, requires_graph true, entity_binding none, "
    "and do not invent entities or Cypher. For other intents structured_query must be null. "
    "structured_result_reference is null or an object with kind none, set_query, select_entities, or "
    "historical_recall and 0-2 one-based ordinals."
)


def _structured_result_reference(payload: Any) -> StructuredResultReferenceDecision:
    if payload is None:
        return StructuredResultReferenceDecision()
    if not isinstance(payload, dict) or set(payload) != {"kind", "ordinals"}:
        raise ValueError("structured_result_reference_schema_invalid")
    kind = str(payload.get("kind") or "")
    if kind not in {"none", "set_query", "select_entities", "historical_recall"}:
        raise ValueError("structured_result_reference_kind_invalid")
    raw_ordinals = payload.get("ordinals")
    if not isinstance(raw_ordinals, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in raw_ordinals
    ):
        raise ValueError("structured_result_reference_ordinals_invalid")
    ordinals = tuple(raw_ordinals)
    if kind == "select_entities":
        if not 1 <= len(ordinals) <= 2 or len(set(ordinals)) != len(ordinals):
            raise ValueError("structured_result_selection_cardinality_invalid")
        if any(item < 1 for item in ordinals):
            raise ValueError("structured_result_selection_ordinal_invalid")
    elif ordinals:
        raise ValueError("structured_result_reference_unexpected_ordinals")
    return StructuredResultReferenceDecision(kind=kind, ordinals=ordinals)  # type: ignore[arg-type]


def validate_structured_router_payload(
    payload: dict[str, Any],
    entities: EntityResolution,
    *,
    min_confidence: float,
    message: str = "",
    routing_state: SessionRoutingState | None = None,
    ui_context: dict[str, Any] | None = None,
) -> IntentDecision:
    """Validate either the established entity route or one typed Asset-set route."""
    reference = _structured_result_reference(payload.get("structured_result_reference"))
    # This extension owns structured_result_reference. Remove it after parsing
    # before delegating to the legacy or set-specific field allow-list. The
    # Router prompt intentionally emits null for ordinary routes, so null must
    # be a valid no-op rather than an unexpected-field failure.
    clean_payload = dict(payload)
    clean_payload.pop("structured_result_reference", None)
    if reference.kind != "none":
        explicit = any(entity.source == "message" for entity in entities.entities)
        if explicit:
            if reference.kind == "set_query":
                raise ValueError("structured_result_reference_cannot_override_explicit_entity")
            decision = validate_structured_router_payload(
                clean_payload,
                entities,
                min_confidence=min_confidence,
                message=message,
                routing_state=routing_state,
                ui_context=ui_context,
            )
            return replace(decision, structured_result_reference=StructuredResultReferenceDecision())

        context = routing_state.structured_query_context if routing_state is not None else None
        if context is None:
            raise ValueError("structured_result_reference_context_unavailable")
        if reference.kind == "set_query":
            decision = validate_structured_router_payload(
                clean_payload,
                entities,
                min_confidence=min_confidence,
                message=message,
                routing_state=routing_state,
                ui_context=None,
            )
            if decision.structured_query is None:
                raise ValueError("structured_result_reference_query_required")
            return replace(decision, structured_result_reference=reference)
        if reference.kind == "historical_recall":
            if (
                clean_payload.get("intent") != "general_knowledge"
                or clean_payload.get("structured_query") is not None
                or any(bool(clean_payload.get(name)) for name in (
                    "requires_graph", "requires_detection", "requires_asset_profile", "requires_knowledge"
                ))
            ):
                raise ValueError("structured_result_historical_recall_route_invalid")
            empty = EntityResolution(status="none")
            decision = validate_structured_router_payload(
                clean_payload,
                empty,
                min_confidence=min_confidence,
                message=message,
                routing_state=replace(
                    routing_state,
                    active_ip=None,
                    active_entities=(),
                    last_resolved_entities=(),
                ),
                ui_context=None,
            )
            return replace(decision, structured_result_reference=reference)

        if context.mode != "search":
            raise ValueError("structured_result_selection_requires_search_context")
        if any(ordinal > len(context.result_refs) for ordinal in reference.ordinals):
            raise ValueError("structured_result_selection_out_of_bounds")
        selected = tuple(context.result_refs[ordinal - 1].ip for ordinal in reference.ordinals)
        if not selected:
            raise ValueError("structured_result_selection_empty")
        resolved = [
            ResolvedEntity(type="ip", value=value, source="conversation")
            for value in selected
        ]
        selection_entities = EntityResolution(
            status="resolved",
            entities=resolved,
            primary_entity=resolved[0] if len(resolved) == 1 else None,
            entity_mode="single" if len(resolved) == 1 else "multiple",
            candidate_count=len(resolved),
            explicit_candidate_count=0,
            valid_entity_count=len(resolved),
            reference_detected=True,
            reference_type="structured_result_selection",
        )
        clean_payload["structured_query"] = None
        clean_payload["entity_binding"] = (
            "active_single" if len(selected) == 1 else "active_pair"
        )
        clean_payload["requires_multiple_entities"] = len(selected) == 2
        selection_state = replace(
            routing_state,
            active_ip=selected[0] if len(selected) == 1 else None,
            active_entities=selected,
            last_resolved_entities=selected,
        )
        decision = validate_structured_router_payload(
            clean_payload,
            selection_entities,
            min_confidence=min_confidence,
            message=message,
            routing_state=selection_state,
            ui_context=None,
        )
        return replace(decision, structured_result_reference=reference)

    payload = clean_payload
    intent = str(payload.get("intent") or "")
    raw_query = payload.get("structured_query")

    if intent not in _SET_INTENTS:
        if raw_query is not None:
            raise ValueError("schema_validation_failed:structured_query_requires_set_intent")
        legacy_payload = dict(payload)
        legacy_payload.pop("structured_query", None)
        return validate_router_payload(
            legacy_payload,
            entities,
            min_confidence=min_confidence,
            message=message,
            routing_state=routing_state,
            ui_context=ui_context,
        )

    if raw_query is None:
        raise ValueError("schema_validation_failed:structured_query_required")
    try:
        query = StructuredQuerySpec.model_validate(raw_query)
    except ValidationError as exc:
        raise ValueError("schema_validation_failed:structured_query") from exc

    expected_mode = (
        StructuredQueryMode.SEARCH
        if intent == "asset_search"
        else StructuredQueryMode.AGGREGATE
    )
    if query.mode is not expected_mode:
        raise ValueError("schema_validation_failed:structured_query_mode")

    allowed = {
        "intent",
        "scope",
        "direction",
        "depth",
        "requires_graph",
        "requires_detection",
        "requires_asset_profile",
        "requires_knowledge",
        "structured_query",
        "entity_binding",
        "requires_multiple_entities",
        "is_followup",
        "classification_confidence",
        "reason",
    }
    if unexpected := sorted(set(payload).difference(allowed)):
        raise ValueError(f"schema_validation_failed:unexpected={','.join(unexpected)}")
    required = allowed.difference({"requires_knowledge"})
    if missing := sorted(required.difference(payload)):
        raise ValueError(f"schema_validation_failed:missing={','.join(missing)}")

    try:
        confidence = float(payload["classification_confidence"])
    except (TypeError, ValueError) as exc:
        raise ValueError("schema_validation_failed:classification_confidence") from exc
    if not 0 <= confidence <= 1:
        raise ValueError("schema_validation_failed:classification_confidence_range")
    if confidence < min_confidence:
        raise ValueError("low_confidence")

    if payload.get("scope") != "none" or payload.get("direction") != "none":
        raise ValueError("schema_validation_failed:structured_query_scope")
    if payload.get("depth") != 0:
        raise ValueError("schema_validation_failed:structured_query_depth")
    if payload.get("entity_binding") != "none":
        raise ValueError("schema_validation_failed:structured_query_entity_binding")
    for name in (
        "requires_graph",
        "requires_detection",
        "requires_asset_profile",
        "requires_multiple_entities",
        "is_followup",
    ):
        if not isinstance(payload.get(name), bool):
            raise ValueError(f"schema_validation_failed:{name}")
    if "requires_knowledge" in payload and not isinstance(payload["requires_knowledge"], bool):
        raise ValueError("schema_validation_failed:requires_knowledge")
    if not payload["requires_graph"]:
        raise ValueError("schema_validation_failed:structured_query_requires_graph")
    if payload["requires_detection"] or payload["requires_asset_profile"]:
        raise ValueError("schema_validation_failed:structured_query_cannot_deepen_without_focal_entity")
    if payload["requires_multiple_entities"]:
        raise ValueError("schema_validation_failed:structured_query_is_not_entity_pair")

    return IntentDecision(
        intent=intent,  # type: ignore[arg-type]
        scope="none",
        direction="none",
        depth=0,
        requires_graph=True,
        requires_detection=False,
        requires_asset_profile=False,
        requires_knowledge=bool(payload.get("requires_knowledge", False)),
        structured_query=query,
        entity_binding="none",
        requested_entity_binding="none",
        binding_source="none",
        binding_available=True,
        materialized_entity_count=0,
        materialized_entities=(),
        requires_multiple_entities=False,
        relationship_mode="none",
        is_followup=bool(payload["is_followup"]),
        classification_confidence=confidence,
        reason=str(payload.get("reason") or "")[:220],
        decision_source="semantic_router",
        router_called=True,
        content_present=True,
    )


class SemanticIntentRouter(_BaseSemanticIntentRouter):
    """Established router plus deterministic validation of Asset-set output."""

    def __init__(self, settings: Any, llm_client: Any) -> None:
        super().__init__(settings, llm_client)
        # The base router owns the retry loop and resolves its repair prompt from
        # its module. Keep that existing path but make its contract 4B.1-aware.
        _intent_module.ROUTER_REPAIR_SYSTEM_PROMPT = _STRUCTURED_REPAIR_SYSTEM_PROMPT

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
        if not content:
            raise ValueError("missing_content")
        if finish_reason == "length":
            raise ValueError("finish_reason_length")
        try:
            payload = json.loads(_extract_first_json_object(content))
        except (ValueError, json.JSONDecodeError):
            logger.warning(
                "event=intent_router_json_parse_failed request_id=%s reason=structured_extension_parse",
                request_id,
            )
            raise
        if not isinstance(payload, dict):
            raise ValueError("malformed_json:not_object")
        return validate_structured_router_payload(
            payload,
            entities,
            min_confidence=self.settings.intent_router_min_confidence,
            message=str(routing_context.get("message") or ""),
            routing_state=routing_state,
            ui_context=ui_context,
        )


def normalize_intent_route(
    decision: IntentDecision,
    entities: EntityResolution,
) -> RouteDecision:
    """Preserve existing routes and carry Asset-set semantics without entity pollution."""
    route = _base_normalize_intent_route(decision, entities)
    if decision.structured_query is None:
        return route
    return replace(
        route,
        use_graph=True,
        use_detection=False,
        use_asset_profile=False,
        structured_query=decision.structured_query,
        entity_binding="none",
        requested_entity_binding="none",
        resolved_entity_binding="none",
        binding_source="none",
        binding_available=True,
        materialized_entity_count=0,
        materialized_entities=(),
        target_entity=None,
        target_entities=[],
        matched_signals=[decision.intent, "structured_asset_set"],
        graph_intent_detected=True,
        asset_investigation_detected=False,
        intent=decision.intent,
        scope="none",
        direction="none",
        depth=0,
        requires_multiple_entities=False,
        relationship_mode="none",
    )
