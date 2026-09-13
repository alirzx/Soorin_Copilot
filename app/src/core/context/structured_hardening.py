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
from src.core.context.models import (
    EntityResolution,
    RouteDecision,
    StructuredResultReferenceDecision,
)
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
_RANKED_HIGH = re.compile(r"\b(?:highest|top(?:\s+one)?)\b", re.IGNORECASE)
_RANKED_LOW = re.compile(r"\b(?:lowest|bottom(?:\s+one)?)\b", re.IGNORECASE)
_SET_VERB = re.compile(r"\b(?:list|find|show|count|group|how\s+many)\b", re.IGNORECASE)
_SET_NOUN = re.compile(r"\b(?:assets?|systems?|devices?)\b", re.IGNORECASE)
_EXPLICIT_IP = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_STRUCTURED_SET_REFERENCE = re.compile(
    r"\b(?:which\s+of\s+(?:them|those)|which\s+ones?|among\s+(?:them|those)|"
    r"of\s+those|from\s+those|those\s+(?:assets?|devices?|results?)|these\s+results?|"
    r"the\s+same\s+(?:set|list)|that\s+list|from\s+that\s+list|the\s+results?\s+above|"
    r"group\s+(?:them|those|these)|count\s+(?:them|those|these)|"
    r"(?:which|list|show)\s+(?:member\s+)?ips?\s+(?:are\s+)?(?:in|from|belong(?:ing)?\s+to))\b",
    re.IGNORECASE,
)
_SCORE_ALIASES: dict[str, str] = {
    "model_confidence": (
        r"(?:model|classification|classifier)\s+(?:confidence|certainty)|"
        r"confidence\s+of\s+classification|"
        r"(?<!mapping\s)(?<!role-mapping\s)confidence(?!\s+of\s+mapping)"
    ),
    "mapping_confidence": (
        r"mapping\s+(?:confidence|certainty)|confidence\s+of\s+mapping|"
        r"role[-\s]+mapping\s+confidence"
    ),
    "unknown_score": (
        r"unknown\s+(?:score|probability)|uncertainty\s+score|classification\s+uncertainty"
    ),
}
_COMPARATORS: dict[str, tuple[str, bool]] = {
    "above": ("min", True),
    "greater than": ("min", True),
    "more than": ("min", True),
    "over": ("min", True),
    "at least": ("min", False),
    "below": ("max", True),
    "less than": ("max", True),
    "under": ("max", True),
    "at most": ("max", False),
}
_COMPARATOR_PATTERN = "|".join(
    re.escape(item) for item in sorted(_COMPARATORS, key=len, reverse=True)
)


@dataclass(frozen=True)
class StructuredFallbackDecision:
    intent: str
    query: StructuredQuerySpec
    reason: str
    reference: StructuredResultReferenceDecision = StructuredResultReferenceDecision()


