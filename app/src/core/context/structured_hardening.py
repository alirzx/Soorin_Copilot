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
    AssetOutputField,
    AssetPredicate,
    AssetPredicateField,
    AssetPredicateOperator,
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
_EXPLICIT_CARDINALITY = re.compile(
    r"\b(?:top|first|show|list|return|only)\s+(?P<count>\d{1,4})\b|"
    r"\breturn\s+the\s+first\s+(?P<return_count>\d{1,4})\b",
    re.IGNORECASE,
)
_FRESH_RERUN = re.compile(
    r"\b(?:find|search|show|list|run|rerun)\s+again\b|"
    r"\bagain\s+(?:find|search|show|list|all)\b|\brerun\b",
    re.IGNORECASE,
)
_FALLBACK_SET_VERB = re.compile(
    r"\b(?:list|find|show|count|group|how\s+many)\b",
    re.IGNORECASE,
)
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
_EXPLICIT_PREVIOUS_SET_REFERENCE = re.compile(
    r"\b(?:previous|prior|earlier|original|base|latest|refined)\s+(?:structured\s+)?"
    r"(?:set|list|results?)\b|\b(?:those|these)\s+results?\b|"
    r"\b(?:the\s+)?results?\s+(?:above|you\s+just\s+returned)\b|"
    r"\bwhich\s+of\s+(?:them|those)\b|\bfrom\s+(?:that|the)\s+list\b",
    re.IGNORECASE,
)
_SAME_TURN_SET_ANTECEDENT = re.compile(
    r"\b(?:find|list|show|select)\b[\s\S]{1,260}?\b(?:assets?|systems?|devices?|servers?|"
    r"controllers?|firewalls?)\b[\s\S]{0,180}?\b(?:them|their|those|these|one)\b",
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

_AMBIGUOUS_SCORE_QUALIFIER = re.compile(
    r"\b(?:high|low)\s+(?:(?:model|mapping|classification)\s+)?(?:confidence|certainty)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_MATERIAL_SELECTOR = re.compile(
    r"\b(?:owned\s+by|owner(?:\s+is)?|department(?:\s+is)?|business\s+unit(?:\s+is)?|"
    r"location(?:\s+is)?|operating\s+system(?:\s+is)?|os\s+is)\b",
    re.IGNORECASE,
)
_GROUP_FIELD_ALIASES: dict[str, AssetGroupField] = {
    "ip": AssetGroupField.IP,
    "ip address": AssetGroupField.IP,
    "asset name": AssetGroupField.ASSET_NAME,
    "name": AssetGroupField.ASSET_NAME,
    "status": AssetGroupField.STATUS,
    "suggested type": AssetGroupField.SUGGESTED_TYPE,
    "asset type": AssetGroupField.SUGGESTED_TYPE,
    "model confidence": AssetGroupField.MODEL_CONFIDENCE,
    "classification confidence": AssetGroupField.MODEL_CONFIDENCE,
    "mapping confidence": AssetGroupField.MAPPING_CONFIDENCE,
    "unknown score": AssetGroupField.UNKNOWN_SCORE,
    "classification summary": AssetGroupField.CLASSIFICATION_SUMMARY,
    "classifcation summary": AssetGroupField.CLASSIFICATION_SUMMARY,
    "classifier summary": AssetGroupField.CLASSIFICATION_SUMMARY,
    "vendor": AssetGroupField.VENDOR,
    "product": AssetGroupField.PRODUCT,
    "role": AssetGroupField.ROLE,
    "roles": AssetGroupField.ROLES,
    "tag": AssetGroupField.TAG,
    "sub tag": AssetGroupField.SUB_TAG,
    "sub-tag": AssetGroupField.SUB_TAG,
    "last detection": AssetGroupField.LAST_DETECTION_AT,
    "last detection time": AssetGroupField.LAST_DETECTION_AT,
    "last detection timestamp": AssetGroupField.LAST_DETECTION_AT,
    "enrichment status": AssetGroupField.ENRICHMENT_STATUS,
}
_CANONICAL_ASSET_CLASSES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{pattern}\b", re.IGNORECASE), canonical)
    for pattern, canonical in (
        (r"(?:domain\s+controllers?|dc\s+assets?)", "Domain Controller"),
        (r"database\s+servers?", "Database Server"),
        (r"firewalls?", "Firewall"),
        (r"splunk\s+indexers?", "Splunk Indexer"),
        (r"siem(?:\s+assets?)?", "SIEM"),
        (r"hypervisors?", "Hypervisor"),
        (r"windows\s+workstations?", "Windows Workstation"),
        (r"linux\s+servers?", "Linux Server"),
    )
)
_CLASS_PREDICATE_FIELDS = frozenset({
    AssetPredicateField.SUGGESTED_TYPE,
    AssetPredicateField.ROLE,
    AssetPredicateField.ROLES,
    AssetPredicateField.CLASSIFICATION_SUMMARY,
})


