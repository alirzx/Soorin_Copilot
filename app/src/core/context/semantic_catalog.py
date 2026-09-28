"""Bounded, cached semantic vocabulary from the active graph projection."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from src.core.graph.structured import (
    AssetPredicate,
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
        """Canonicalize matching categorical values without changing zero-matches."""
        try:
            version = self.graph_service.semantic_catalog_version()
            if not version:
                return query
            requested = _query_categorical_values(query)
            if not requested:
                return query
            resolved = self.graph_service.canonicalize_semantic_values(version, requested)
            if not resolved:
                logger.info(
                    "event=semantic_canonicalization request_id=%s active_graph_version=%s "
                    "requested_value_count=%s resolved_value_count=0 status=zero_match",
                    request_id,
                    version,
                    sum(len(values) for values in requested.values()),
                )
                return query
            canonical = _apply_canonical_values(query, resolved)
            logger.info(
                "event=semantic_canonicalization request_id=%s active_graph_version=%s "
                "requested_value_count=%s resolved_value_count=%s status=resolved",
                request_id,
                version,
                sum(len(values) for values in requested.values()),
                sum(len(values) for values in resolved.values()),
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