def normalize_natural_score(value: Any, *, percent_explicit: bool = False) -> float:
    """Normalize an unambiguous fraction/percentage without weakening 0..1 validation."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a score")
    text = str(value).strip()
    if not text:
        raise ValueError("score is blank")
    if text.endswith("%"):
        percent_explicit = True
        text = text[:-1].strip()
    try:
        score = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("score is not numeric") from exc
    if not math.isfinite(score) or score < 0:
        raise ValueError("score is outside the supported range")
    if percent_explicit:
        if score > 100:
            raise ValueError("percentage is outside 0..100")
        return score / 100.0
    if score <= 1:
        return score
    # Bare whole numbers such as 90 are conventional percentage shorthand.
    # Decimal values above one (for example 2.4) are ambiguous and fail closed.
    if score <= 100 and score.is_integer():
        return score / 100.0
    raise ValueError("bare score above one is ambiguous")


def normalize_structured_query_payload_for_language(
    raw_query: Any,
    message: str,
) -> Any:
    """Repair natural score units before Pydantic enforces the strict typed contract."""
    if not isinstance(raw_query, dict):
        return raw_query
    normalized = dict(raw_query)
    raw_filters = normalized.get("filters")
    if not isinstance(raw_filters, dict):
        return normalized
    filters = dict(raw_filters)
    for name in (
        "model_confidence_min",
        "model_confidence_max",
        "mapping_confidence_min",
        "mapping_confidence_max",
        "unknown_score_min",
        "unknown_score_max",
    ):
        if name in filters and filters[name] is not None:
            score_name = name.rsplit("_", 1)[0]
            alias_pattern = _SCORE_ALIASES[score_name]
            percent_explicit = bool(
                re.search(
                    rf"\b(?:{alias_pattern})\s+(?:{_COMPARATOR_PATTERN})\s+"
                    r"-?\d+(?:\.\d+)?\s*(?:%(?!\w)|percent\b)",
                    message or "",
                    re.I,
                )
            )
            filters[name] = normalize_natural_score(
                filters[name],
                percent_explicit=percent_explicit,
            )
    normalized["filters"] = filters
    return normalized


def merge_structured_query_for_followup(
    previous: StructuredQuerySpec,
    current: StructuredQuerySpec,
) -> StructuredQuerySpec:
    """Apply current selectors to the retained query; refs remain identity-only."""
    merged_filters = previous.filters.model_dump(exclude_none=True)
    merged_filters.update(current.filters.model_dump(exclude_none=True))
    updates: dict[str, Any] = {
        "filters": AssetSearchFilters.model_validate(merged_filters),
    }
    if current.mode is StructuredQueryMode.SEARCH and previous.mode is StructuredQueryMode.SEARCH:
        if current.sort is None and previous.sort is not None:
            updates["sort"] = previous.sort
        if current.direction is None and previous.direction is not None:
            updates["direction"] = previous.direction
    return current.model_copy(update=updates)


def looks_like_structured_set_reference(message: str) -> bool:
    return bool(_STRUCTURED_SET_REFERENCE.search(message or ""))


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
    for score_name, alias_pattern in _SCORE_ALIASES.items():
        minimum = f"{score_name}_min"
        maximum = f"{score_name}_max"
        if values.get(minimum) is not None and re.search(
            rf"\b(?:{alias_pattern})\s+(?:above|greater\s+than|more\s+than|over)\b",
            message,
            re.I,
        ):
            values[minimum] = math.nextafter(float(values[minimum]), math.inf)
        if values.get(maximum) is not None and re.search(
            rf"\b(?:{alias_pattern})\s+(?:below|less\s+than|under)\b",
            message,
            re.I,
        ):
            values[maximum] = math.nextafter(float(values[maximum]), -math.inf)
    filters = AssetSearchFilters.model_validate(values)

    updates: dict[str, object] = {"filters": filters}
    if query.mode is StructuredQueryMode.SEARCH and query.sort is not None:
        if _RANKED_HIGH.search(message) or _RANKED_LOW.search(message):
            updates["limit"] = max(2, int(query.limit or 0))
    return query.model_copy(update=updates)


def looks_like_structured_set_request(
    message: str,
    *,
    structured_context_available: bool = False,
) -> bool:
    """Conservatively identify self-contained set operations before UI binding.

    Explicit IPs deliberately opt out so ordinary focal-asset questions preserve
    the established explicit > UI > active authority.
    """

    text = (message or "").strip()
    if not text:
        return False
    if structured_context_available and looks_like_structured_set_reference(text):
        return True
    if _EXPLICIT_IP.search(text) and not re.search(
        r"\b(?:list|find|show)\b.*\bassets?\b.*\bip\b",
        text,
        re.I,
    ):
        return False
    if re.search(
        r"\bgroup\s+(?:(?:all\s+)?assets?|them|those|these)\s+by\s+(?:role|status|vendor|product|tag|sub\s*tag|suggested\s+type|enrichment\s+status)\b",
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
        structured_context_available = bool(
            getattr(routing_state, "structured_query_context", None)
        )
        if looks_like_structured_set_request(
            message,
            structured_context_available=structured_context_available,
        ):
            ui_context = None
        return super().resolve(
            message,
            ui_context,
            routing_state,
            **kwargs,
        )


def _captured_value(match: re.Match[str] | None) -> str | None:
    if match is None:
        return None
    value = match.group("value").strip(" .,:;?!")
    return value or None


def _natural_filter_values(message: str) -> tuple[dict[str, object], bool]:
    """Extract only bounded, allow-listed, unambiguous selector phrases."""
    text = message
    values: dict[str, object] = {}
    invalid_score = False

    role_match = re.search(
        r"\b(?:role(?:\s+is)?|primary\s+(?:role|function)(?:\s+is)?|"
        r"(?:device\s+)?function(?:\s+is)?|serves\s+as|acts\s+as)\s+"
        r"(?P<value>.+?)(?=\s+(?:and|with|having|where|whose|above|below|over|under|at\s+least|at\s+most)\b|[?.!,]|$)",
        text,
        re.I,
    )
    roles_match = re.search(
        r"\b(?:also\s+has|includes?|carries|has)\s+(?:the\s+)?role\s+"
        r"(?P<value>.+?)(?=\s+(?:and|with|having|where|whose)\b|[?.!,]|$)",
        text,
        re.I,
    )
    if value := _captured_value(roles_match):
        values["roles"] = value
    elif value := _captured_value(role_match):
        values["role"] = value
    else:
        known = re.search(
            r"\b(domain\s+controller|database\s+server|firewall|siem|splunk\s+indexer|"
            r"hypervisor|windows\s+workstation|linux\s+server)\b",
            text,
            re.I,
        )
        if known:
            values["role"] = known.group(1)

    exact_patterns = {
        "vendor": r"\b(?:made\s+by|manufacturer(?:\s+is)?|maker(?:\s+is)?|vendor(?:\s+is)?)\s+(?P<value>[\w.&-]+)",
        "suggested_type": r"\b(?:suggested\s+type|asset\s+type|device\s+type|kind\s+of\s+(?:asset|device)|classification\s+type)(?:\s+is)?\s+(?P<value>[\w][\w .&/-]*?)(?=[?.!,]|$)",
        "product": r"\b(?:product(?:\s+family)?|platform/product)(?:\s+is)?\s+(?P<value>[\w][\w .&/-]*?)(?=[?.!,]|$)",
        "tag": r"\b(?:tag|asset\s+label)(?:\s+is)?\s+(?P<value>[\w][\w .&/-]*?)(?=[?.!,]|$)",
        "sub_tag": r"\b(?:sub[-\s]?tag|secondary\s+tag|subclassification\s+label)(?:\s+is)?\s+(?P<value>[\w][\w .&/-]*?)(?=[?.!,]|$)",
        "asset_name": r"\b(?:asset\s+name(?:\s+is)?|hostname/name(?:\s+is)?|name\s+is|named)\s+(?P<value>[\w][\w.-]*)(?=[?.!,]|$)",
        "classification_summary": r"\b(?:classification\s+(?:summary|description)|classifier\s+summary)(?:\s+is)?\s+(?P<value>.+?)(?=[?.!,]|$)",
        "enrichment_status": r"\b(?:enrichment\s+status)(?:\s+is)?\s+(?P<value>[\w-]+)(?=[?.!,]|$)",
    }
    for field, pattern in exact_patterns.items():
        if value := _captured_value(re.search(pattern, text, re.I)):
            values[field] = value

    status = re.search(r"\b(unconfirmed|confirmed)\b", text, re.I)
    if status:
        values["status"] = status.group(1)
    ip_match = re.search(r"\bip(?:\s+(?:is|address))?\s+(?P<value>(?:\d{1,3}\.){3}\d{1,3})\b", text, re.I)
    if value := _captured_value(ip_match):
        values["ip"] = value

    for score_name, alias_pattern in _SCORE_ALIASES.items():
        match = re.search(
            rf"\b(?:{alias_pattern})\s+(?P<operator>{_COMPARATOR_PATTERN})\s+"
            rf"(?P<value>-?\d+(?:\.\d+)?)\s*"
            rf"(?P<percent>%|percent\b)?(?=$|[\s,.;?!])",
            text,
            re.I,
        )
        if match is None:
            continue
        bound, _strict = _COMPARATORS[" ".join(match.group("operator").casefold().split())]
        try:
            value = normalize_natural_score(
                match.group("value"),
                percent_explicit=bool(match.group("percent")),
            )
        except ValueError:
            invalid_score = True
            continue
        values[f"{score_name}_{bound}"] = value

    timestamp = re.search(
        r"\b(?:last\s+(?:detection|detected|classified)|classification\s+time|"
        r"detection\s+timestamp|seen\s+by\s+detection)\s+"
        r"(?P<operator>after|before)\s+(?P<value>\d{4}-\d{2}-\d{2}(?:T[^\s,;]+)?)",
        text,
        re.I,
    )
    if timestamp:
        values[
            "last_detection_at_from"
            if timestamp.group("operator").casefold() == "after"
            else "last_detection_at_to"
        ] = timestamp.group("value").rstrip(".,;?!")
    return values, invalid_score


def _merge_referenced_query(
    prior_query: StructuredQuerySpec | None,
    current: StructuredQuerySpec,
    *,
    reference: bool,
) -> StructuredQuerySpec:
    return (
        merge_structured_query_for_followup(prior_query, current)
        if reference and prior_query is not None
        else current
    )


def deterministic_structured_fallback(
    message: str,
    *,
    prior_query: StructuredQuerySpec | None = None,
    prior_group_values: tuple[str, ...] = (),
) -> StructuredFallbackDecision | None:
    """Parse only unambiguous common set requests when the semantic Router fails."""

    text = " ".join((message or "").strip().split())
    reference = bool(prior_query is not None and looks_like_structured_set_reference(text))
    if not looks_like_structured_set_request(
        text,
        structured_context_available=prior_query is not None,
    ):
        return None

    membership = re.search(
        r"\b(?:which|list|show)\s+(?:member\s+)?ips?\s+"
        r"(?:are\s+)?(?:in|from|belong(?:ing)?\s+to)\s+(?:the\s+)?"
        r"(?P<value>.+?)(?:\s+(?:group|category|bucket))?[?.!,]*$",
        text,
        re.I,
    )
    if (
        membership is not None
        and prior_query is not None
        and prior_query.mode is StructuredQueryMode.AGGREGATE
        and prior_query.group_by is not None
    ):
        group_value = membership.group("value").strip(" .,:;?!")
        if prior_group_values and group_value.casefold() not in {
            value.casefold() for value in prior_group_values
        }:
            return None
        try:
            current = merge_structured_query_for_followup(
                prior_query,
                StructuredQuerySpec(
                    mode=StructuredQueryMode.SEARCH,
                    filters=AssetSearchFilters.model_validate(
                        {prior_query.group_by.value: group_value}
                    ),
                    sort=AssetSortField.IP,
                    direction=SortDirection.ASC,
                ),
            )
        except ValueError:
            return None
        return StructuredFallbackDecision(
            "asset_search",
            normalize_structured_query_for_language(current, text),
            "deterministic_structured_group_membership",
            StructuredResultReferenceDecision(kind="set_query"),
        )

    group = re.search(
        r"\bgroup\s+(?:(?:all\s+)?assets?|them|those|these)\s+by\s+(role|status|vendor|product|tag|sub\s*tag|suggested\s+type|enrichment\s+status)\b",
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
        try:
            query = _merge_referenced_query(
                prior_query,
                StructuredQuerySpec(
                    mode=StructuredQueryMode.AGGREGATE,
                    operation=AssetAggregateOperation.GROUP_COUNT,
                    group_by=field,
                ),
                reference=reference,
            )
        except ValueError:
            return None
        return StructuredFallbackDecision(
            "asset_aggregate",
            query,
            "deterministic_structured_group_count",
            StructuredResultReferenceDecision(kind="set_query") if reference else StructuredResultReferenceDecision(),
        )

    values, invalid_score = _natural_filter_values(text)
    if invalid_score:
        return None

    try:
        filters = AssetSearchFilters.model_validate(values)
    except ValueError:
        return None
    if re.search(r"\bhow\s+many\b|\bcount\b", text, re.I) and (values or reference):
        query = StructuredQuerySpec(
            mode=StructuredQueryMode.AGGREGATE,
            filters=filters,
            operation=AssetAggregateOperation.COUNT,
        )
        try:
            query = _merge_referenced_query(
                prior_query,
                query,
                reference=reference,
            )
            query = normalize_structured_query_for_language(query, text)
        except ValueError:
            return None
        return StructuredFallbackDecision(
            "asset_aggregate",
            query,
            "deterministic_structured_count",
            StructuredResultReferenceDecision(kind="set_query") if reference else StructuredResultReferenceDecision(),
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
    try:
        query = _merge_referenced_query(
            prior_query,
            query,
            reference=reference,
        )
        query = normalize_structured_query_for_language(query, text)
    except ValueError:
        return None
    return StructuredFallbackDecision(
        "asset_search",
        query,
        "deterministic_structured_search",
        StructuredResultReferenceDecision(kind="set_query") if reference else StructuredResultReferenceDecision(),
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
        context = getattr(routing_state, "structured_query_context", None)
        structured = deterministic_structured_fallback(
            message,
            prior_query=getattr(context, "query", None),
            prior_group_values=tuple(
                str(group.value)
                for group in tuple(getattr(context, "aggregate_groups", ()) or ())
                if group.value is not None
            ),
        )
        if structured is not None:
            return RouteDecision(
                use_graph=True,
                reason=structured.reason,
                structured_query=structured.query,
                structured_result_reference=structured.reference,
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
        if looks_like_structured_set_request(
            message,
            structured_context_available=context is not None,
        ):
            return RouteDecision(
                use_graph=False,
                reason="deterministic_structured_parse_unavailable",
                entity_binding="none",
                requested_entity_binding="none",
                resolved_entity_binding="none",
                binding_source="none",
                binding_available=True,
                materialized_entity_count=0,
                materialized_entities=(),
                target_entity=None,
                target_entities=[],
                matched_signals=["structured_asset_set", "router_fallback", "fail_closed"],
                intent="unclear",
                scope="none",
                direction="none",
                depth=0,
                decision_source="deterministic_fallback",
                fallback_used=True,
                fallback_reason=str(
                    kwargs.get("fallback_reason") or "semantic_router_unavailable"
                ),
            )
        return super().route(message, entities, routing_state, **kwargs)
