"""Compact, allowlisted workflow event logging."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any


SAFE_EVENT_FIELDS = {
    "request_id", "trace_id", "session_id", "plan_id", "step_id", "intent",
    "complexity", "entity_count", "entity_binding_source", "capability", "provider",
    "deployment", "model", "status", "latency_ms", "retry_count", "cache_status",
    "freshness", "completeness", "total_items", "included_items", "omitted_items",
    "truncated", "review_outcome", "supplemental_retrieval_count", "tool_call_count",
    "planner_called", "fallback_used", "error_class", "safe_error_code", "reason",
    "max_concurrency", "max_calls", "max_graph_depth", "runtime", "bounded",
}


@dataclass(frozen=True)
class WorkflowEventContext:
    request_id: str
    trace_id: str
    session_id: str


class WorkflowEventLogger:
    def __init__(self, logger: logging.Logger, context: WorkflowEventContext) -> None:
        self.logger = logger
        self.context = context

    def emit(self, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
        values = {
            "request_id": self.context.request_id,
            "trace_id": self.context.trace_id,
            "session_id": self.context.session_id,
            **{key: value for key, value in fields.items() if key in SAFE_EVENT_FIELDS},
        }
        metadata = " ".join(
            f"{key}={self._clean(value)}"
            for key, value in values.items()
            if value is not None and value != ""
        )
        self.logger.log(level, "event=%s %s", event, metadata)

    @staticmethod
    def _clean(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (tuple, list, set)):
            return ",".join(str(item).replace("\n", " ")[:80] for item in value)
        return str(value).replace("\n", " ")[:160]

