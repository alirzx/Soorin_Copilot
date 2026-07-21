"""Request-local LLM usage collection with one outbound product report."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Protocol
from urllib.parse import urlsplit

from src.config.settings import Settings
from src.core.product_client import ProductApiClient
from src.core.product_client.errors import ProductApiError


logger = logging.getLogger(__name__)
_CURRENT_COLLECTOR: ContextVar["UsageCollector | None"] = ContextVar("soorin_llm_usage_collector", default=None)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


@dataclass(frozen=True)
class LLMUsageCall:
    request_id: str
    call_id: str
    trace_id: str
    provider: str
    model: str
    purpose: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    status: str = "success"
    occurred_at: str = ""

    @classmethod
    def from_usage(
        cls,
        *,
        request_id: str,
        call_id: str,
        trace_id: str = "",
        provider: str = "",
        model: str = "",
        purpose: str,
        usage: dict[str, Any] | None,
        latency_ms: int = 0,
        status: str = "success",
    ) -> "LLMUsageCall":
        usage = usage or {}
        input_tokens = _as_int(usage.get("input_tokens") or usage.get("prompt_tokens"))
        output_tokens = _as_int(usage.get("output_tokens") or usage.get("completion_tokens"))
        total_tokens = _as_int(usage.get("total_tokens")) or input_tokens + output_tokens
        return cls(
            request_id=request_id,
            call_id=call_id,
            trace_id=trace_id,
            provider=provider,
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_ms=max(0, int(latency_ms or 0)),
            status=status or "success",
            occurred_at=_utc_now(),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "status": self.status,
        }


class LLMUsageRecorder(Protocol):
    def record(self, call: LLMUsageCall) -> None: ...


@dataclass
class UsageCollector:
    request_id: str
    trace_id: str
    started_at: str = field(default_factory=_utc_now)
    calls: list[LLMUsageCall] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record(self, call: LLMUsageCall) -> None:
        with self._lock:
            self.calls.append(call)

    def snapshot(self) -> list[LLMUsageCall]:
        with self._lock:
            return list(self.calls)

    def clear(self) -> None:
        with self._lock:
            self.calls.clear()


@dataclass(frozen=True)
class UsageRequestScope:
    collector: UsageCollector | None
    token: Token[UsageCollector | None] | None


class ProductUsageReporter(LLMUsageRecorder):
    """Collect LLM usage per request and report once through the shared product client."""

    def __init__(self, settings: Settings, product_client: ProductApiClient) -> None:
        self.settings = settings
        self.product_client = product_client
        self.reporting_url = settings.llm_usage_reporting_url.strip()
        self.enabled = bool(settings.llm_usage_reporting_enabled and self.reporting_url)
        if settings.llm_usage_reporting_enabled and not self.reporting_url:
            logger.warning("event=usage_reporting_disabled reason=missing_url")

    def start_request(self, request_id: str, trace_id: str) -> UsageRequestScope:
        if not self.enabled:
            return UsageRequestScope(None, None)
        collector = UsageCollector(request_id=request_id, trace_id=trace_id)
        return UsageRequestScope(collector, _CURRENT_COLLECTOR.set(collector))

    def record(self, call: LLMUsageCall) -> None:
        collector = _CURRENT_COLLECTOR.get()
        if collector is None:
            return
        collector.record(call)

    def finish_request(self, scope: UsageRequestScope, *, request_success: bool) -> None:
        collector = scope.collector
        if scope.token is not None:
            _CURRENT_COLLECTOR.reset(scope.token)
        if collector is None:
            return
        try:
            calls = collector.snapshot()
            if not calls:
                logger.info(
                    "event=usage_report_skipped request_id=%s trace_id=%s reason=no_llm_calls",
                    collector.request_id,
                    collector.trace_id,
                )
                return

            payload = self._payload_for_request(collector, calls, request_success=request_success)
            self._send_payload(payload)
        finally:
            collector.clear()

    def _payload_for_request(
        self,
        collector: UsageCollector,
        calls: list[LLMUsageCall],
        *,
        request_success: bool,
    ) -> dict[str, Any]:
        completed_at = _utc_now()
        all_success = bool(calls) and all(call.status == "success" for call in calls)
        status = "success" if request_success and all_success else "partial"
        return {
            "request_id": collector.request_id,
            "trace_id": collector.trace_id,
            "status": status,
            "started_at": collector.started_at,
            "completed_at": completed_at,
            "input_tokens": sum(call.input_tokens for call in calls),
            "output_tokens": sum(call.output_tokens for call in calls),
            "total_tokens": sum(call.total_tokens for call in calls),
            "call_count": len(calls),
            "calls": [call.to_payload() for call in calls],
        }

    def _send_payload(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("request_id") or "")
        trace_id = str(payload.get("trace_id") or "")
        attempts = 2
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self.product_client.post_json(
                    self.reporting_url,
                    payload,
                    request_id=request_id,
                    idempotency_key=request_id,
                )
                logger.info(
                    "event=usage_report_sent request_id=%s trace_id=%s status=%s call_count=%s input_tokens=%s output_tokens=%s total_tokens=%s host=%s attempt=%s",
                    request_id,
                    trace_id,
                    payload.get("status"),
                    payload.get("call_count"),
                    payload.get("input_tokens"),
                    payload.get("output_tokens"),
                    payload.get("total_tokens"),
                    urlsplit(self.reporting_url).hostname or "",
                    attempt,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = min(max(0.0, float(self.settings.product_retry_backoff_seconds)), 1.0)
                logger.warning(
                    "event=usage_report_retry_scheduled request_id=%s trace_id=%s attempt=%s next_attempt=%s error_type=%s delay_seconds=%.3f",
                    request_id,
                    trace_id,
                    attempt,
                    attempt + 1,
                    type(exc).__name__,
                    delay,
                )
                if delay:
                    time.sleep(delay)

        logger.warning(
            "event=usage_report_failed request_id=%s trace_id=%s status=%s call_count=%s error_type=%s",
            request_id,
            trace_id,
            payload.get("status"),
            payload.get("call_count"),
            type(last_error).__name__ if last_error else ProductApiError.__name__,
        )
