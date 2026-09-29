"""Bounded, cached semantic vocabulary from the active graph projection."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from src.core.graph.structured import (
    AssetPredicate,
    AssetPredicateField,
    AssetPredicateOperator,
    AssetSearchFilters,
    StructuredQuerySpec,
)


logger = logging.getLogger(__name__)

SEMANTIC_CATALOG_FIELDS = (
    "suggested_type",
    "role",
    "roles",
    "vendor",
    "product",
    "tag",
    "sub_tag",
    "status",
    "enrichment_status",
)

_CLASS_CATALOG_FIELDS = frozenset({"suggested_type", "role", "roles"})
_CLASS_NOISE_TOKENS = frozenset({
    "asset",
    "assets",
    "device",
    "devices",
    "system",
    "systems",
    "machine",
    "machines",
    "host",
    "hosts",
})
_MAX_CLASS_EXPANSION_LEAVES = 18
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


class SemanticCatalogProvider:
    """Expose graph vocabulary without making it an input-language allow-list."""

    def __init__(
        self,
        graph_service: Any,
        *,
        ttl_seconds: float = 600.0,
        unavailable_ttl_seconds: float = 30.0,
        per_field_limit: int = 24,
        product_limit: int = 16,
        total_value_limit: int = 128,
        max_value_chars: int = 96,
        total_value_chars: int = 6000,
    ) -> None:
        self.graph_service = graph_service
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.unavailable_ttl_seconds = max(0.0, float(unavailable_ttl_seconds))
        self.per_field_limit = max(1, min(int(per_field_limit), 64))
        self.product_limit = max(1, min(int(product_limit), self.per_field_limit))
        self.total_value_limit = max(1, min(int(total_value_limit), 256))
        self.max_value_chars = max(16, min(int(max_value_chars), 160))
        self.total_value_chars = max(256, min(int(total_value_chars), 12000))
        self._lock = threading.RLock()
        self._cached_version: str | None = None
        self._cached_at = 0.0
        self._cached_payload: dict[str, Any] | None = None
        self._unavailable_at = 0.0

    def get(self, *, request_id: str = "") -> dict[str, Any]:
        started = time.perf_counter()
        cache_state = "miss"
        version_changed = False
        try:
            with self._lock:
                now = time.monotonic()
                if (
                    self._unavailable_at
                    and now - self._unavailable_at < self.unavailable_ttl_seconds
                ):
                    logger.info(
                        "event=semantic_catalog request_id=%s available=false cache=hit "
                        "version_changed=false field_count=0 total_value_count=0 latency_ms=%s",
                        request_id,
                        int((time.perf_counter() - started) * 1000),
                    )
                    return {"available": False}
                version = self.graph_service.semantic_catalog_version()
                if not version:
                    raise RuntimeError("active_graph_projection_unavailable")
                version_changed = bool(
                    self._cached_version is not None and self._cached_version != version
                )
                if (
                    self._cached_payload is not None
                    and self._cached_version == version
                    and now - self._cached_at < self.ttl_seconds
                ):
                    cache_state = "hit"
                    payload = self._cached_payload
                else:
                    raw = self.graph_service.semantic_catalog_values(
                        version,
                        per_field_limit=self.per_field_limit,
                    )
                    fields = self._bounded_fields(raw)
                    payload = {
                        "available": True,
                        "active_graph_version": version,
                        "fields": fields,
                    }
                    self._cached_version = version
                    self._cached_at = now
                    self._cached_payload = payload
                self._unavailable_at = 0.0
                total = sum(len(values) for values in payload["fields"].values())
                logger.info(
                    "event=semantic_catalog request_id=%s available=true cache=%s "
                    "version_changed=%s field_count=%s total_value_count=%s latency_ms=%s",
                    request_id,
                    cache_state,
                    str(version_changed).lower(),
                    sum(bool(values) for values in payload["fields"].values()),
                    total,
                    int((time.perf_counter() - started) * 1000),
                )
                return payload
        except Exception as exc:
            with self._lock:
                self._unavailable_at = time.monotonic()
            logger.warning(
                "event=semantic_catalog request_id=%s available=false cache=%s "
                "version_changed=%s field_count=0 total_value_count=0 latency_ms=%s error_type=%s",
                request_id,
                cache_state,
                str(version_changed).lower(),
                int((time.perf_counter() - started) * 1000),
                type(exc).__name__,
            )
            return {"available": False}

    def _bounded_fields(self, raw: Any) -> dict[str, list[str]]:
        source = raw if isinstance(raw, dict) else {}
        candidates_by_field: dict[str, list[str]] = {}
        for field in SEMANTIC_CATALOG_FIELDS:
            limit = self.product_limit if field == "product" else self.per_field_limit
            values: list[str] = []
            seen: set[str] = set()
            candidates = source.get(field, ())
            if not isinstance(candidates, (list, tuple)):
                candidates = ()
            for candidate in candidates:
                value = str(candidate).strip() if candidate is not None else ""
                folded = value.casefold()
                if not value or len(value) > self.max_value_chars or folded in seen:
                    continue
                seen.add(folded)
                values.append(value)
                if len(values) >= limit:
                    break
            candidates_by_field[field] = values

        # Allocate one value per field per pass so early high-cardinality fields
        # cannot starve later fields under the global count/character bounds.
        bounded = {field: [] for field in SEMANTIC_CATALOG_FIELDS}
        remaining = self.total_value_limit
        remaining_chars = self.total_value_chars
        positions = {field: 0 for field in SEMANTIC_CATALOG_FIELDS}
        while remaining > 0 and remaining_chars > 0:
            added = False
            advanced = False
            for field in SEMANTIC_CATALOG_FIELDS:
                candidates = candidates_by_field[field]
                while positions[field] < len(candidates):
                    value = candidates[positions[field]]
                    positions[field] += 1
                    advanced = True
                    if len(value) > remaining_chars:
                        continue
                    bounded[field].append(value)
                    remaining -= 1
                    remaining_chars -= len(value)
                    added = True
                    break
                if remaining <= 0 or remaining_chars <= 0:
                    break
            if not added and not advanced:
                break
        return bounded

    def canonicalize_query(
        self,
        query: StructuredQuerySpec,
        *,
        request_id: str = "",
    ) -> StructuredQuerySpec:
        """Ground Router categorical values in the active graph vocabulary.

        Execution remains exact.  First, case-insensitive exact values are
        canonicalized through Neo4j.  If a *generic asset-class* concept has no
        exact class match, a bounded lexical concept expansion may replace it
        only with canonical class/function values present in the current
        catalog.  This supports broad concepts such as ``Linux`` -> ``Linux
        Server`` and ``Windows`` -> all current Windows class variants without a
        static environment-specific alias table or fuzzy Cypher.
        """
        try:
            version = self.graph_service.semantic_catalog_version()
            if not version:
                return query
            requested = _query_categorical_values(query)
            if not requested:
                return query
            resolved = self.graph_service.canonicalize_semantic_values(version, requested)
            canonical = _apply_canonical_values(query, resolved) if resolved else query

            class_exact = _semantic_class_exactly_resolved(query, resolved)
            expanded_pairs: tuple[tuple[str, str], ...] = ()
            if (
                query.semantic_class
                and query.class_mapping_mode == "generic_asset_class"
                and not class_exact
            ):
                catalog = self.get(request_id=request_id)
                expanded_pairs = _catalog_class_expansion(query, catalog)
                if expanded_pairs:
                    canonical = _apply_class_expansion(canonical, expanded_pairs)
                    logger.info(
                        "event=semantic_class_expanded request_id=%s active_graph_version=%s "
                        "semantic_class=%s canonical_value_count=%s leaf_count=%s source=active_catalog",
                        request_id,
                        version,
                        query.semantic_class,
                        len({value.casefold() for _, value in expanded_pairs}),
                        len(expanded_pairs),
                    )

            resolved_count = sum(len(values) for values in resolved.values())
            status = "expanded" if expanded_pairs else "resolved" if resolved else "zero_match"
            logger.info(
                "event=semantic_canonicalization request_id=%s active_graph_version=%s "
                "requested_value_count=%s resolved_value_count=%s status=%s",
                request_id,
                version,
                sum(len(values) for values in requested.values()),
                resolved_count,
                status,
            )
            return canonical
        except Exception as exc:
            logger.warning(
                "event=semantic_canonicalization_failed request_id=%s error_type=%s",
                request_id,
                type(exc).__name__,
            )
            return query


_CANONICAL_FIELDS = frozenset(SEMANTIC_CATALOG_FIELDS)


def _query_categorical_values(query: StructuredQuerySpec) -> dict[str, tuple[str, ...]]:
    collected: dict[str, list[str]] = {}
    raw = query.filters.model_dump(mode="python")
    for field in _CANONICAL_FIELDS:
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            collected.setdefault(field, []).append(value.strip())

    def visit(predicate: AssetPredicate | None) -> None:
        if predicate is None:
            return
        for child in (*predicate.all, *predicate.any):
            visit(child)
        visit(predicate.not_)
        if predicate.field is None or predicate.field.value not in _CANONICAL_FIELDS:
            return
        values = predicate.values or (
            (predicate.value,) if predicate.value is not None else ()
        )
        collected.setdefault(predicate.field.value, []).extend(
            str(value).strip() for value in values if str(value).strip()
        )

    visit(query.filters.predicate)
    return {
        field: tuple(dict.fromkeys(values))
        for field, values in collected.items()
    }


def _apply_canonical_values(
    query: StructuredQuerySpec,
    resolved: dict[str, dict[str, str]],
) -> StructuredQuerySpec:
    def canonical(field: str, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return resolved.get(field, {}).get(value.strip().casefold(), value)

    raw_filters = query.filters.model_dump(mode="python", by_alias=True)
    for field in _CANONICAL_FIELDS:
        if raw_filters.get(field) is not None:
            raw_filters[field] = canonical(field, raw_filters[field])

    def rewrite(raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        updated = dict(raw)
        field = str(updated.get("field") or "")
        if field in _CANONICAL_FIELDS:
            if updated.get("value") is not None:
                updated["value"] = canonical(field, updated["value"])
            if updated.get("values"):
                updated["values"] = [canonical(field, value) for value in updated["values"]]
        for name in ("all", "any"):
            if updated.get(name):
                updated[name] = [rewrite(item) for item in updated[name]]
        if updated.get("not") is not None:
            updated["not"] = rewrite(updated["not"])
        return updated

    predicate = raw_filters.get("predicate")
    if isinstance(predicate, dict):
        raw_filters["predicate"] = rewrite(predicate)
    filters = AssetSearchFilters.model_validate(raw_filters)
    return query.model_copy(update={"filters": filters})


def _semantic_class_exactly_resolved(
    query: StructuredQuerySpec,
    resolved: dict[str, dict[str, str]],
) -> bool:
    semantic_class = (query.semantic_class or "").strip().casefold()
    if not semantic_class:
        return False
    fields = {
        field.value
        for field in query.class_selector_fields
        if field.value in _CLASS_CATALOG_FIELDS
    }
    return any(semantic_class in resolved.get(field, {}) for field in fields)


def _token_key(token: str) -> str:
    value = token.casefold()
    if len(value) > 4 and value.endswith("ies"):
        return value[:-3] + "y"
    if len(value) > 3 and value.endswith("s") and not value.endswith("ss"):
        return value[:-1]
    return value


def _semantic_tokens(value: str) -> tuple[str, ...]:
    return tuple(_token_key(token) for token in _TOKEN_RE.findall(value.casefold()))


def _catalog_class_expansion(
    query: StructuredQuerySpec,
    catalog: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    if not catalog.get("available") or not query.semantic_class:
        return ()
    raw_fields = catalog.get("fields")
    if not isinstance(raw_fields, dict):
        return ()

    selector_fields = tuple(
        field.value
        for field in query.class_selector_fields
        if field.value in _CLASS_CATALOG_FIELDS
    )
    if not selector_fields:
        return ()

    concept_tokens = tuple(
        token
        for token in _semantic_tokens(query.semantic_class)
        if token not in {_token_key(item) for item in _CLASS_NOISE_TOKENS}
    )
    if not concept_tokens:
        return ()
    concept = set(concept_tokens)

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field in selector_fields:
        candidates = raw_fields.get(field, ())
        if not isinstance(candidates, (list, tuple)):
            continue
        for candidate in candidates:
            canonical = str(candidate).strip()
            if not canonical:
                continue
            tokens = _semantic_tokens(canonical)
            token_set = set(tokens)
            initials = "".join(token[0] for token in tokens if token)
            matches_concept = concept.issubset(token_set)
            matches_initialism = len(concept_tokens) == 1 and concept_tokens[0] == initials
            if not (matches_concept or matches_initialism):
                continue
            key = (field, canonical.casefold())
            if key in seen:
                continue
            seen.add(key)
            pairs.append((field, canonical))
            if len(pairs) >= _MAX_CLASS_EXPANSION_LEAVES:
                return tuple(pairs)
    return tuple(pairs)


def _class_leaf(field: str, value: str) -> AssetPredicate:
    predicate_field = AssetPredicateField(field)
    return AssetPredicate(
        field=predicate_field,
        operator=(
            AssetPredicateOperator.MEMBER_EQ
            if predicate_field is AssetPredicateField.ROLES
            else AssetPredicateOperator.EQ
        ),
        value=value,
    )


def _class_only_predicate(
    predicate: AssetPredicate,
    semantic_class: str,
    selector_fields: frozenset[str],
) -> bool:
    if predicate.all or predicate.any:
        children = predicate.all or predicate.any
        return bool(children) and all(
            _class_only_predicate(child, semantic_class, selector_fields)
            for child in children
        )
    if predicate.not_ is not None or predicate.field is None:
        return False
    if predicate.field.value not in selector_fields:
        return False
    values = predicate.values or (
        (predicate.value,) if predicate.value is not None else ()
    )
    return bool(values) and all(
        str(value).strip().casefold() == semantic_class
        for value in values
    )


def _replace_class_predicate(
    predicate: AssetPredicate,
    semantic_class: str,
    selector_fields: frozenset[str],
    replacement: AssetPredicate,
) -> AssetPredicate:
    if _class_only_predicate(predicate, semantic_class, selector_fields):
        return replacement
    if predicate.all:
        return AssetPredicate(all=tuple(
            _replace_class_predicate(child, semantic_class, selector_fields, replacement)
            for child in predicate.all
        ))
    if predicate.any:
        return AssetPredicate(any=tuple(
            _replace_class_predicate(child, semantic_class, selector_fields, replacement)
            for child in predicate.any
        ))
    if predicate.not_ is not None:
        return AssetPredicate.model_validate({
            "not": _replace_class_predicate(
                predicate.not_, semantic_class, selector_fields, replacement
            )
        })
    return predicate


def _apply_class_expansion(
    query: StructuredQuerySpec,
    pairs: tuple[tuple[str, str], ...],
) -> StructuredQuerySpec:
    semantic_class = (query.semantic_class or "").strip().casefold()
    selector_fields = frozenset(
        field.value
        for field in query.class_selector_fields
        if field.value in _CLASS_CATALOG_FIELDS
    )
    leaves = tuple(_class_leaf(field, value) for field, value in pairs)
    if not leaves or not semantic_class or not selector_fields:
        return query
    replacement = leaves[0] if len(leaves) == 1 else AssetPredicate(any=leaves)

    raw_filters = query.filters.model_dump(mode="python", by_alias=True)
    for field in selector_fields:
        value = raw_filters.get(field)
        if isinstance(value, str) and value.strip().casefold() == semantic_class:
            raw_filters[field] = None

    predicate = query.filters.predicate
    if predicate is None:
        predicate = replacement
    else:
        predicate = _replace_class_predicate(
            predicate,
            semantic_class,
            selector_fields,
            replacement,
        )
    raw_filters["predicate"] = predicate.model_dump(mode="python", by_alias=True)
    filters = AssetSearchFilters.model_validate(raw_filters)
    return query.model_copy(
        update={
            "filters": filters,
            "class_mapping_mode": "generic_asset_class_catalog_expansion",
        }
    )