@dataclass(frozen=True)
class StructuredFallbackDecision:
    intent: str
    query: StructuredQuerySpec
    reason: str
    reference: StructuredResultReferenceDecision = StructuredResultReferenceDecision()


def structured_material_gap_reason(message: str) -> str | None:
    """Identify material selector language that the allow-list cannot encode exactly."""
    text = message or ""
    if _AMBIGUOUS_SCORE_QUALIFIER.search(text):
        return "ambiguous_confidence_threshold"
    if _UNSUPPORTED_MATERIAL_SELECTOR.search(text):
        return "unsupported_structured_property"
    return None


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
    if isinstance(filters.get("predicate"), dict):
        filters["predicate"] = _normalize_predicate_payload_scores(
            filters["predicate"],
            message,
        )
    return normalized


def _normalize_predicate_payload_scores(payload: dict[str, Any], message: str) -> dict[str, Any]:
    normalized = dict(payload)
    for key in ("all", "any"):
        if isinstance(normalized.get(key), list):
            normalized[key] = [
                _normalize_predicate_payload_scores(item, message)
                if isinstance(item, dict) else item
                for item in normalized[key]
            ]
    if isinstance(normalized.get("not"), dict):
        normalized["not"] = _normalize_predicate_payload_scores(normalized["not"], message)
    field = str(normalized.get("field") or "")
    if field not in _SCORE_ALIASES:
        return normalized
    if "value" in normalized:
        normalized["value"] = normalize_natural_score(normalized["value"])
    if isinstance(normalized.get("values"), list):
        normalized["values"] = [normalize_natural_score(item) for item in normalized["values"]]
    operator = str(normalized.get("operator") or "")
    alias = _SCORE_ALIASES[field]
    if operator == "gte" and re.search(
        rf"\b(?:{alias})\s+(?:above|greater\s+than|more\s+than|over)\b", message, re.I
    ):
        normalized["operator"] = "gt"
    elif operator == "lte" and re.search(
        rf"\b(?:{alias})\s+(?:below|less\s+than|under)\b", message, re.I
    ):
        normalized["operator"] = "lt"
    return normalized


def merge_structured_query_for_followup(
    previous: StructuredQuerySpec,
    current: StructuredQuerySpec,
) -> StructuredQuerySpec:
    """Apply current selectors to the retained query; refs remain identity-only."""
    merged_filters = previous.filters.model_dump(exclude_none=True)
    merged_filters.update(current.filters.model_dump(exclude_none=True))
    previous_predicate = previous.filters.predicate
    current_predicate = current.filters.predicate
    if previous_predicate is not None and current_predicate is not None:
        merged_filters["predicate"] = AssetPredicate(all=(previous_predicate, current_predicate))
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
    return structured_reference_scope(message) == "previous"


def structured_reference_scope(message: str) -> str:
    """Distinguish same-turn antecedents from actual prior-result references."""
    text = message or ""
    if _FRESH_RERUN.search(text):
        return "fresh_rerun"
    if _EXPLICIT_PREVIOUS_SET_REFERENCE.search(text):
        return "previous"
    if _SAME_TURN_SET_ANTECEDENT.search(text):
        return "same_turn"
    return "previous" if _STRUCTURED_SET_REFERENCE.search(text) else "none"


