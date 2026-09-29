"""Request-local LLM usage collection with one outbound product report."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Protocol
from urllib.parse import urlsplit

from src.config.settings import Settings
from src.core.product_client import ProductApiClient


logger = logging.getLogger(__name__)
_CURRENT_COLLECTOR: ContextVar["UsageCollector | None"] = ContextVar("soorin_llm_usage_collector", default=None)
_OUTBOUND_PURPOSES = {
    "intent_router": "router",
    "intent_router_repair": "router",
    "planner": "planner",
    "planner_repair": "planner",
    "investigator": "investigator",
    "chat": "chat",
}


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
    deployment: str
    model: str
    purpose: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    status: str = "success"
    http_status: int | None = None
    finish_reason: str | None = None
    fallback_used: bool | None = None
    occurred_at: str = ""

    @classmethod
    def from_usage(
        cls,
        *,
        request_id: str,
        call_id: str,
        trace_id: str = "",
        provider: str = "",
        deployment: str = "",
        model: str = "",
        purpose: str,
        usage: dict[str, Any] | None,
        latency_ms: int = 0,
        status: str = "success",
        http_status: int | None = None,
        finish_reason: str | None = None,
        fallback_used: bool | None = None,
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
            deployment=deployment,
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_ms=max(0, int(latency_ms or 0)),
            status=status or "success",
            http_status=http_status,
            finish_reason=finish_reason,
            fallback_used=fallback_used,
            occurred_at=_utc_now(),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "trace_id": self.trace_id,
            "purpose": self.purpose,
            "deployment": self.deployment,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "http_status": self.http_status,
            "finish_reason": self.finish_reason,
            "fallback_used": self.fallback_used,
            "occurred_at": self.occurred_at,
        }


class LLMUsageRecorder(Protocol):
    def record(self, call: LLMUsageCall) -> None: ...


@dataclass
class UsageCollector:
    request_id: str
    trace_id: str
    session_id: str = ""
    started_at: str = field(default_factory=_utc_now)
    calls: list[LLMUsageCall] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False)
    _call_ids: set[str] = field(default_factory=set, repr=False)

    def record(self, call: LLMUsageCall) -> None:
        with self._lock:
            if call.call_id in self._call_ids:
                return
            self._call_ids.add(call.call_id)
            self.calls.append(call)

    def snapshot(self) -> list[LLMUsageCall]:
        with self._lock:
            return list(self.calls)

    def clear(self) -> None:
        with self._lock:
            self.calls.clear()
            self._call_ids.clear()


@dataclass
class UsageRequestScope:
    collector: UsageCollector | None
    token: Token[UsageCollector | None] | None
    finished: bool = False
    _lock: Lock = field(default_factory=Lock, repr=False)

    def claim_finish(self) -> bool:
        with self._lock:
            if self.finished:
                return False
            self.finished = True
            return True


class ProductUsageReporter(LLMUsageRecorder):
    """Collect LLM usage per request and report once through the shared product client."""

    def __init__(self, settings: Settings, product_client: ProductApiClient) -> None:
        self.settings = settings
        self.product_client = product_client
        self.reporting_url = settings.llm_usage_reporting_url.strip()
        self.enabled = bool(settings.llm_usage_reporting_enabled and self.reporting_url)
        if settings.llm_usage_reporting_enabled and not self.reporting_url:
            logger.warning("event=usage_reporting_disabled reason=missing_url")

    def start_request(
        self,
        request_id: str,
        trace_id: str,
        session_id: str = "",
    ) -> UsageRequestScope:
        if not self.enabled:
            return UsageRequestScope(None, None)
        collector = UsageCollector(
            request_id=request_id,
            trace_id=trace_id,
            session_id=session_id,
        )
        return UsageRequestScope(collector, _CURRENT_COLLECTOR.set(collector))

    def record(self, call: LLMUsageCall) -> None:
        collector = _CURRENT_COLLECTOR.get()
        if collector is None:
            return
        collector.record(call)

    def finish_request(self, scope: UsageRequestScope, *, request_success: bool) -> None:
        if not scope.claim_finish():
            return
        collector = scope.collector
        if scope.token is not None:
            _CURRENT_COLLECTOR.reset(scope.token)
        if collector is None:
            return
        try:
            calls = collector.snapshot()
            payload = self._payload_for_request(calls)
            self._send_payload(
                payload,
                request_id=collector.request_id,
                trace_id=collector.trace_id,
                request_success=request_success,
                call_count=len(calls),
            )
        finally:
            collector.clear()

    @staticmethod
    def _payload_for_request(calls: list[LLMUsageCall]) -> dict[str, Any]:
        aggregated: dict[tuple[str, str], dict[str, Any]] = {}
        for call in calls:
            purpose = _OUTBOUND_PURPOSES.get(call.purpose)
            if purpose is None:
                continue
            key = (purpose, call.model)
            entry = aggregated.setdefault(
                key,
                {
                    "purpose": purpose,
                    "model": call.model,
                    "inputTokens": 0,
                    "outputTokens": 0,
                },
            )
            entry["inputTokens"] += call.input_tokens
            entry["outputTokens"] += call.output_tokens

        models = list(aggregated.values())
        return {
            "inputTokens": sum(entry["inputTokens"] for entry in models),
            "outputTokens": sum(entry["outputTokens"] for entry in models),
            "models": models,
        }

    def _send_payload(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
        trace_id: str,
        request_success: bool,
        call_count: int,
    ) -> None:
        try:
            self.product_client.post_json(
                self.reporting_url,
                payload,
                request_id=request_id,
                idempotency_key=request_id,
                operation="usage_report",
            )
        except Exception as exc:
            logger.warning(
                "event=usage_report_failed request_id=%s trace_id=%s status=%s call_count=%s error_type=%s",
                request_id,
                trace_id,
                "success" if request_success else "partial",
                call_count,
                type(exc).__name__ or "Exception",
            )
            return
        logger.info(
            "event=usage_report_sent request_id=%s trace_id=%s status=%s call_count=%s input_tokens=%s output_tokens=%s total_tokens=%s host=%s",
            request_id,
            trace_id,
            "success" if request_success else "partial",
            call_count,
            payload.get("inputTokens"),
            payload.get("outputTokens"),
            payload.get("inputTokens", 0) + payload.get("outputTokens", 0),
            urlsplit(self.reporting_url).hostname or "",
        )
