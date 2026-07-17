"""Shared lossless JSON context-provider mechanics for per-asset product data."""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from src.config.settings import Settings
from src.core.context.models import AssetProfileProviderResult, DetectionProviderResult, ProviderProvenance, approx_tokens
from src.core.product_client import ProductAssetResponse
from src.core.product_client.errors import ProductApiError


logger = logging.getLogger(__name__)
ResultT = TypeVar("ResultT", DetectionProviderResult, AssetProfileProviderResult)


@dataclass(frozen=True)
class _CacheEntry:
    response: ProductAssetResponse
    stored_at: float


def _safe_error(exc: Exception) -> str:
    return (str(exc).strip() or type(exc).__name__)[:160]


def _serialize(payload: dict[str, Any] | list[Any] | None) -> str:
    if payload is None:
        return ""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class FullJsonContextProvider:
    """Fetch, cache, and measure complete JSON without interpreting product fields."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider_name: str,
        source: str,
        fetcher: Callable[..., ProductAssetResponse],
        result_type: type[ResultT],
    ) -> None:
        self.settings = settings
        self.provider_name = provider_name
        self.source = source
        self.fetcher = fetcher
        self.result_type = result_type
        # Profile intentionally follows the established product-context cache policy,
        # but keeps an independent provider-local namespace.
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def fetch(self, ip: str, request_id: str, *, session_id: str = "") -> ResultT:
        started = time.perf_counter()
        try:
            normalized_ip = str(ipaddress.ip_address(str(ip).strip()))
        except ValueError:
            return self._unavailable(started, str(ip), "ValueError", "Invalid IP address.")

        logger.info(
            "event=%s_provider_started request_id=%s session_id=%s target_ip=%s cache_enabled=%s",
            self.provider_name,
            request_id,
            session_id,
            normalized_ip,
            self.settings.detection_cache_enabled,
        )
        cached = self._get_cache_entry(normalized_ip)
        miss_reason = self._cache_miss_reason(cached)
        if cached and miss_reason is None:
            age = self._cache_age(cached)
            logger.info(
                "event=%s_cache_hit request_id=%s target_ip=%s cache_age_seconds=%s stale=false",
                self.provider_name,
                request_id,
                normalized_ip,
                age,
            )
            return self._from_response(
                copy.deepcopy(cached.response),
                started=started,
                cache_hit=True,
                cache_age_seconds=age,
            )

        logger.info(
            "event=%s_cache_miss request_id=%s target_ip=%s cache_miss_reason=%s",
            self.provider_name,
            request_id,
            normalized_ip,
            miss_reason,
        )
        try:
            response = self.fetcher(normalized_ip, request_id=request_id)
        except ProductApiError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            if cached and self.settings.detection_stale_on_error:
                age = self._cache_age(cached)
                logger.warning(
                    "event=%s_provider_failed request_id=%s status=available error_type=%s stale_fallback=true latency_ms=%s",
                    self.provider_name,
                    request_id,
                    type(exc).__name__,
                    latency_ms,
                )
                return self._from_response(
                    copy.deepcopy(cached.response),
                    started=started,
                    cache_hit=True,
                    cache_age_seconds=age,
                    cache_miss_reason=miss_reason,
                    stale=True,
                    error_type=type(exc).__name__,
                    safe_error=_safe_error(exc),
                )
            logger.warning(
                "event=%s_provider_failed request_id=%s status=unavailable error_type=%s stale_fallback=false latency_ms=%s",
                self.provider_name,
                request_id,
                type(exc).__name__,
                latency_ms,
            )
            return self._unavailable(started, normalized_ip, type(exc).__name__, _safe_error(exc))

        if self.settings.detection_cache_enabled:
            with self._lock:
                self._cache[normalized_ip] = _CacheEntry(response=copy.deepcopy(response), stored_at=time.time())
        return self._from_response(
            response,
            started=started,
            cache_hit=False,
            cache_age_seconds=0,
            cache_miss_reason=miss_reason,
        )

    def _from_response(
        self,
        response: ProductAssetResponse,
        *,
        started: float,
        cache_hit: bool,
        cache_age_seconds: int,
        cache_miss_reason: str | None = None,
        stale: bool = False,
        error_type: str | None = None,
        safe_error: str | None = None,
    ) -> ResultT:
        payload = copy.deepcopy(response.raw_payload)
        serialized = _serialize(payload)
        status = "not_found" if response.found is False else "available"
        raw_bytes = len(serialized.encode("utf-8"))
        top_level_count = len(payload) if isinstance(payload, (dict, list)) else 0
        fetched_at = datetime.now(timezone.utc).isoformat()
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=%s_provider_completed status=%s target_ip=%s asset_found=%s http_status=%s cache_hit=%s stale=%s provider_latency_ms=%s raw_json_bytes=%s raw_json_chars=%s raw_json_approx_tokens=%s raw_top_level_key_count=%s raw_payload_present=%s full_payload_fetched=%s",
            self.provider_name,
            status,
            response.target_ip,
            response.found,
            response.status_code,
            cache_hit,
            stale,
            latency_ms,
            raw_bytes,
            len(serialized),
            approx_tokens(serialized),
            top_level_count,
            payload is not None,
            payload is not None,
        )
        return self.result_type(
            provider=self.provider_name,
            status=status,
            ip=response.target_ip,
            raw_payload=payload,
            serialized_json=serialized,
            provenance=ProviderProvenance(source=self.source, status=status),
            limitations=[f"{self.provider_name.replace('_', ' ').title()} is product-derived point-in-time evidence."],
            cache_hit=cache_hit,
            cache_age_seconds=cache_age_seconds,
            cache_miss_reason=cache_miss_reason,
            raw_json_bytes=raw_bytes,
            raw_json_chars=len(serialized),
            raw_json_approx_tokens=approx_tokens(serialized),
            raw_top_level_key_count=top_level_count,
            raw_payload_present=payload is not None,
            full_payload_fetched=payload is not None,
            asset_found=response.found,
            http_status=response.status_code,
            fetched_at=fetched_at,
            stale=stale,
            latency_ms=latency_ms,
            error_type=error_type,
            safe_error=safe_error,
        )

    def _unavailable(self, started: float, ip: str, error_type: str, safe_error: str) -> ResultT:
        return self.result_type(
            provider=self.provider_name,
            status="unavailable",
            ip=ip,
            provenance=ProviderProvenance(source=self.source, status="unavailable"),
            limitations=[f"{self.provider_name.replace('_', ' ').title()} was unavailable for this request."],
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_type=error_type,
            safe_error=safe_error,
        )

    def _get_cache_entry(self, ip: str) -> _CacheEntry | None:
        if not self.settings.detection_cache_enabled:
            return None
        with self._lock:
            return self._cache.get(ip)

    def _cache_miss_reason(self, entry: _CacheEntry | None) -> str | None:
        if not self.settings.detection_cache_enabled:
            return "disabled"
        if entry is None:
            return "not_found"
        if self._cache_age(entry) > max(0, self.settings.detection_cache_ttl_seconds):
            return "expired"
        return None

    @staticmethod
    def _cache_age(entry: _CacheEntry) -> int:
        return max(0, int(time.time() - entry.stored_at))
