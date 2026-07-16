"""Asset-detection context provider with a small in-memory TTL cache."""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config.settings import Settings
from src.core.context.models import DetectionDetail, DetectionProviderResult, ProviderProvenance, approx_tokens
from src.core.detection import adapt_asset_detection, compact_full, summary
from src.core.detection.models import AssetDetectionEvidence
from src.core.product_client import ProductApiClient
from src.core.product_client.errors import ProductApiError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CacheEntry:
    evidence: AssetDetectionEvidence
    stored_at: float
    detail: DetectionDetail


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:160]


class DetectionContextProvider:
    """Fetch bounded product asset-detection evidence independently of graph."""

    def __init__(self, settings: Settings, product_client: ProductApiClient | None = None) -> None:
        self.settings = settings
        self.product_client = product_client or ProductApiClient(settings)
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def fetch(
        self,
        ip: str,
        detail: DetectionDetail,
        request_id: str,
        *,
        session_id: str = "",
    ) -> DetectionProviderResult:
        started = time.perf_counter()
        detail = detail if detail in {"summary", "compact_full"} else "summary"
        try:
            normalized_ip = str(ipaddress.ip_address(str(ip).strip()))
        except ValueError:
            return self._unavailable(started, detail, str(ip), "ValueError", "Invalid IP address.")

        logger.info(
            "event=detection_provider_started request_id=%s session_id=%s ip=%s detail=%s cache_enabled=%s",
            request_id,
            session_id,
            normalized_ip,
            detail,
            self.settings.detection_cache_enabled,
        )

        cached = self._get_cache_entry(normalized_ip)
        cache_miss_reason = self._cache_miss_reason(cached, detail)
        if cached and cache_miss_reason is None:
            age = self._cache_age(cached)
            rendered = self._render(cached.evidence, detail)
            status = "not_found" if cached.evidence.found is False else "available"
            latency_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "event=detection_cache_hit request_id=%s ip=%s cache_age_seconds=%s cached_detail=%s requested_detail=%s stale=false",
                request_id,
                normalized_ip,
                age,
                cached.detail,
                detail,
            )
            return self._result(
                status=status,
                detail=detail,
                ip=normalized_ip,
                evidence=cached.evidence,
                rendered_context=rendered,
                cache_hit=True,
                cache_age_seconds=age,
                cached_detail=cached.detail,
                stale=False,
                latency_ms=latency_ms,
            )

        logger.info(
            "event=detection_cache_miss request_id=%s ip=%s cache_miss_reason=%s cached_detail=%s requested_detail=%s",
            request_id,
            normalized_ip,
            cache_miss_reason,
            cached.detail if cached else "",
            detail,
        )

        try:
            raw = self.product_client.get_asset_detection(normalized_ip, request_id=request_id)
            evidence = adapt_asset_detection(raw, fetched_at=datetime.now(timezone.utc))
        except ProductApiError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            if cached and self.settings.detection_stale_on_error and self._detail_satisfies(cached.detail, detail):
                age = self._cache_age(cached)
                rendered = self._render(cached.evidence, detail)
                logger.warning(
                    "event=detection_provider_failed request_id=%s status=available error_type=%s stale_fallback=true latency_ms=%s",
                    request_id,
                    type(exc).__name__,
                    latency_ms,
                )
                return self._result(
                    status="available",
                    detail=detail,
                    ip=normalized_ip,
                    evidence=cached.evidence,
                    rendered_context=rendered,
                    cache_hit=True,
                    cache_age_seconds=age,
                    cache_miss_reason=cache_miss_reason,
                    cached_detail=cached.detail,
                    stale=True,
                    latency_ms=latency_ms,
                    error_type=type(exc).__name__,
                    safe_error=_safe_error(exc),
                )
            logger.warning(
                "event=detection_provider_failed request_id=%s status=unavailable error_type=%s stale_fallback=false latency_ms=%s",
                request_id,
                type(exc).__name__,
                latency_ms,
            )
            return self._unavailable(started, detail, normalized_ip, type(exc).__name__, _safe_error(exc))

        if self.settings.detection_cache_enabled:
            with self._lock:
                self._cache[normalized_ip] = _CacheEntry(evidence=evidence, stored_at=time.time(), detail=detail)

        rendered = self._render(evidence, detail)
        status = "not_found" if evidence.found is False else "available"
        latency_ms = int((time.perf_counter() - started) * 1000)
        if status == "not_found":
            logger.info(
                "event=detection_not_found request_id=%s session_id=%s ip=%s source=product_asset_detection latency_ms=%s",
                request_id,
                session_id,
                normalized_ip,
                latency_ms,
            )
        logger.info(
            "event=detection_provider_completed request_id=%s status=%s asset_found=%s detail=%s matched_rules=%s conflicts=%s context_chars=%s context_approx_tokens=%s latency_ms=%s",
            request_id,
            status,
            evidence.found,
            detail,
            len(evidence.matched_rules),
            len(evidence.conflicts),
            len(rendered),
            approx_tokens(rendered),
            latency_ms,
        )
        return self._result(
            status=status,
            detail=detail,
            ip=normalized_ip,
            evidence=evidence,
            rendered_context=rendered,
            limitations=evidence.limitations,
            cache_hit=False,
            cache_age_seconds=0,
            cache_miss_reason=cache_miss_reason,
            cached_detail=cached.detail if cached else None,
            stale=False,
            latency_ms=latency_ms,
        )

    def _get_cache_entry(self, ip: str) -> _CacheEntry | None:
        if not self.settings.detection_cache_enabled:
            return None
        with self._lock:
            return self._cache.get(ip)

    def _is_fresh(self, entry: _CacheEntry) -> bool:
        return self._cache_age(entry) <= max(0, self.settings.detection_cache_ttl_seconds)

    @staticmethod
    def _detail_satisfies(cached_detail: DetectionDetail, requested_detail: DetectionDetail) -> bool:
        return cached_detail == "compact_full" or requested_detail == "summary"

    def _cache_miss_reason(self, entry: _CacheEntry | None, requested_detail: DetectionDetail) -> str | None:
        if not self.settings.detection_cache_enabled:
            return "disabled"
        if entry is None:
            return "not_found"
        if not self._is_fresh(entry):
            return "expired"
        if not self._detail_satisfies(entry.detail, requested_detail):
            return "detail_mismatch"
        return None

    @staticmethod
    def _cache_age(entry: _CacheEntry) -> int:
        return max(0, int(time.time() - entry.stored_at))

    @staticmethod
    def _render(evidence: AssetDetectionEvidence, detail: DetectionDetail) -> str:
        if detail == "compact_full":
            return compact_full(evidence)
        return "[SOORIN ASSET DETECTION EVIDENCE]\nDetail: summary\n" + summary(evidence)

    def _unavailable(
        self,
        started: float,
        detail: DetectionDetail,
        ip: str,
        error_type: str,
        safe_error: str,
    ) -> DetectionProviderResult:
        return self._result(
            status="unavailable",
            detail=detail,
            ip=ip,
            limitations=["Detection evidence was unavailable for this request."],
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_type=error_type,
            safe_error=safe_error,
        )

    @staticmethod
    def _result(
        *,
        status: str,
        detail: DetectionDetail,
        ip: str,
        evidence: AssetDetectionEvidence | None = None,
        rendered_context: str = "",
        limitations: list[str] | None = None,
        cache_hit: bool = False,
        cache_age_seconds: int | None = None,
        cache_miss_reason: str | None = None,
        cached_detail: DetectionDetail | None = None,
        stale: bool = False,
        latency_ms: int = 0,
        error_type: str | None = None,
        safe_error: str | None = None,
    ) -> DetectionProviderResult:
        return DetectionProviderResult(
            provider="detection",
            status=status,  # type: ignore[arg-type]
            detail=detail,
            ip=ip,
            evidence=evidence,
            rendered_context=rendered_context,
            provenance=ProviderProvenance(source="product_asset_detection", status=status),  # type: ignore[arg-type]
            limitations=list(limitations or (evidence.limitations if evidence else [])),
            cache_hit=cache_hit,
            cache_age_seconds=cache_age_seconds,
            cache_miss_reason=cache_miss_reason,
            cached_detail=cached_detail,
            context_truncated=rendered_context.endswith("...[truncated]"),
            context_truncation_reason=("detection_context_character_limit" if rendered_context.endswith("...[truncated]") else None),
            stale=stale,
            latency_ms=latency_ms,
            error_type=error_type,
            safe_error=safe_error,
        )