def select_structured_query_context(routing_state: Any, message: str) -> Any:
    """Resolve explicit base/latest language against the bounded lineage."""
    current = getattr(routing_state, "structured_query_context", None)
    lineage = tuple(getattr(routing_state, "structured_query_lineage", ()) or ())
    if not lineage:
        lineage = (current,) if current is not None else ()
    text = message or ""
    if re.search(r"\b(?:original|base)\s+(?:structured\s+)?(?:set|list|results?)\b", text, re.I):
        return lineage[0] if lineage else current
    if re.search(r"\b(?:latest|refined)\s+(?:structured\s+)?(?:set|list|results?)\b", text, re.I):
        return lineage[-1] if lineage else current
    named_same = re.search(r"\b(?:the\s+)?same\s+(?P<label>[\w -]+?)\s+set\b", text, re.I)
    if named_same and len(lineage) > 1:
        label = " ".join(named_same.group("label").casefold().split())
        for context in lineage:
            role = str(getattr(getattr(context.query, "filters", None), "role", "") or "")
            if role.casefold() == label:
                return context
    return current


def _normalized_filters(filters: AssetSearchFilters) -> AssetSearchFilters:
    values = filters.model_dump()
    for name in _TEXT_FILTERS:
        value = values.get(name)
        if isinstance(value, str):
            values[name] = value.strip().casefold()
    predicate = filters.predicate
    if predicate is not None:
        values["predicate"] = _normalized_predicate(predicate)
    return AssetSearchFilters.model_validate(values)


def _normalized_predicate(predicate: AssetPredicate) -> AssetPredicate:
    if predicate.all:
        return AssetPredicate(all=tuple(_normalized_predicate(item) for item in predicate.all))
    if predicate.any:
        return AssetPredicate(any=tuple(_normalized_predicate(item) for item in predicate.any))
    if predicate.not_ is not None:
        return AssetPredicate.model_validate({"not": _normalized_predicate(predicate.not_)})
    assert predicate.field is not None and predicate.operator is not None
    text_fields = {
        AssetPredicateField.ASSET_NAME,
        AssetPredicateField.STATUS,
        AssetPredicateField.SUGGESTED_TYPE,
        AssetPredicateField.CLASSIFICATION_SUMMARY,
        AssetPredicateField.VENDOR,
        AssetPredicateField.PRODUCT,
        AssetPredicateField.ROLE,
        AssetPredicateField.ROLES,
        AssetPredicateField.TAG,
        AssetPredicateField.SUB_TAG,
        AssetPredicateField.ENRICHMENT_STATUS,
    }
    payload = predicate.model_dump(by_alias=True)
    if predicate.field in text_fields:
        if payload.get("value") is not None:
            payload["value"] = str(payload["value"]).strip().casefold()
        if payload.get("values"):
            payload["values"] = [str(item).strip().casefold() for item in payload["values"]]
    return AssetPredicate.model_validate(payload)


def normalize_structured_query_for_language(
    query: StructuredQuerySpec,
    message: str,
    *,
    normalize_asset_classes: bool = True,
) -> StructuredQuerySpec:
    """Canonicalize text selectors and preserve strict/ranked user semantics.

    Stored Product values keep their display casing; Neo4j comparison performs
    case-insensitive equality for text selectors. Strict natural-language
    boundaries use the next representable float so the existing inclusive range
    schema remains backward compatible. Ranked single-selection requests fetch
    three rows so the runtime can distinguish a unique result, an exact top-two
    tie, and a larger tie without inventing a winner.
    """

    class_value_hint = _query_class_value(query, message)
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

    normalized = query.model_copy(update={"filters": filters})
    if normalize_asset_classes:
        normalized = _normalize_asset_class_semantics(
            normalized,
            message,
            canonical_hint=class_value_hint,
        )
    requested_outputs = tuple(dict.fromkeys((
        *normalized.requested_output_fields,
        *_requested_output_fields(message),
    )))
    supplied_limit = (
        normalized.router_supplied_limit
        if normalized.router_supplied_limit is not None
        else normalized.limit
    )
    explicit_limit = _explicit_cardinality(message)
    updates: dict[str, object] = {
        "requested_output_fields": requested_outputs,
        "router_supplied_limit": supplied_limit,
        "user_explicit_limit": explicit_limit is not None,
        "fresh_rerun": structured_reference_scope(message) == "fresh_rerun",
        "limit": explicit_limit,
    }
    if normalized.mode is StructuredQueryMode.SEARCH and normalized.sort is not None:
        if _RANKED_HIGH.search(message) or _RANKED_LOW.search(message):
            updates["limit"] = max(3, int(explicit_limit or supplied_limit or 0))
    return normalized.model_copy(update=updates)


