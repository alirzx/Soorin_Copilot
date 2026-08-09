"""Bounded dependency-aware executor for registered read-only capabilities."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from src.core.agent.contracts import ExecutionPlan, PlanStep, ToolResult
from src.core.agent.context_identity import identity_for_tool_result
from src.core.agent.events import WorkflowEventLogger
from src.core.agent.registry import CapabilityRegistry
from src.core.observability.metrics import get_metrics


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CapabilityExecutor:
    """Execute one validated DAG under strict call, time, and concurrency limits."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        max_concurrency: int = 4,
        max_calls: int = 6,
        total_timeout_seconds: float = 120.0,
    ) -> None:
        self.registry = registry
        self.max_concurrency = max(1, min(8, max_concurrency))
        self.max_calls = max(1, max_calls)
        self.total_timeout_seconds = max(0.1, total_timeout_seconds)
        self._group_locks: dict[str, threading.Lock] = {"product": threading.Lock()}
        logger.info(
            "event=capability_executor_initialized max_concurrency=%s max_calls=%s total_timeout_seconds=%s product_calls_serialized=true",
            self.max_concurrency,
            self.max_calls,
            self.total_timeout_seconds,
        )

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        base_payload: dict[str, Any] | None = None,
        events: WorkflowEventLogger | None = None,
        cancellation: threading.Event | None = None,
    ) -> list[ToolResult]:
        if not plan.validated:
            raise ValueError("CapabilityExecutor accepts only validated plans.")
        if len(plan.steps) > min(self.max_calls, plan.maximum_allowed_calls):
            raise ValueError("Validated plan exceeds executor call limit.")

        started = time.monotonic()
        pending = {step.id: step for step in plan.steps}
        completed_ids: set[str] = set()
        results: list[ToolResult] = []
        results_by_step: dict[str, ToolResult] = {}
        abandoned_running_work = False
        pool = ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="soorin-capability")
        try:
            while pending:
                if cancellation and cancellation.is_set():
                    for step in pending.values():
                        results.append(self._failure(step, "unavailable", "cancelled", "Capability execution was cancelled."))
                        if events:
                            events.emit("step_cancelled", plan_id=plan.plan_id, step_id=step.id, capability=step.capability, status="unavailable")
                    break
                if time.monotonic() - started >= self.total_timeout_seconds:
                    for step in pending.values():
                        results.append(self._failure(step, "unavailable", "request_budget_exceeded", "Capability request budget was exceeded."))
                        if events:
                            events.emit("step_cancelled", plan_id=plan.plan_id, step_id=step.id, capability=step.capability, status="unavailable", safe_error_code="request_budget_exceeded")
                    break

                ready = [step for step in pending.values() if set(step.depends_on) <= completed_ids]
                if not ready:
                    raise RuntimeError("Validated plan became unschedulable.")

                runnable: list[PlanStep] = []
                for step in ready:
                    failed_dependencies = [
                        dependency
                        for dependency in step.depends_on
                        if results_by_step[dependency].status in {"unavailable", "invalid", "not_configured"}
                    ]
                    if failed_dependencies:
                        result = self._failure(step, "unavailable", "dependency_failed", "A required dependency did not produce usable evidence.")
                        results.append(result)
                        results_by_step[step.id] = result
                        completed_ids.add(step.id)
                        pending.pop(step.id)
                        if events:
                            events.emit("step_skipped", plan_id=plan.plan_id, step_id=step.id, capability=step.capability, status=result.status, safe_error_code=result.safe_error_code)
                    else:
                        runnable.append(step)

                futures = []
                for step in runnable:
                    if events:
                        events.emit("step_scheduled", plan_id=plan.plan_id, step_id=step.id, capability=step.capability)
                    futures.append((step, pool.submit(self._execute_step, step, base_payload or {}, events, plan.plan_id)))

                for step, future in futures:
                    spec = self.registry.get(step.capability)
                    remaining = max(0.1, self.total_timeout_seconds - (time.monotonic() - started))
                    try:
                        result = future.result(timeout=min(spec.timeout_seconds, remaining))
                    except TimeoutError:
                        future.cancel()
                        abandoned_running_work = True
                        result = self._failure(step, "unavailable", "capability_timeout", "Capability execution timed out.")
                        if events:
                            events.emit("step_failed", plan_id=plan.plan_id, step_id=step.id, capability=step.capability, status=result.status, safe_error_code=result.safe_error_code)
                    results.append(result)
                    results_by_step[step.id] = result
                    completed_ids.add(step.id)
                    pending.pop(step.id)
        finally:
            pool.shutdown(wait=not abandoned_running_work, cancel_futures=True)
        return results

    def _execute_step(
        self,
        step: PlanStep,
        base_payload: dict[str, Any],
        events: WorkflowEventLogger | None,
        plan_id: str,
    ) -> ToolResult:
        spec = self.registry.get(step.capability)
        payload = {**step.arguments, **base_payload}
        started = time.perf_counter()
        if events:
            events.emit("step_started", plan_id=plan_id, step_id=step.id, capability=step.capability)
        try:
            lock = self._group_locks.get(spec.concurrency_group)
            if lock is None:
                result = self.registry.execute(step.capability, payload)
            else:
                with lock:
                    result = self.registry.execute(step.capability, payload)
        except Exception as exc:
            logger.warning(
                "event=capability_execution_exception plan_id=%s step_id=%s capability=%s error_class=%s",
                plan_id,
                step.id,
                step.capability,
                type(exc).__name__,
            )
            result = self._failure(step, "unavailable", type(exc).__name__, "Capability execution failed safely.")
        latency_ms = int((time.perf_counter() - started) * 1000)
        result = replace(
            result,
            step_id=step.id,
            latency_ms=result.latency_ms or latency_ms,
            provider=result.provider or spec.concurrency_group,
            evidence_type=result.evidence_type or spec.evidence_type,
        )
        result = replace(
            result,
            context_identity=result.context_identity or identity_for_tool_result(result),
        )
        get_metrics().observe_tool(
            step.capability,
            result.selected_views or tuple(step.arguments.get("views") or ()),
            result.status,
            result.latency_ms / 1000,
        )
        if events:
            event = "step_completed" if result.status in {"ok", "empty", "not_found"} else "step_partial" if result.status == "partial" else "step_failed"
            events.emit(
                event,
                plan_id=plan_id,
                step_id=step.id,
                capability=step.capability,
                provider=result.provider,
                status=result.status,
                latency_ms=result.latency_ms,
                retry_count=result.retry_count,
                cache_status=result.cache_status,
                freshness=result.freshness,
                completeness=result.completeness,
                total_items=result.total_count,
                included_items=result.included_count,
                omitted_items=result.omitted_count,
                truncated=result.truncated,
                error_class=result.error_classification,
                safe_error_code=result.safe_error_code,
            )
        return result

    @staticmethod
    def _failure(step: PlanStep, status: str, code: str, limitation: str) -> ToolResult:
        return ToolResult(
            status=status,  # type: ignore[arg-type]
            entities=tuple(step.arguments.get("entities") or ()),
            source_capability=step.capability,
            retrieved_at=_now(),
            freshness="unknown",
            completeness="unknown",
            limitations=(limitation,),
            error_classification=code,
            safe_error_code=code,
            step_id=step.id,
        )
