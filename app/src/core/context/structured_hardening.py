"""Deterministic hardening for structured Asset-set semantics.

This module is deliberately small and allow-list based. It never emits Cypher,
never invents conversational entities, and only normalizes semantics already
represented by StructuredQuerySpec.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from src.core.context.entities import EntityResolver as BaseEntityResolver
from src.core.context.models import EntityResolution, RouteDecision
from src.core.context.router import DeterministicFallbackRouter as BaseFallbackRouter
from src.core.graph.structured import (
    AssetAggregateOperation,
    AssetGroupField,
    AssetSearchFilters,
    AssetSortField,
    SortDirection,
    StructuredQueryMode,
    StructuredQuerySpec,
)


_TEXT_FILTERS = (
    "asset_name",
    "status",
    "suggested_type",
    "classification_summary",
    "role",
    "roles",
    "vendor",
    "product",
    "tag",
    "sub_tag",
    "enrichment_status",
)
_STRICT_ABOVE = re.compile(r"\b(?:above|greater\s+than|more\s+than|over)\b", re.IGNORECASE)
_STRICT_BELOW = re.compile(r"\b(?:below|less\s+than|under)\b", re.IGNORECASE)
_RANKED_HIGH = re.compile(r"\b(?:highest|top(?:\s+one)?)\b", re.IGNORECASE)
_RANKED_LOW = re.compile(r"\b(?:lowest|bottom(?:\s+one)?)\b", re.IGNORECASE)
_SET_VERB = re.compile(r"\b(?:list|find|show|count|group|how\s+many)\b", re.IGNORECASE)
_SET_NOUN = re.compile(r"\b(?:assets?|systems?|devices?)\b", re.IGNORECASE)
_EXPLICIT_IP = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


@dataclass(frozen=True)
class StructuredFallbackDecision:
    intent: str
    query: StructuredQuerySpec
    reason: str


def _normalized_filters(filters: AssetSearchFilters) -> AssetSearchFilters:
    values = filters.model_dump()
    for name in _TEXT_FILTERS:
        value = values.get(name)
        if isinstance(value, str):
            values[name] = value.strip().casefold()
    return AssetSearchFilters.model_validate(values)


def normalize_structured_query_for_language(
    query: StructuredQuerySpec,
    message: str,
) -> StructuredQuerySpec:
    """Canonicalize text selectors and preserve strict/ranked user semantics.

    Stored Product values keep their display casing; Neo4j comparison performs
    case-insensitive equality for text selectors. Strict natural-language
    boundaries use the next representable float so the existing inclusive range
    schema remains backward compatible. Ranked single-selection requests fetch
    two rows so the runtime can distinguish a unique top result from a tie while
    still deepening at most one focal Asset.
    """

    filters = _normalized_filters(query.filters)
    values = filters.model_dump()
    min_names = (
        "model_confidence_min",
        "mapping_confidence_min",
        "unknown_score_min",
    )
    max_names = (
        "model_confidence_max",
        "mapping_confidence_max",
        "unknown_score_max",
    )
    present_min = [name for name in min_names if values.get(name) is not None]
    present_max = [name for name in max_names if values.get(name) is not None]
    if _STRICT_ABOVE.search(message) and len(present_min) == 1:
        name = present_min[0]
        values[name] = math.nextafter(float(values[name]), math.inf)
    if _STRICT_BELOW.search(message) and len(present_max) == 1:
        name = present_max[0]
        values[name] = math.nextafter(float(values[name]), -math.inf)
    filters = AssetSearchFilters.model_validate(values)

    updates: dict[str, object] = {"filters": filters}
    if query.mode is StructuredQueryMode.SEARCH and query.sort is not None:
        if _RANKED_HIGH.search(message) or _RANKED_LOW.search(message):
            updates["limit"] = max(2, int(query.limit or 0))
    return query.model_copy(update=updates)


def looks_like_structured_set_request(message: str) -> bool:
    """Conservatively identify self-contained set operations before UI binding.

    Explicit IPs deliberately opt out so ordinary focal-asset questions preserve
    the established explicit > UI > active authority.
    """

    text = (message or "").strip()
    if not text or _EXPLICIT_IP.search(text):
        return False
    if re.search(
        r"\bgroup\s+(?:all\s+)?assets?\s+by\s+(?:role|status|vendor|product|tag|sub\s*tag|suggested\s+type|enrichment\s+status)\b",
        text,
        re.I,
    ):
        return True
    if re.search(
        r"\bhow\s+many\b.*\b(?:assets?|controllers?|servers?|firewalls?|workstations?)\b",
        text,
        re.I,
    ):
        return True
    if _SET_VERB.search(text) and (
        _SET_NOUN.search(text)
        or re.search(
            r"\b(?:domain\s+controllers?|database\s+servers?|firewalls?|siem|splunk\s+indexers?|hypervisors?)\b",
            text,
            re.I,
        )
    ):
        return True
    return False


class StructuredAwareEntityResolver(BaseEntityResolver):
    """Prevent incidental UI selection from contaminating self-contained set queries."""

    def resolve(
        self,
        message: str,
        ui_context: dict[str, Any] | None = None,
        routing_state: Any = None,
        **kwargs: Any,
    ) -> EntityResolution:
        if looks_like_structured_set_request(message) and not _EXPLICIT_IP.search(message or ""):
            ui_context = None
        return super().resolve(
            message,
            ui_context,
            routing_state,
            **kwargs,
        )


def deterministic_structured_fallback(message: str) -> StructuredFallbackDecision | None:
    """Parse only unambiguous common set requests when the semantic Router fails."""

    text = " ".join((message or "").strip().split())
    if not looks_like_structured_set_request(text):
        return None

    group = re.search(
        r"\bgroup\s+(?:all\s+)?assets?\s+by\s+(role|status|vendor|product|tag|sub\s*tag|suggested\s+type|enrichment\s+status)\b",
        text,
        re.I,
    )
    if group:
        token = re.sub(r"\s+", "_", group.group(1).casefold())
        mapping = {
            "role": AssetGroupField.ROLE,
            "status": AssetGroupField.STATUS,
            "vendor": AssetGroupField.VENDOR,
            "product": AssetGroupField.PRODUCT,
            "tag": AssetGroupField.TAG,
            "sub_tag": AssetGroupField.SUB_TAG,
            "suggested_type": AssetGroupField.SUGGESTED_TYPE,
            "enrichment_status": AssetGroupField.ENRICHMENT_STATUS,
        }
        field = mapping.get(token)
        if field is None:
            return None
        return StructuredFallbackDecision(
            "asset_aggregate",
            StructuredQuerySpec(
                mode=StructuredQueryMode.AGGREGATE,
                operation=AssetAggregateOperation.GROUP_COUNT,
                group_by=field,
            ),
            "deterministic_structured_group_count",
        )

    role: str | None = None
    explicit_role = re.search(
        r"\brole\s+(?:is\s+)?(.+?)(?=\s+(?:and|with|having|where|whose|above|below|over|under)\b|[?.!,]|$)",
        text,
        re.I,
    )
    if explicit_role:
        role = explicit_role.group(1).strip()
    else:
        known = re.search(
            r"\b(domain\s+controller|database\s+server|firewall|siem|splunk\s+indexer|hypervisor|windows\s+workstation|linux\s+server)\b",
            text,
            re.I,
        )
        if known:
            role = known.group(1).strip()

    values: dict[str, object] = {}
    if role:
        values["role"] = role
    if re.search(r"\bconfirmed\b", text, re.I):
        values["status"] = "confirmed"

    confidence = re.search(
        r"\bmodel\s+confidence\s+(?:above|greater\s+than|more\s+than|over|at\s+least)\s+(0(?:\.\d+)?|1(?:\.0+)?)",
        text,
        re.I,
    )
    if confidence:
        values["model_confidence_min"] = float(confidence.group(1))

    filters = AssetSearchFilters.model_validate(values)
    if re.search(r"\bhow\s+many\b|\bcount\b", text, re.I) and role:
        query = StructuredQuerySpec(
            mode=StructuredQueryMode.AGGREGATE,
            filters=filters,
            operation=AssetAggregateOperation.COUNT,
        )
        return StructuredFallbackDecision(
            "asset_aggregate",
            normalize_structured_query_for_language(query, text),
            "deterministic_structured_count",
        )

    if not values:
        return None

    sort = None
    direction = None
    limit = None
    if re.search(r"\bhighest\s+model\s+confidence\b", text, re.I):
        sort, direction, limit = AssetSortField.MODEL_CONFIDENCE, SortDirection.DESC, 2
    elif re.search(r"\blowest\s+model\s+confidence\b", text, re.I):
        sort, direction, limit = AssetSortField.MODEL_CONFIDENCE, SortDirection.ASC, 2
    query = StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=filters,
        sort=sort,
        direction=direction,
        limit=limit,
    )
    return StructuredFallbackDecision(
        "asset_search",
        normalize_structured_query_for_language(query, text),
        "deterministic_structured_search",
    )


class StructuredAwareFallbackRouter(BaseFallbackRouter):
    """Add a fail-closed Phase-4 set route before the legacy entity fallback."""

    def route(
        self,
        message: str,
        entities: EntityResolution,
        routing_state: Any = None,
        **kwargs: Any,
    ) -> RouteDecision:
        structured = deterministic_structured_fallback(message)
        if structured is not None:
            return RouteDecision(
                use_graph=True,
                reason=structured.reason,
                structured_query=structured.query,
                entity_binding="none",
                requested_entity_binding="none",
                resolved_entity_binding="none",
                binding_source="none",
                binding_available=True,
                binding_normalized=True,
                binding_normalization_reason="deterministic_structured_router_fallback",
                materialized_entity_count=0,
                materialized_entities=(),
                target_entity=None,
                target_entities=[],
                matched_signals=[structured.intent, "structured_asset_set", "router_fallback"],
                graph_intent_detected=True,
                asset_investigation_detected=False,
                followup_detected=False,
                intent=structured.intent,  # type: ignore[arg-type]
                scope="none",
                direction="none",
                depth=0,
                requires_multiple_entities=False,
                relationship_mode="none",
                intent_confidence=1.0,
                decision_source="deterministic_fallback",
                fallback_used=True,
                fallback_reason=str(kwargs.get("fallback_reason") or "semantic_router_unavailable"),
                route_normalized=True,
                route_normalization_reason="deterministic_structured_router_fallback",
            )
        return super().route(message, entities, routing_state, **kwargs)