def _explicit_cardinality(message: str) -> int | None:
    match = _EXPLICIT_CARDINALITY.search(message or "")
    if match is None:
        return None
    value = int(match.group("count") or match.group("return_count"))
    return value if value > 0 else None


def _requested_output_fields(message: str) -> tuple[AssetOutputField, ...]:
    """Extract only a clearly introduced presentation/projection clause."""
    match = re.search(
        r"(?:,|\band\b)?\s*(?:show|display|return|include)(?:\s+with)?\s+"
        r"(?P<fields>(?:their\s+)?[^?.!;]+)$|\bshow\s+with\s+(?P<with_fields>[^?.!;]+)$",
        message or "",
        re.IGNORECASE,
    )
    if match is None:
        return ()
    text = (match.group("fields") or match.group("with_fields") or "").casefold()
    aliases: tuple[tuple[AssetOutputField, str], ...] = (
        (AssetOutputField.IP, r"\bips?\b|\bip\s+addresses?\b"),
        (AssetOutputField.ASSET_NAME, r"\basset\s+names?\b|\bhostnames?\b"),
        (AssetOutputField.VENDOR, r"\bvendors?\b|\bmanufacturers?\b"),
        (AssetOutputField.PRODUCT, r"\bproducts?\b"),
        (AssetOutputField.ROLE, r"\bprimary\s+roles?\b|\brole\b"),
        (AssetOutputField.ROLES, r"\broles\b"),
        (AssetOutputField.STATUS, r"\bstatus\b"),
        (AssetOutputField.MODEL_CONFIDENCE, r"\bmodel\s+confidence\b"),
        (AssetOutputField.MAPPING_CONFIDENCE, r"\bmapping\s+confidence\b"),
        (AssetOutputField.UNKNOWN_SCORE, r"\bunknown\s+score\b"),
        (AssetOutputField.CLASSIFICATION_SUMMARY, r"\bclassification\s+summary\b"),
        (AssetOutputField.SUGGESTED_TYPE, r"\bsuggested\s+type\b"),
        (AssetOutputField.TAG, r"\btags?\b"),
        (AssetOutputField.SUB_TAG, r"\bsub[-\s]?tags?\b"),
        (AssetOutputField.LAST_DETECTION_AT, r"\blast\s+detection(?:\s+time)?\b"),
        (AssetOutputField.COUNT, r"\bcounts?\b"),
        (AssetOutputField.PERCENTAGE, r"\bpercentages?\b|%"),
        (AssetOutputField.MEMBER_IPS, r"\bmember\s+ips?\b"),
    )
    return tuple(field for field, pattern in aliases if re.search(pattern, text, re.I))


def _canonical_asset_class(message: str) -> str | None:
    """Conservative vocabulary used only by deterministic fallback parsing."""
    for pattern, canonical in _CANONICAL_ASSET_CLASSES:
        if pattern.search(message or ""):
            return canonical
    return None


def _query_class_value(
    query: StructuredQuerySpec,
    message: str = "",
) -> str | None:
    """Use the semantic Router's typed selector as the primary class vocabulary."""
    fields = tuple(_CLASS_PREDICATE_FIELDS)
    allowed = set(fields)
    values: list[str] = []
    raw = query.filters.model_dump(mode="python")
    for field in fields:
        value = raw.get(field.value)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())

    def visit(predicate: AssetPredicate | None) -> None:
        if predicate is None:
            return
        for child in (*predicate.all, *predicate.any):
            visit(child)
        visit(predicate.not_)
        if predicate.field not in allowed:
            return
        candidates = predicate.values or (
            (predicate.value,) if predicate.value is not None else ()
        )
        values.extend(
            str(value).strip()
            for value in candidates
            if isinstance(value, str) and value.strip()
        )

    visit(query.filters.predicate)
    mentioned = tuple(
        value
        for value in values
        if value.casefold() in (message or "").casefold()
    )
    if mentioned:
        values = list(mentioned)
    unique = tuple(dict.fromkeys(value.casefold() for value in values))
    if len(unique) != 1:
        return None
    return next(value for value in values if value.casefold() == unique[0])


