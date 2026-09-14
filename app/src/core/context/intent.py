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
from src.core.context.router import is_exhaustive_connection_request
from src.core.context.entities import IPV4_CANDIDATE_RE, _extract_subnets


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
ALLOWED_ENTITY_BINDINGS: set[str] = {"explicit", "ui", "active_single", "active_pair", "none"}
DETECTION_EVIDENCE_WORDS = re.compile(
    r"\b(?:classified|classification|detect(?:ion|ed)?|evidence|rules?|matched\s+rules?|conflicts?|full\s+details?|all\s+available\s+detection|why\s+was)\b",
    re.IGNORECASE,
)
ASSET_PROFILE_WORDS = re.compile(
    r"\b(?:asset\s+profile|profile|inventory|owner|assigned\s+user|hostname|operating\s+system|os|asset\s+type|"
    r"device\s+type|risk(?:\s+(?:score|level|trend))?|alerts?|services?|authentication|kerberos|ldap|ntlm|smb|"
    r"domain\s+join(?:ed)?|mac(?:\s+(?:address|vendor))?|open\s+ports?|kdc|identity)\b",
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
        subnet_constraints=entities.subnet_constraints,
        unsupported_constraints=entities.unsupported_constraints,
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
        subnet_constraints=entities.subnet_constraints,
        unsupported_constraints=entities.unsupported_constraints,
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
    requires_multiple_entities: bool = False,
) -> tuple[EntityResolution, str]:
    """Materialize one router-selected binding from already-known candidates."""
    if entity_binding == "none":
        return _empty_resolution(entities), "none"

    if entity_binding == "explicit":
        explicit = [entity for entity in entities.entities if entity.source == "message"]
        if not explicit:
            raise ValueError("entity_requirement_failed")
        resolved = list(explicit)
        if requires_multiple_entities and len(resolved) == 1:
            known_values = {entity.value for entity in resolved}
            conversation = [
                entity
                for entity in entities.entities
                if entity.source == "conversation" and entity.value not in known_values
            ]
            active_ip = _valid_ipv4(routing_state.active_ip if routing_state else None)
            if conversation:
                resolved.append(conversation[0])
            elif active_ip and active_ip not in known_values:
                resolved.append(ResolvedEntity(type="ip", value=active_ip, source="conversation"))
        return _resolution_from_entities(entities, resolved[:2]), "message"

    if entity_binding == "ui":
        ui_ip = _valid_ipv4(str((ui_context or {}).get("selected_ip") or ""))
        if ui_ip:
            resolved = [ResolvedEntity(type="ip", value=ui_ip, source="ui")]
            if requires_multiple_entities:
                peer = next(
                    (
                        entity
                        for entity in entities.entities
                        if entity.value != ui_ip
                    ),
                    None,
                )
                if peer is not None:
                    resolved.append(peer)
            return _resolution_from_entities(entities, resolved[:2]), "ui"
        ui_entities = [entity for entity in entities.entities if entity.source == "ui"]
        if not ui_entities:
            raise ValueError("entity_requirement_failed")
        resolved = ui_entities[:1]
        if requires_multiple_entities:
            peer = next(
                (
                    entity
                    for entity in entities.entities
                    if entity.value != resolved[0].value
                ),
                None,
            )
            if peer is not None:
                resolved.append(peer)
        return _resolution_from_entities(entities, resolved[:2]), "ui"

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
    original_sources = {entity.value: entity.source for entity in original_entities.entities}
    resolved = [
        ResolvedEntity(
            type="ip",
            value=value,
            source=original_sources.get(value, source),  # type: ignore[arg-type]
        )
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
COMBINED_ANALYSIS_WORDS = re.compile(
    r"\b(?:analy[sz]e\s+(?:this\s+)?asset\s+deeply|all\s+(?:available\s+)?evidence|"
    r"complete\s+(?:the\s+)?analysis|comprehensive\s+(?:analytical\s+)?report|"
    r"full\s+asset\s+assessment|identity\s+and\s+connections|classification\s+and\s+topology)\b",
    re.IGNORECASE,
)
SECURITY_ANALYSIS_WORDS = re.compile(
    r"\b(?:anomal(?:y|ies|ous)|abnormal|unusual\s+behavio[u]?r|suspicious\s+behavio[u]?r|"
    r"security\s+concern|unexpected\s+communication|possible\s+compromise)\b",
    re.IGNORECASE,
)

ROUTER_SYSTEM_PROMPT_FALLBACK = (
    "Classify Soorin Copilot routing only; return one JSON object and never answer. "
    "Use only supplied entities. entity_binding is explicit, ui, active_single, active_pair, or none. "
    "Scopes are none, node_summary, one_hop, full_neighbors, two_hop, path, or multi_entity_comparison. "
    "Select graph, detection, asset_profile, and knowledge independently. Reference bounded "
    "latest_structured_context only through structured_result_reference; never guess a result set. "
    "Depth is at most 2."
)
ROUTER_REPAIR_SYSTEM_PROMPT = (
    "Repair one Soorin routing object. Return JSON only. Required keys: intent, scope, direction, depth, "
    "requires_graph, requires_detection, requires_asset_profile, requires_knowledge, entity_binding, requires_multiple_entities, "
    "is_followup, classification_confidence, reason. Allowed intents: general_knowledge, asset_investigation, "
    "graph_neighbors, graph_relationships, graph_path, graph_followup, unclear. Allowed scopes: none, "
    "node_summary, one_hop, full_neighbors, two_hop, path, multi_entity_comparison. Allowed directions: none, "
    "inbound, outbound, both. Allowed entity_binding values: explicit, ui, active_single, active_pair, none. Never invent entities."
)


def _extract_first_json_object(text: str) -> str:
    """Return the first balanced JSON object, respecting strings and escapes."""
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if start < 0:
            if char == "{":
                start = index
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    if start < 0:
        raise ValueError("malformed_json:no_object")
    raise ValueError("malformed_json:unbalanced_object")


def _json_from_text(text: str) -> dict[str, Any]:
    payload = json.loads(_extract_first_json_object(text))
    if not isinstance(payload, dict):
        raise ValueError("malformed_json")
    return payload


def build_routing_context(
    message: str,
    entities: EntityResolution,
    routing_state: SessionRoutingState,
    *,
    ui_context: dict[str, Any] | None = None,
    recent_messages: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    primary = entities.primary_entity
    explicit_entities = [entity for entity in entities.entities if entity.source == "message"]
    recent_turns = [
        {
            "role": item.get("role", "")[:20],
            "content": compact_preview(item.get("content", ""), limit=220),
        }
        for item in (recent_messages or [])[-4:]
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    recent_entity_candidates: list[str] = []
    for item in reversed(recent_messages or []):
        _subnets, host_text = _extract_subnets(item.get("content", ""))
        for candidate in IPV4_CANDIDATE_RE.findall(host_text):
            ip = _valid_ipv4(candidate)
            if ip and ip not in recent_entity_candidates:
                recent_entity_candidates.append(ip)
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
        "active_entities": list(routing_state.active_entities),
        "recent_turns": recent_turns,
        "recent_entity_candidates": recent_entity_candidates[:2],
        "recent_turn_count": len(recent_turns),
        "ui_entity_present": bool((ui_context or {}).get("selected_ip")),
        "ui_selected_entity_present": bool((ui_context or {}).get("selected_ip")),
        "previous_provider": routing_state.last_provider,
        "previous_providers": list(routing_state.last_providers),
        "previous_intent": routing_state.previous_intent,
        "previous_scope": routing_state.previous_scope,
        "previous_direction": routing_state.previous_direction,
        "previous_depth": routing_state.previous_depth,
        "previous_requires_detection": routing_state.previous_requires_detection,
        "previous_requires_asset_profile": routing_state.previous_requires_asset_profile,
        "explicit_topic_detachment": entities.reference_suppressed,
        "subnet_constraints": list(entities.subnet_constraints),
        "unsupported_constraints": list(entities.unsupported_constraints),
        "latest_structured_context": (
            routing_state.structured_query_context.routing_summary()
            if routing_state.structured_query_context is not None
            else {"available": False}
        ),
        "structured_lineage": [
            {
                "position": (
                    "base" if index == 0 else
                    "latest" if index == len(routing_state.structured_query_lineage) - 1 else
                    "parent"
                ),
                **item.routing_summary(),
            }
            for index, item in enumerate(routing_state.structured_query_lineage)
        ],
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
        "requires_detection",
        "requires_asset_profile",
        "requires_multiple_entities",
        "is_followup",
        "classification_confidence",
        "reason",
    }
    if unexpected := sorted(set(payload).difference(required | {"entity_binding", "requires_knowledge"})):
        raise ValueError(f"schema_validation_failed:unexpected={','.join(unexpected)}")
    if missing := sorted(required.difference(payload)):
        raise ValueError(f"schema_validation_failed:missing={','.join(missing)}")

    intent = str(payload["intent"])
    scope = str(payload["scope"])
    direction = str(payload["direction"])
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

    for name in ("requires_graph", "requires_detection", "requires_asset_profile", "requires_multiple_entities", "is_followup"):
        if not isinstance(payload[name], bool):
            raise ValueError(f"schema_validation_failed:{name}")
    requires_graph = payload["requires_graph"]
    requires_detection = payload["requires_detection"]
    requires_asset_profile = payload["requires_asset_profile"]
    if "requires_knowledge" in payload and not isinstance(payload["requires_knowledge"], bool):
        raise ValueError("schema_validation_failed:requires_knowledge")
    requires_knowledge = bool(payload.get("requires_knowledge", False))
    requires_multiple = payload["requires_multiple_entities"]
    deterministic_comparison_pair = bool(
        len(entities.entities) == 2
        and entities.reference_type == "compare_with_reference"
        and not entities.reference_suppressed
    )
    if deterministic_comparison_pair:
        intent, scope, direction, depth = "graph_relationships", "multi_entity_comparison", "both", 1
        requires_graph = True
        requires_multiple = True
        normalize("deterministic_comparison_pair_preserved", prefer=True)
    exhaustive_connections = is_exhaustive_connection_request(message)
    graph_like_scope = scope in {"node_summary", "one_hop", "full_neighbors", "two_hop", "path", "multi_entity_comparison"}
    if (
        len(entities.entities) > 2
        and intent not in {"general_knowledge", "unclear"}
        and (requires_graph or graph_like_scope or requires_multiple)
    ):
        raise ValueError("entity_requirement_failed:too_many_entities")

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
    materialized_entities, binding_source = materialize_entity_binding(
        entity_binding,
        entities,
        routing_state,
        ui_context=ui_context,
        requires_multiple_entities=requires_multiple or scope == "multi_entity_comparison",
    )
    entity_count = len(materialized_entities.entities)
    is_followup = payload["is_followup"]
    if entity_binding in {"active_single", "active_pair"} and entities.reference_detected and not is_followup:
        is_followup = True
        normalize("referential_binding_requires_followup")
    if entity_count > 2:
        raise ValueError("entity_requirement_failed:too_many_entities")
    if (
        intent not in {"general_knowledge", "unclear"}
        and any((requires_graph, requires_detection, requires_asset_profile))
        and entity_count == 0
    ):
        raise ValueError("entity_requirement_failed")
    if entity_count == 2 and any((requires_graph, requires_detection, requires_asset_profile)) and not requires_multiple:
        requires_multiple = True
        normalize("multi_entity_provider_route_requires_pair", prefer=True)
    if entity_count == 1 and requires_multiple:
        requires_multiple = False
        normalize("single_entity_route_clears_pair_requirement")

    if intent in {"general_knowledge", "unclear"}:
        if scope != "none":
            raise ValueError("schema_validation_failed:general_scope")
        if any((requires_graph, requires_detection, requires_asset_profile)):
            normalize("general_skips_product_context")
        requires_graph = requires_detection = requires_asset_profile = False
        if intent == "unclear":
            requires_knowledge = False
        scope, direction, depth, requires_multiple = "none", "none", 0, False
    elif intent == "asset_investigation" and entity_count in {1, 2} and not any((requires_graph, requires_detection, requires_asset_profile)):
        requires_asset_profile = True
        normalize("asset_investigation_requires_evidence")

    if scope in {"node_summary", "one_hop", "full_neighbors", "two_hop", "path", "multi_entity_comparison"} and not requires_graph:
        requires_graph = True
        normalize("graph_scope_requires_graph")
    if requires_graph and scope == "none":
        if entity_count == 2:
            intent, scope, direction, depth, requires_multiple = "graph_relationships", "multi_entity_comparison", "both", 1, True
            normalize("multi_entity_graph_requires_comparison_scope", prefer=True)
        else:
            scope, direction, depth = "node_summary", "both", 0
            normalize("node_summary_requires_graph", prefer=True)
    if not requires_graph and scope != "none":
        scope, direction, depth = "none", "none", 0
        normalize("provider_only_route_clears_graph_scope")
    if exhaustive_connections and entity_count == 1:
        intent, scope, direction, depth = "graph_neighbors", "full_neighbors", "both", 1
        requires_graph, requires_multiple = True, False
        normalize("exhaustive_connections_require_full_neighbors", prefer=True)
    elif exhaustive_connections and entity_count == 2:
        intent, scope, direction, depth = "graph_relationships", "multi_entity_comparison", "both", 1
        requires_graph, requires_multiple = True, True
        normalize("exhaustive_pair_requires_complete_comparison", prefer=True)
    if scope == "node_summary" and (direction != "both" or depth != 0):
        direction, depth = "both", 0
        normalize("node_summary_requires_graph", prefer=True)
    if intent == "graph_neighbors" and (requires_multiple or entity_count == 2):
        intent = "graph_relationships"
        scope = "multi_entity_comparison"
        direction = "both"
        depth = 1
        requires_graph = True
        requires_multiple = True
        normalize("multi_entity_neighbors_to_relationship")
    if scope == "full_neighbors" and depth != 1:
        raise ValueError("schema_validation_failed:full_neighbors_depth")
    if scope == "one_hop" and depth != 1:
        raise ValueError("schema_validation_failed:one_hop_depth")
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
    if intent == "asset_investigation" and entity_count not in {1, 2}:
        raise ValueError("entity_requirement_failed")
    if intent == "graph_relationships" and entity_count != 2:
        raise ValueError("entity_requirement_failed")
    if intent == "graph_relationships" and not requires_multiple:
        raise ValueError("schema_validation_failed:relationship_requires_multiple")
    if intent == "graph_relationships" and scope not in {"one_hop", "multi_entity_comparison"}:
        raise ValueError("schema_validation_failed:relationship_scope")
    if requires_multiple and entity_count != 2:
        raise ValueError("entity_requirement_failed")
    if any((requires_graph, requires_detection, requires_asset_profile)) and not materialized_entities.entities:
        raise ValueError("entity_requirement_failed")
    return IntentDecision(
        intent=intent,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        depth=depth,
        requires_graph=requires_graph,
        requires_detection=requires_detection,
        requires_asset_profile=requires_asset_profile,
        requires_knowledge=requires_knowledge,
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
        is_followup=is_followup,
        classification_confidence=confidence,
        reason=str(payload.get("reason") or "")[:220],
        decision_source="semantic_router",
        exhaustive_connections_requested=exhaustive_connections,
        router_called=True,
        content_present=True,
        route_normalized=route_normalized,
        route_normalization_reason=route_normalization_reason,
    )


class SemanticIntentRouter:
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
        recent_messages: list[dict[str, str]] | None = None,
        trace_id: str = "",
        request_id: str = "",
    ) -> IntentDecision:
        if not self.settings.intent_router_enabled:
            return self.disabled_decision()

        routing_context = build_routing_context(
            message,
            entities,
            routing_state,
            ui_context=ui_context,
            recent_messages=recent_messages,
        )
        return self._classify_with_context(
            routing_context,
            entities,
            routing_state=routing_state,
            ui_context=ui_context,
            trace_id=trace_id,
            request_id=request_id,
        )

    def _classify_with_context(
        self,
        routing_context: dict[str, Any],
        entities: EntityResolution,
        *,
        routing_state: SessionRoutingState | None = None,
        ui_context: dict[str, Any] | None = None,
        trace_id: str = "",
        request_id: str = "",
    ) -> IntentDecision:
        started = time.perf_counter()
        router_deployment = self.settings.deployment_for_purpose("intent_router")
        router_request = router_deployment.request_config("intent_router")
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": json.dumps(routing_context, sort_keys=True)},
        ]
        logger.info(
            "event=intent_router_start request_id=%s router_deployment=%s router_provider=%s router_model=%s router_engine=%s entity_status=%s entity_count=%s previous_scope=%s router_timeout_seconds=%s router_max_tokens=%s",
            request_id,
            router_deployment.name,
            router_deployment.provider_type,
            router_deployment.model,
            router_deployment.model,
            routing_context.get("entity_status"),
            routing_context.get("entity_count"),
            routing_context.get("previous_scope") or "",
            router_request.read_timeout_seconds,
            router_request.max_tokens,
        )
        try:
            result = self.llm_client.chat(
                messages,
                request_id=request_id,
                max_tokens=router_request.max_tokens,
                temperature=router_request.temperature,
                top_p=router_request.top_p,
                timeout_seconds=router_request.read_timeout_seconds,
                purpose="intent_router",
                transient_retries=0,
                trace_id=trace_id,
            )
        except LLMError as exc:
            failure_reason = str(getattr(exc, "reason", "") or "provider_error")
            logger.warning(
                "event=intent_router_transport_failure request_id=%s reason=%s error_type=%s repair_attempted=false",
                request_id,
                failure_reason,
                type(exc).__name__,
            )
            return self._failure(
                failure_reason,
                int((time.perf_counter() - started) * 1000),
                0,
                None,
                False,
                str(exc),
            )

        finish_reason = result.finish_reason
        content = (result.text or "").strip()
        completion_tokens = (result.usage or {}).get("completion_tokens", "")
        try:
            decision = self._decision_from_content(
                content,
                finish_reason,
                entities,
                routing_context,
                routing_state,
                ui_context,
                request_id,
            )
            return self._complete(decision, started, 0, finish_reason, bool(content), completion_tokens, request_id)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = str(exc) or "schema_validation_failed"

        if not content and finish_reason == "length":
            logger.warning(
                "event=intent_router_truncated_empty request_id=%s repair_attempted=false",
                request_id,
            )
            return self._failure(
                "empty_content_truncated",
                int((time.perf_counter() - started) * 1000),
                0,
                finish_reason,
                False,
            )

        if not self.settings.intent_router_retry_enabled:
            return self._failure(
                last_error,
                int((time.perf_counter() - started) * 1000),
                0,
                finish_reason,
                bool(content),
            )

        repair_deployment = self.settings.deployment_for_purpose("intent_router_repair")
        repair_request = repair_deployment.request_config("intent_router_repair")
        logger.info(
            "event=intent_router_repair_started request_id=%s router_repair_deployment=%s router_repair_provider=%s router_repair_model=%s router_repair_engine=%s reason=%s router_max_tokens=%s",
            request_id,
            repair_deployment.name,
            repair_deployment.provider_type,
            repair_deployment.model,
            repair_deployment.model,
            last_error[:120],
            repair_request.max_tokens,
        )
        repair_payload = {
            "invalid_output": content[:1200],
            "routing_context": routing_context,
            "validation_error": last_error[:160],
        }
        repair_messages = [
            {"role": "system", "content": ROUTER_REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(repair_payload, sort_keys=True)},
        ]
        try:
            repair_result = self.llm_client.chat(
                repair_messages,
                request_id=request_id,
                max_tokens=repair_request.max_tokens,
                temperature=repair_request.temperature,
                top_p=repair_request.top_p,
                timeout_seconds=repair_request.read_timeout_seconds,
                purpose="intent_router_repair",
                transient_retries=0,
                trace_id=trace_id,
            )
        except LLMError as exc:
            failure_reason = str(getattr(exc, "reason", "") or "provider_error")
            logger.warning(
                "event=intent_router_repair_failed request_id=%s reason=%s error_type=%s",
                request_id,
                failure_reason,
                type(exc).__name__,
            )
            return self._failure(
                f"repair_{failure_reason}",
                int((time.perf_counter() - started) * 1000),
                1,
                finish_reason,
                bool(content),
                str(exc),
            )

        repair_content = (repair_result.text or "").strip()
        repair_finish_reason = repair_result.finish_reason
        repair_completion_tokens = (repair_result.usage or {}).get("completion_tokens", "")
        try:
            decision = self._decision_from_content(
                repair_content,
                repair_finish_reason,
                entities,
                routing_context,
                routing_state,
                ui_context,
                request_id,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = str(exc) or "schema_validation_failed"
            logger.warning(
                "event=intent_router_repair_failed request_id=%s reason=%s completion_tokens=%s",
                request_id,
                last_error[:120],
                repair_completion_tokens,
            )
            return self._failure(
                last_error,
                int((time.perf_counter() - started) * 1000),
                1,
                repair_finish_reason,
                bool(repair_content),
            )

        logger.info(
            "event=intent_router_repair_succeeded request_id=%s completion_tokens=%s",
            request_id,
            repair_completion_tokens,
        )
        return self._complete(
            IntentDecision(**{**decision.__dict__, "decision_source": "semantic_router_repair"}),
            started,
            1,
            repair_finish_reason,
            bool(repair_content),
            repair_completion_tokens,
            request_id,
        )

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
            logger.warning("event=intent_router_json_parse_failed request_id=%s reason=missing_content", request_id)
            raise ValueError("missing_content")
        if finish_reason == "length":
            logger.warning("event=intent_router_json_parse_failed request_id=%s reason=finish_reason_length", request_id)
            raise ValueError("finish_reason_length")
        try:
            extracted = _extract_first_json_object(content)
            logger.info("event=intent_router_json_extracted request_id=%s chars=%s", request_id, len(extracted))
            payload = json.loads(extracted)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "event=intent_router_json_parse_failed request_id=%s reason=%s",
                request_id,
                str(exc)[:120] or type(exc).__name__,
            )
            raise
        if not isinstance(payload, dict):
            logger.warning("event=intent_router_json_parse_failed request_id=%s reason=not_object", request_id)
            raise ValueError("malformed_json:not_object")
        try:
            return validate_router_payload(
                payload,
                entities,
                min_confidence=self.settings.intent_router_min_confidence,
                message=str(routing_context.get("message") or ""),
                routing_state=routing_state,
                ui_context=ui_context,
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "event=intent_router_schema_validation_failed request_id=%s reason=%s",
                request_id,
                str(exc)[:120] or type(exc).__name__,
            )
            raise

    @staticmethod
    def _complete(
        decision: IntentDecision,
        started: float,
        retry_count: int,
        finish_reason: str | None,
        content_present: bool,
        completion_tokens: object,
        request_id: str,
    ) -> IntentDecision:
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
            "event=intent_router_complete request_id=%s decision_source=%s intent=%s scope=%s direction=%s depth=%s requires_graph=%s requires_detection=%s requires_asset_profile=%s requires_knowledge=%s entity_binding=%s binding_source=%s binding_available=%s binding_normalized=%s binding_normalization_reason=%s materialized_entity_count=%s route_normalized=%s route_normalization_reason=%s confidence=%s retry_count=%s latency_ms=%s finish_reason=%s content_present=%s completion_tokens=%s",
            request_id,
            decision.decision_source,
            decision.intent,
            decision.scope,
            decision.direction,
            decision.depth,
            decision.requires_graph,
            decision.requires_detection,
            decision.requires_asset_profile,
            decision.requires_knowledge,
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
            completion_tokens,
        )
        return decision

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
        logger.warning("event=intent_router_fallback_used reason=%s retry_count=%s", reason, retry_count)
        return IntentDecision(
            intent="unclear",
            scope="none",
            direction="none",
            depth=0,
            requires_graph=False,
            classification_confidence=0.0,
            reason="Router failed; deterministic fallback required.",
            decision_source="deterministic_fallback",
            router_called=True,
            latency_ms=latency_ms,
            retry_count=retry_count,
            finish_reason=finish_reason,
            content_present=content_present,
            error_reason=reason,
            fallback_used=True,
            fallback_reason=reason,
        )
