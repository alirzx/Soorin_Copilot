"""Bounded, cached semantic vocabulary from the active graph projection."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any


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
        remaining = self.total_value_limit
        remaining_chars = self.total_value_chars
        bounded: dict[str, list[str]] = {}
        for field in SEMANTIC_CATALOG_FIELDS:
            limit = self.product_limit if field == "product" else self.per_field_limit
            values: list[str] = []
            seen: set[str] = set()
            candidates = source.get(field, ())
            if not isinstance(candidates, (list, tuple)):
                candidates = ()
            for candidate in candidates:
                value = (
                    str(candidate).strip()[: self.max_value_chars]
                    if candidate is not None
                    else ""
                )
                folded = value.casefold()
                if not value or folded in seen:
                    continue
                if len(value) > remaining_chars:
                    break
                seen.add(folded)
                values.append(value)
                remaining_chars -= len(value)
                if len(values) >= limit or len(values) >= remaining:
                    break
            bounded[field] = values
            remaining -= len(values)
            if remaining <= 0 or remaining_chars <= 0:
                for trailing in SEMANTIC_CATALOG_FIELDS[len(bounded) :]:
                    bounded[trailing] = []
                break
        return bounded