def _class_mapping(message: str) -> tuple[str, tuple[AssetPredicateField, ...]]:
    text = message or ""
    if re.search(r"\b(?:classification\s+(?:summary|description)|classifier\s+summary)\b", text, re.I):
        return "explicit_classification_summary", (AssetPredicateField.CLASSIFICATION_SUMMARY,)
    if re.search(
        r"\b(?:suggested\s+type|asset\s+type|device\s+type|"
        r"kind\s+of\s+(?:asset|device)|classification\s+type)\b",
        text,
        re.I,
    ):
        return "explicit_suggested_type", (AssetPredicateField.SUGGESTED_TYPE,)
    if re.search(
        r"\broles?\s+include(?:s|d)?\b|"
        r"\b(?:includes?|carries|has)\s+(?:the\s+)?role\b",
        text,
        re.I,
    ):
        return "explicit_roles_membership", (AssetPredicateField.ROLES,)
    if re.search(r"\brole\s+or\s+roles\b|\broles\s+or\s+role\b", text, re.I):
        return "explicit_role_or_roles", (
            AssetPredicateField.ROLE,
            AssetPredicateField.ROLES,
        )
    if re.search(
        r"\b(?:primary\s+role|role\s+(?:is|=)|(?:device\s+)?function)\b",
        text,
        re.I,
    ):
        return "explicit_role", (AssetPredicateField.ROLE,)
    return "generic_asset_class", (
        AssetPredicateField.SUGGESTED_TYPE,
        AssetPredicateField.ROLE,
        AssetPredicateField.ROLES,
    )


def _class_predicate(
    canonical: str,
    fields: tuple[AssetPredicateField, ...],
) -> AssetPredicate:
    leaves = tuple(
        AssetPredicate(
            field=field,
            operator=(
                AssetPredicateOperator.MEMBER_EQ
                if field is AssetPredicateField.ROLES
                else AssetPredicateOperator.EQ
            ),
            value=canonical,
        )
        for field in fields
    )
    return leaves[0] if len(leaves) == 1 else AssetPredicate(any=leaves)


def _replace_class_leaves(
    predicate: AssetPredicate,
    canonical: str,
    replacement: AssetPredicate,
) -> tuple[AssetPredicate, bool]:
    if _predicate_is_class_only(predicate, canonical):
        return replacement, True
    if predicate.all or predicate.any:
        children: list[AssetPredicate] = []
        replaced = False
        for child in predicate.all or predicate.any:
            updated, child_replaced = _replace_class_leaves(child, canonical, replacement)
            children.append(updated)
            replaced = replaced or child_replaced
        return (
            AssetPredicate(all=tuple(children))
            if predicate.all
            else AssetPredicate(any=tuple(children)),
            replaced,
        )
    if predicate.not_ is not None:
        updated, replaced = _replace_class_leaves(predicate.not_, canonical, replacement)
        return AssetPredicate.model_validate({"not": updated}), replaced
    values = predicate.values or ((predicate.value,) if predicate.value is not None else ())
    if (
        predicate.field in _CLASS_PREDICATE_FIELDS
        and any(str(value).strip().casefold() == canonical.casefold() for value in values)
    ):
        return replacement, True
    return predicate, False


def _predicate_is_class_only(predicate: AssetPredicate, canonical: str) -> bool:
    if predicate.all or predicate.any:
        return all(
            _predicate_is_class_only(child, canonical)
            for child in predicate.all or predicate.any
        )
    if predicate.not_ is not None or predicate.field not in _CLASS_PREDICATE_FIELDS:
        return False
    values = predicate.values or ((predicate.value,) if predicate.value is not None else ())
    return bool(values) and all(
        str(value).strip().casefold() == canonical.casefold() for value in values
    )


def _normalize_asset_class_semantics(
    query: StructuredQuerySpec,
    message: str,
    *,
    canonical_hint: str | None = None,
) -> StructuredQuerySpec:
    mapping_mode, selector_fields = _class_mapping(message)
    canonical = canonical_hint or _query_class_value(query)
    if canonical is None:
        return query
    replacement = _class_predicate(canonical, selector_fields)
    values = query.filters.model_dump()
    flat_replaced = False
    for name in ("suggested_type", "role", "roles", "classification_summary"):
        value = values.get(name)
        if isinstance(value, str) and value.strip().casefold() == canonical.casefold():
            values[name] = None
            flat_replaced = True
    predicate = query.filters.predicate
    predicate_replaced = False
    if predicate is not None:
        predicate, predicate_replaced = _replace_class_leaves(
            predicate,
            canonical,
            replacement,
        )
    if len(selector_fields) == 1:
        if not predicate_replaced:
            values[selector_fields[0].value] = canonical.casefold()
        values["predicate"] = predicate
        return query.model_copy(update={
            "filters": AssetSearchFilters.model_validate(values),
            "semantic_class": canonical,
            "class_mapping_mode": mapping_mode,
            "class_selector_fields": selector_fields,
        })
    if flat_replaced:
        predicate = (
            AssetPredicate(all=(predicate, replacement))
            if predicate is not None and not predicate_replaced
            else predicate or replacement
        )
    elif predicate is None:
        # The Router may correctly identify the class intent but omit the
        # selector field; bounded normalization can still compile this known
        # canonical class without inventing database vocabulary.
        predicate = replacement
    values["predicate"] = predicate
    return query.model_copy(update={
        "filters": AssetSearchFilters.model_validate(values),
        "semantic_class": canonical,
        "class_mapping_mode": mapping_mode,
        "class_selector_fields": selector_fields,
    })


def looks_like_structured_set_request(
    message: str,
    *,
    structured_context_available: bool = False,
) -> bool:
    """Recognize only bounded set forms for deterministic fallback parsing.

    This helper is not an input-vocabulary gate and must not run before the
    semantic Router. Explicit IPs opt out so fallback routing preserves the
    established explicit > UI > active authority.
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
        r"\bgroup\s+(?:(?:all\s+)?assets?|them|those|these)\s+by\s+(?:ip(?:\s+address)?|asset\s+name|name|status|suggested\s+type|asset\s+type|model\s+confidence|classification\s+confidence|mapping\s+confidence|unknown\s+score|classif(?:i)?cation\s+summary|classifier\s+summary|vendor|product|role|roles|tag|sub[-\s]?tag|last\s+detection(?:\s+(?:time|timestamp))?|enrichment\s+status)\b",
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
    if _FALLBACK_SET_VERB.search(text) and (
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
    """Preserve deterministic entity precedence until semantic routing."""

    def resolve(
        self,
        message: str,
        ui_context: dict[str, Any] | None = None,
        routing_state: Any = None,
        **kwargs: Any,
    ) -> EntityResolution:
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
        if canonical := _canonical_asset_class(text):
            values["role"] = canonical

    exact_patterns = {
        "vendor": r"\b(?:made\s+by|manufacturer\s+is|maker\s+is|vendor\s+is)\s+(?P<value>[\w.&-]+)",
        "suggested_type": r"\b(?:suggested\s+type|asset\s+type|device\s+type|kind\s+of\s+(?:asset|device)|classification\s+type)(?:\s+is)?\s+(?P<value>[\w][\w .&/-]*?)(?=[?.!,]|$)",
        "product": r"\b(?:product(?:\s+family)?|platform/product)(?:\s+is)?\s+(?P<value>[\w][\w .&/-]*?)(?=[?.!,]|$)",
        "tag": r"\b(?:tag|asset\s+label)(?:\s+is)?\s+(?P<value>[\w][\w .&/-]*?)(?=[?.!,]|$)",
        "sub_tag": r"\b(?:sub[-\s]?tag|secondary\s+tag|subclassification\s+label)(?:\s+is)?\s+(?P<value>[\w][\w .&/-]*?)(?=[?.!,]|$)",
        "asset_name": r"\b(?:asset\s+name(?:\s+is)?|hostname/name(?:\s+is)?|name\s+is|named)\s+(?P<value>[\w][\w.-]*)(?=[?.!,]|$)",
        "classification_summary": r"\b(?:classif(?:i)?cation\s+(?:summary|description)|classifier\s+summary)(?:\s+is)?\s+(?P<value>.+?)(?=[?.!,]|$)",
        "enrichment_status": r"\b(?:enrichment\s+status)(?:\s+is)?\s+(?P<value>[\w-]+)(?=[?.!,]|$)",
    }
    for field, pattern in exact_patterns.items():
        if value := _captured_value(re.search(pattern, text, re.I)):
            values[field] = value

    status = re.search(r"\b(unconfirmed|confirmed)\b", text, re.I)
    if status:
        values["status"] = status.group(1)
    elif re.search(r"\bconfirm\s+(?:assets?|devices?|systems?)\b", text, re.I):
        values["status"] = "confirmed"
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


def _natural_boolean_predicate(
    message: str,
    values: dict[str, object],
) -> AssetPredicate | None:
    """Compile only explicit, bounded OR constructions the grammar can prove."""

    cross_field = re.search(
        r"\brole(?:\s+is|\s*=)?\s+(?P<role>[\w][\w .&/-]*?)\s+or\s+"
        r"suggested\s+type(?:\s+is|\s*=)?\s+(?P<suggested>[\w][\w .&/-]*?)"
        r"(?=\s+(?:and|with|having|where|whose)\b|[?.!,]|$)",
        message,
        re.I,
    )
    if cross_field:
        values.pop("role", None)
        values.pop("suggested_type", None)
        return AssetPredicate(any=(
            AssetPredicate(
                field=AssetPredicateField.ROLE,
                operator=AssetPredicateOperator.EQ,
                value=cross_field.group("role"),
            ),
            AssetPredicate(
                field=AssetPredicateField.SUGGESTED_TYPE,
                operator=AssetPredicateOperator.EQ,
                value=cross_field.group("suggested"),
            ),
        ))

    vendor = re.search(
        r"\b(?:either\s+)?vendor(?:\s+is|\s*=)?\s+(?P<first>[\w.&-]+)\s+or\s+"
        r"(?:vendor(?:\s+is|\s*=)?\s+)?(?P<second>[\w.&-]+)\b",
        message,
        re.I,
    )
    if vendor:
        values.pop("vendor", None)
        return AssetPredicate(
            field=AssetPredicateField.VENDOR,
            operator=AssetPredicateOperator.IN,
            values=(vendor.group("first"), vendor.group("second")),
        )

    role_choice = re.search(
        r"\b(?P<first>database\s+server|domain\s+controller|linux\s+server|"
        r"windows\s+workstation|firewall|siem|splunk\s+indexer|hypervisor)\s+or\s+"
        r"(?P<second>database\s+server|domain\s+controller|linux\s+server|"
        r"windows\s+workstation|firewall|siem|splunk\s+indexer|hypervisor)\b",
        message,
        re.I,
    )
    if role_choice:
        values.pop("role", None)
        return AssetPredicate(
            field=AssetPredicateField.ROLE,
            operator=AssetPredicateOperator.IN,
            values=(role_choice.group("first"), role_choice.group("second")),
        )

    timestamp = re.search(
        r"\b(?:last\s+(?:detection|detected|classified)|classification\s+time|"
        r"detection\s+timestamp|seen\s+by\s+detection)\s+"
        r"(?P<operator>after|before)\s+(?P<value>\d{4}-\d{2}-\d{2}(?:T[^\s,;]+)?)",
        message,
        re.I,
    )
    if timestamp:
        return AssetPredicate(
            field=AssetPredicateField.LAST_DETECTION_AT,
            operator=(
                AssetPredicateOperator.GT
                if timestamp.group("operator").casefold() == "after"
                else AssetPredicateOperator.LT
            ),
            value=timestamp.group("value").rstrip(".,;?!"),
        )
    return None


def _fallback_material_constraints_complete(
    message: str,
    values: dict[str, object],
    group_fields: tuple[AssetGroupField, ...] | None,
) -> bool:
    """Fail closed when material allow-listed selector wording was not parsed."""
    checks: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
        (re.compile(r"\b(?:confirmed|unconfirmed)\b", re.I), ("status",)),
        (re.compile(r"\b(?:made\s+by|manufacturer\s+is|maker\s+is|vendor\s+is)\b", re.I), ("vendor",)),
        (re.compile(r"\bproduct(?:\s+family)?\s+is\b", re.I), ("product",)),
        (re.compile(r"\bmapping\s+(?:confidence|certainty)\b", re.I), ("mapping_confidence_min", "mapping_confidence_max")),
        (re.compile(r"\b(?:model|classification)\s+(?:confidence|certainty)\b", re.I), ("model_confidence_min", "model_confidence_max")),
        (re.compile(r"\bunknown\s+(?:score|probability)\b", re.I), ("unknown_score_min", "unknown_score_max")),
    )
    grouped = {item.value for item in (group_fields or ())}
    return all(
        not pattern.search(message)
        or any(name in values for name in names)
        or any(name.rsplit("_", 1)[0] in grouped for name in names)
        for pattern, names in checks
    )


def _group_fields_from_message(message: str) -> tuple[AssetGroupField, ...] | None:
    match = re.search(
        r"\bgroup\s+(?:(?:all\s+)?assets?|them|those|these)\s+by\s+"
        r"(?P<fields>[^?.!;]+)",
        message,
        re.I,
    )
    if match is None:
        return None
    field_text = re.split(
        r"\s+and\s+(?:show|display|give|list|return)\b|\s+for\s+each\b",
        match.group("fields"),
        maxsplit=1,
        flags=re.I,
    )[0].strip(" ,")
    raw_tokens = re.split(r"\s*(?:,|\band\b)\s*", field_text, flags=re.I)
    fields: list[AssetGroupField] = []
    for raw in raw_tokens:
        token = re.sub(r"\s+", " ", raw.strip().casefold())
        if not token:
            continue
        token = re.sub(r"^(?:their\s+|the\s+)?(?:primary\s+)?", "", token)
        field = _GROUP_FIELD_ALIASES.get(token)
        if field is None:
            return None
        fields.append(field)
    if not 1 <= len(fields) <= 3 or len(set(fields)) != len(fields):
        return None
    return tuple(fields)


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
    if structured_material_gap_reason(text) is not None:
        return None
    reference_scope = structured_reference_scope(text)
    reference = bool(prior_query is not None and reference_scope == "previous")
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
            normalize_structured_query_for_language(
                current,
                text,
                normalize_asset_classes=False,
            ),
            "deterministic_structured_group_membership",
            StructuredResultReferenceDecision(kind="set_query"),
        )

    group_fields = _group_fields_from_message(text)
    values, invalid_score = _natural_filter_values(text)
    if invalid_score:
        return None
    # The exact-selector grammar must not reinterpret the aggregation phrase
    # "by (their primary) role and status" as a role value of "and status".
    if (
        group_fields
        and AssetGroupField.ROLE in group_fields
        and str(values.get("role") or "").casefold().startswith("and ")
    ):
        values.pop("role", None)
    predicate = _natural_boolean_predicate(text, values)
    if predicate is not None:
        values["predicate"] = predicate
    current_class_selector = bool(
        {"suggested_type", "role", "roles", "classification_summary"}
        .intersection(values)
    )
    if not _fallback_material_constraints_complete(text, values, group_fields):
        return None
    try:
        filters = AssetSearchFilters.model_validate(values)
    except ValueError:
        return None

    if group_fields:
        try:
            query = _merge_referenced_query(
                prior_query,
                StructuredQuerySpec(
                    mode=StructuredQueryMode.AGGREGATE,
                    filters=filters,
                    operation=AssetAggregateOperation.GROUP_COUNT,
                    group_by=group_fields[0] if len(group_fields) == 1 else None,
                    group_by_fields=group_fields,
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
            query = normalize_structured_query_for_language(
                query,
                text,
                normalize_asset_classes=not reference or current_class_selector,
            )
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
        sort, direction, limit = AssetSortField.MODEL_CONFIDENCE, SortDirection.DESC, 3
    elif re.search(r"\blowest\s+model\s+confidence\b", text, re.I):
        sort, direction, limit = AssetSortField.MODEL_CONFIDENCE, SortDirection.ASC, 3
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
        query = normalize_structured_query_for_language(
            query,
            text,
            normalize_asset_classes=not reference or current_class_selector,
        )
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
        context = select_structured_query_context(routing_state, message)
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
