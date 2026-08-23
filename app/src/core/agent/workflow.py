"""Bounded LangGraph workflow for Soorin Copilot investigations."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from src.core.agent.contracts import InvestigationState, TaskSpec
from src.core.agent.events import WorkflowEventContext, WorkflowEventLogger
from src.core.identity import RequestIdentity
from src.core.observability.metrics import get_metrics


logger = logging.getLogger(__name__)
DirectExecutor = Callable[..., dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RetryableWorkflowError(RuntimeError):
    """Explicit transient node failure eligible for one workflow-level retry."""


class WorkflowNodeRuntime(Protocol):
    """Run-scoped dependency boundary used by graph nodes."""

    def resolve_entities(self, state: InvestigationState) -> dict[str, Any]: ...
    def route(self, state: InvestigationState) -> dict[str, Any]: ...
    def validate_task(self, state: InvestigationState) -> dict[str, Any]: ...
    def build_direct_plan(self, state: InvestigationState) -> dict[str, Any]: ...
    def build_plan(self, state: InvestigationState) -> dict[str, Any]: ...
    def validate_plan(self, state: InvestigationState) -> dict[str, Any]: ...
    def build_fallback_plan(self, state: InvestigationState) -> dict[str, Any]: ...
    def execute_capabilities(self, state: InvestigationState) -> dict[str, Any]: ...
    def dispatch_specialists(self, state: InvestigationState) -> dict[str, Any]: ...
    def join_specialist_results(self, state: InvestigationState) -> dict[str, Any]: ...
    def build_evidence(self, state: InvestigationState) -> dict[str, Any]: ...
    def review_retrieval(self, state: InvestigationState) -> dict[str, Any]: ...
    def supplemental_retrieval(self, state: InvestigationState) -> dict[str, Any]: ...
    def compose_context(self, state: InvestigationState) -> dict[str, Any]: ...
    def review_context(self, state: InvestigationState) -> dict[str, Any]: ...
    def synthesize(self, state: InvestigationState) -> dict[str, Any]: ...
    def update_memory(self, state: InvestigationState) -> dict[str, Any]: ...
    def clarification_response(self, state: InvestigationState) -> dict[str, Any]: ...
    def safe_failure_response(self, state: InvestigationState) -> dict[str, Any]: ...
    def apply_clarification(self, state: InvestigationState, value: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class WorkflowRunContext:
    runtime: WorkflowNodeRuntime
    interrupt_on_clarification: bool = False


class _CompatibilityRuntime:
    """Keep the historical callback contract while production uses real node methods."""

    def __init__(self, executor: DirectExecutor, stream_sink: Any) -> None:
        self.executor = executor
        self.stream_sink = stream_sink

    @staticmethod
    def _empty(_state: InvestigationState) -> dict[str, Any]:
        return {}

    resolve_entities = _empty
    route = _empty
    validate_task = _empty
    build_direct_plan = _empty
    build_plan = _empty
    build_fallback_plan = _empty
    execute_capabilities = _empty
    dispatch_specialists = _empty
    join_specialist_results = _empty
    build_evidence = _empty
    review_retrieval = _empty
    supplemental_retrieval = _empty
    compose_context = _empty
    review_context = _empty
    update_memory = _empty
    clarification_response = _empty
    safe_failure_response = _empty
    apply_clarification = _empty

    @staticmethod
    def validate_plan(_state: InvestigationState) -> dict[str, Any]:
        return {"plan_validation_result": {"valid": True, "fallback_allowed": False}}

    def synthesize(self, state: InvestigationState) -> dict[str, Any]:
        response = self.executor(
            state["message"],
            state.get("session_id") or None,
            ui_context=state.get("ui_context"),
            request_id=state["request_id"],
            trace_id=state["trace_id"],
            stream_sink=self.stream_sink,
        )
        phase_state = response.pop("_phase2_state", {})
        return {
            "final_response": response,
            "synthesis_result": {
                "status": "completed",
                "provider": response.get("provider", ""),
                "model": response.get("model", ""),
            },
            "workflow_status": "completed",
            "terminal": False,
            **phase_state,
        }


class BoundedCopilotWorkflow:
    """Compile the full request lifecycle as explicit bounded LangGraph nodes."""

    recursion_limit = 32
    required_nodes = (
        "resolve_entities",
        "route",
        "validate_task",
        "build_direct_plan",
        "build_plan",
        "validate_plan",
        "build_fallback_plan",
        "dispatch_specialists",
        "join_specialist_results",
        "build_evidence",
        "review_retrieval",
        "supplemental_retrieval",
        "compose_context",
        "review_context",
        "synthesize",
        "update_memory",
        "clarification",
        "clarification_interrupt",
        "safe_failure",
    )

    def __init__(self, settings: Any | None = None) -> None:
        self.settings = settings
        checkpoint_backend = str(
            getattr(settings, "langgraph_checkpoint_backend", "none")
        )
        if checkpoint_backend == "sqlite":
            logger.warning(
                "event=langgraph_checkpoint_deferred backend=sqlite reason=checkpoint_safe_state_projection_required"
            )
        try:
            from langgraph.graph import END, START, StateGraph
        except ModuleNotFoundError:
            self._state_graph_type = None
            self._start = None
            self._end = None
            self.runtime = "deterministic_compat"
            self.graph = None
        else:
            self._state_graph_type = StateGraph
            self._start = START
            self._end = END
            self.runtime = "langgraph"
            self.graph = self._compile_graph()
        logger.info(
            "event=langgraph_initialized runtime=%s bounded=true recursion_limit=%s persistence=none configured_checkpoint_backend=%s",
            self.runtime,
            self.recursion_limit,
            checkpoint_backend,
        )

    @staticmethod
    def _events(state: InvestigationState) -> WorkflowEventLogger:
        return WorkflowEventLogger(
            logger,
            WorkflowEventContext(
                request_id=state.get("request_id", ""),
                trace_id=state.get("trace_id", ""),
                session_id=state.get("session_id", ""),
            ),
        )

    def _node(self, name: str) -> Callable[..., dict[str, Any]]:
        def execute(state: InvestigationState, runtime: Any) -> dict[str, Any]:
            events = self._events(state)
            attempt = int((state.get("retry_counters") or {}).get(name, 0)) + 1
            repeatable = name in {
                "resolve_entities",
                "route",
                "validate_task",
                "validate_plan",
                "build_evidence",
                "review_retrieval",
            }
            if name in set(state.get("completed_nodes") or ()) and not repeatable:
                events.emit(
                    "langgraph_node_skipped",
                    workflow_id=state.get("workflow_id"),
                    thread_id=state.get("thread_id"),
                    node=name,
                    attempt=attempt,
                    status="completed",
                    resumed=True,
                    reason="already_completed",
                )
                return {"stages": [f"{name}:skipped"]}
            started = time.perf_counter()
            started_at = _now()
            events.emit(
                "langgraph_node_started",
                workflow_id=state.get("workflow_id"),
                thread_id=state.get("thread_id"),
                node=name,
                attempt=attempt,
                status="running",
                started_at=started_at,
                resumed=bool(state.get("resumed")),
                input_summary=self._input_summary(name, state),
            )
            try:
                handler = getattr(runtime.context.runtime, name)
                update = dict(handler(state) or {})
            except RetryableWorkflowError as exc:
                events.emit(
                    "langgraph_node_retried",
                    level=logging.WARNING,
                    workflow_id=state.get("workflow_id"),
                    thread_id=state.get("thread_id"),
                    node=name,
                    attempt=attempt,
                    status="retrying",
                    error_type=type(exc).__name__,
                    retryable=True,
                    retry_count=1,
                )
                attempt += 1
                try:
                    update = dict(handler(state) or {})
                except Exception as retry_exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    events.emit(
                        "langgraph_node_failed",
                        level=logging.ERROR,
                        workflow_id=state.get("workflow_id"),
                        thread_id=state.get("thread_id"),
                        node=name,
                        attempt=attempt,
                        status="failed",
                        latency_ms=latency_ms,
                        error_type=type(retry_exc).__name__,
                        retryable=False,
                    )
                    get_metrics().observe_stage(
                        name,
                        latency_ms / 1000,
                        error=type(retry_exc).__name__,
                    )
                    raise
            except Exception as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                events.emit(
                    "langgraph_node_failed",
                    level=logging.ERROR,
                    workflow_id=state.get("workflow_id"),
                    thread_id=state.get("thread_id"),
                    node=name,
                    attempt=attempt,
                    status="failed",
                    latency_ms=latency_ms,
                    error_type=type(exc).__name__,
                    retryable=False,
                )
                get_metrics().observe_stage(
                    name,
                    latency_ms / 1000,
                    error=type(exc).__name__,
                )
                raise
            latency_ms = int((time.perf_counter() - started) * 1000)
            next_edge = str(update.get("next_edge") or "")
            input_summary = self._input_summary(name, state)
            output_summary = self._output_summary(name, update)
            record = {
                "node": name,
                "attempt": attempt,
                "status": "completed",
                "started_at": started_at,
                "completed_at": _now(),
                "latency_ms": latency_ms,
                "next_edge": next_edge,
                "resumed": bool(state.get("resumed")),
                "input_summary": input_summary,
                "output_summary": output_summary,
            }
            update.update(
                {
                    "updated_at": record["completed_at"],
                    "completed_nodes": [name],
                    "node_records": [record],
                    "stages": [name],
                }
            )
            events.emit(
                "langgraph_node_completed",
                workflow_id=state.get("workflow_id"),
                thread_id=state.get("thread_id"),
                node=name,
                attempt=attempt,
                status="completed",
                latency_ms=latency_ms,
                next_edge=next_edge,
                output_summary=output_summary,
                resumed=bool(state.get("resumed")),
            )
            get_metrics().observe_stage(name, latency_ms / 1000)
            return update

        execute.__name__ = name
        return execute

    @staticmethod
    def _input_summary(name: str, state: InvestigationState) -> str:
        resolution = state.get("resolved_entities")
        resolved = tuple(
            (resolution.get("entities") if isinstance(resolution, dict) else getattr(resolution, "entities", ()))
            or ()
        )
        task_entities = tuple(getattr(state.get("task"), "entities", ()) or ())
        entities = resolved or task_entities
        if name == "resolve_entities":
            return f"explicit=pending,resolved={len(resolved)},status={state.get('workflow_status', 'running')}"
        if name == "route":
            previous_scope = getattr(state.get("active_entity_state"), "previous_scope", None) or "none"
            return f"entities={len(entities)},previous_scope={previous_scope}"
        if name in {"dispatch_specialists", "join_specialist_results"}:
            plan = state.get("execution_plan")
            return f"plan_steps={len(getattr(plan, 'steps', ()) or ())},entities={len(entities)}"
        if name == "build_evidence":
            results = state.get("tool_results") or ()
            usable = sum(item.status in {"ok", "partial", "empty", "not_found"} for item in results)
            failed = len(results) - usable
            return f"tool_results={len(results)},usable={usable},failed={failed}"
        return f"entities={len(entities)},status={state.get('workflow_status', 'running')}"

    @staticmethod
    def _output_summary(name: str, update: dict[str, Any]) -> str:
        if name == "resolve_entities":
            resolution = update.get("resolved_entities")
            entities = tuple(getattr(resolution, "entities", ()) or ())
            return (
                f"explicit={getattr(resolution, 'explicit_candidate_count', 0)},"
                f"resolved={len(entities)},reference={bool(getattr(resolution, 'reference_detected', False))}"
            )
        if name == "route":
            route = update.get("routing_result")
            return (
                f"intent={getattr(route, 'intent', 'unknown')},"
                f"scope={getattr(route, 'scope', 'none')},"
                f"entities={len(getattr(route, 'materialized_entities', ()) or ())}"
            )
        if name == "dispatch_specialists":
            return (
                f"specialists={len(update.get('specialist_records') or ())},"
                f"results={len(update.get('specialist_tool_results') or ()) + len(update.get('generic_tool_results') or ())}"
            )
        if name == "join_specialist_results":
            return f"tool_results={len(update.get('tool_results') or ())},edge={update.get('next_edge', '')}"
        return (
            f"node={name},edge={update.get('next_edge', '')},"
            f"status={update.get('workflow_status', 'running')}"
        )

    def _compile_graph(self) -> Any:
        from langgraph.graph import StateGraph

        graph = StateGraph(InvestigationState, context_schema=WorkflowRunContext)
        for name in self.required_nodes:
            if name == "clarification_interrupt":
                graph.add_node(name, self._clarification_interrupt)
                continue
            runtime_name = {
                "clarification": "clarification_response",
                "safe_failure": "safe_failure_response",
            }.get(name, name)
            graph.add_node(name, self._node(runtime_name))

        graph.add_edge(self._start, "resolve_entities")
        graph.add_conditional_edges(
            "resolve_entities",
            self._after_resolution,
            {"route": "route", "clarification": "clarification_interrupt"},
        )
        graph.add_conditional_edges(
            "clarification_interrupt",
            self._after_clarification_interrupt,
            {"resume": "resolve_entities", "respond": "clarification"},
        )
        graph.add_edge("route", "validate_task")
        graph.add_conditional_edges(
            "validate_task",
            self._after_task,
            {
                "direct": "build_direct_plan",
                "planner": "build_plan",
                "clarification": "clarification",
                "safe_failure": "safe_failure",
            },
        )
        graph.add_edge("build_direct_plan", "validate_plan")
        graph.add_edge("build_plan", "validate_plan")
        graph.add_conditional_edges(
            "validate_plan",
            self._after_plan_validation,
            {
                "execute": "dispatch_specialists",
                "fallback": "build_fallback_plan",
                "safe_failure": "safe_failure",
            },
        )
        graph.add_edge("build_fallback_plan", "validate_plan")
        graph.add_edge("dispatch_specialists", "join_specialist_results")
        graph.add_edge("join_specialist_results", "build_evidence")
        graph.add_edge("build_evidence", "review_retrieval")
        graph.add_conditional_edges(
            "review_retrieval",
            self._after_retrieval_review,
            {
                "supplemental": "supplemental_retrieval",
                "compose": "compose_context",
                "safe_failure": "compose_context",
            },
        )
        graph.add_edge("supplemental_retrieval", "build_evidence")
        graph.add_edge("compose_context", "review_context")
        graph.add_conditional_edges(
            "review_context",
            self._after_context_review,
            {"synthesize": "synthesize", "limited": "synthesize", "blocked": "synthesize"},
        )
        graph.add_conditional_edges(
            "synthesize",
            self._after_synthesis,
            {"memory": "update_memory", "terminal": self._end},
        )
        graph.add_edge("update_memory", self._end)
        graph.add_edge("clarification", self._end)
        graph.add_edge("safe_failure", self._end)
        return graph.compile(name="soorin_bounded_investigation")

    def _clarification_interrupt(self, state: InvestigationState, runtime: Any) -> dict[str, Any]:
        if not runtime.context.interrupt_on_clarification:
            return {"next_edge": "respond"}
        from langgraph.types import interrupt

        clarification = dict(state.get("clarification") or {})
        self._events(state).emit(
            "langgraph_interrupt_created",
            workflow_id=state.get("workflow_id"),
            thread_id=state.get("thread_id"),
            node="clarification_interrupt",
            status="clarification_required",
            reason=clarification.get("code", "clarification_required"),
        )
        value = interrupt(
            {
                "type": "clarification_required",
                "request_id": state.get("request_id"),
                "trace_id": state.get("trace_id"),
                "question": clarification.get("answer", "Please clarify the request."),
                "code": clarification.get("code", "clarification_required"),
            }
        )
        return dict(runtime.context.runtime.apply_clarification(state, value) or {})

    @staticmethod
    def _after_resolution(state: InvestigationState) -> str:
        return "clarification" if state.get("workflow_status") == "clarification_required" else "route"

    @staticmethod
    def _after_clarification_interrupt(state: InvestigationState) -> str:
        return "resume" if state.get("workflow_status") == "running" else "respond"

    @staticmethod
    def _after_task(state: InvestigationState) -> str:
        if state.get("workflow_status") == "clarification_required":
            return "clarification"
        if state.get("workflow_status") in {"failed", "cancelled"}:
            return "safe_failure"
        task = state.get("task")
        return "planner" if task and task.workflow_mode == "multi_step" and state.get("planner_called") else "direct"

    @staticmethod
    def _after_plan_validation(state: InvestigationState) -> str:
        result = state.get("plan_validation_result") or {}
        if result.get("valid"):
            return "execute"
        if result.get("fallback_allowed") and not state.get("fallback_used"):
            return "fallback"
        return "safe_failure"

    @staticmethod
    def _after_retrieval_review(state: InvestigationState) -> str:
        decision = state.get("review_decision")
        if (
            decision
            and decision.outcome == "missing_required_evidence"
            and decision.supplemental_allowed
            and int(state.get("supplemental_retrieval_count", 0)) < 1
        ):
            return "supplemental"
        if decision and decision.outcome == "safe_failure":
            return "safe_failure"
        return "compose"

    @staticmethod
    def _after_context_review(state: InvestigationState) -> str:
        return str((state.get("context_review") or {}).get("decision") or "synthesize")

    @staticmethod
    def _after_synthesis(state: InvestigationState) -> str:
        if state.get("workflow_status") in {"failed", "cancelled"}:
            return "terminal"
        return "memory"

    def run(
        self,
        *,
        message: str,
        session_id: str | None,
        ui_context: dict[str, Any] | None,
        request_id: str,
        trace_id: str | None = None,
        request_identity: RequestIdentity | None = None,
        stream_sink: Any = None,
        direct_executor: DirectExecutor | None = None,
        typed_executor: DirectExecutor | None = None,
        node_runtime: WorkflowNodeRuntime | None = None,
        interrupt_on_clarification: bool = False,
    ) -> dict[str, Any]:
        request_started = time.perf_counter()
        resolved_trace_id = trace_id or uuid4().hex[:16]
        identity = request_identity or RequestIdentity.resolve(
            session_id=session_id,
            request_id=request_id,
        )
        session = identity.session_id
        workflow_id = f"wf-{request_id}"
        initial: InvestigationState = {
            "request_identity": identity,
            "request_id": request_id,
            "trace_id": resolved_trace_id,
            "session_id": session,
            "message": message,
            "original_message": message,
            "ui_context": ui_context,
            "streaming": stream_sink is not None,
            "workflow_id": workflow_id,
            "thread_id": identity.thread_key,
            "started_at": _now(),
            "updated_at": _now(),
            "workflow_status": "running",
            "terminal": False,
            "resumed": False,
            "iteration_count": 0,
            "supplemental_retrieval_count": 0,
            "retry_counters": {},
            "errors": [],
            "stages": [],
            "completed_nodes": [],
            "node_records": [],
            "planner_called": False,
            "fallback_used": False,
            "routing_fallback_used": False,
            "task": TaskSpec(
                request=message,
                intent="delegated_semantic_route",
                scope="none",
                direction="none",
                entities=(),
                required_capabilities=(),
            ),
        }
        selected_executor = typed_executor or direct_executor
        if node_runtime is None:
            if selected_executor is None:
                raise ValueError("A node runtime or compatibility executor is required.")
            node_runtime = _CompatibilityRuntime(selected_executor, stream_sink)

        config = self._config(request_id)
        events = self._events(initial)
        events.emit(
            "langgraph_workflow_started",
            workflow_id=workflow_id,
            thread_id=identity.thread_key,
            runtime=self.runtime,
            status="running",
        )
        try:
            if self.graph is None or self._state_graph_type is None:
                final = self._run_deterministic(initial, node_runtime)
            else:
                final = self.graph.invoke(
                    initial,
                    config=config,
                    context=WorkflowRunContext(node_runtime, interrupt_on_clarification),
                )
        except Exception:
            get_metrics().observe_copilot(
                "failed",
                "unknown",
                time.perf_counter() - request_started,
            )
            raise
        response = final.get("final_response")
        if not isinstance(response, dict):
            if interrupt_on_clarification and final.get("workflow_status") == "clarification_required":
                clarification = final.get("clarification") or {}
                get_metrics().observe_copilot(
                    "partial",
                    str(getattr(final.get("task"), "workflow_mode", "unknown")),
                    time.perf_counter() - request_started,
                )
                return {
                    "session_id": final.get("session_id", ""),
                    "answer": str(clarification.get("answer") or ""),
                    "provider": "langgraph",
                    "model": "workflow-interrupt",
                    "_warnings": [str(clarification.get("code") or "clarification_required")],
                    "_interrupt": True,
                }
            get_metrics().observe_copilot(
                "failed",
                str(getattr(final.get("task"), "workflow_mode", "unknown")),
                time.perf_counter() - request_started,
            )
            raise RuntimeError("Workflow completed without a final response.")
        status = str(final.get("workflow_status") or "completed")
        workflow_mode = str(getattr(final.get("task"), "workflow_mode", "unknown"))
        events.emit(
            "langgraph_workflow_completed" if status.startswith("completed") else "langgraph_workflow_partial",
            workflow_id=workflow_id,
            thread_id=identity.thread_key,
            status=status,
            tool_call_count=len(final.get("tool_results") or ()),
            planner_called=bool(final.get("planner_called")),
            fallback_used=bool(final.get("fallback_used")),
            reason=final.get("limitation_reasons") or (),
        )
        self._render_workflow_trace(final)
        metric_status = (
            "completed" if status.startswith("completed") else
            "cancelled" if status == "cancelled" else
            "failed" if status == "failed" else
            "partial"
        )
        get_metrics().observe_copilot(
            metric_status,
            workflow_mode,
            time.perf_counter() - request_started,
        )
        if final.get("routing_fallback_used"):
            get_metrics().observe_workflow_fallback("routing")
        if final.get("fallback_used"):
            get_metrics().observe_workflow_fallback("plan")
        return response

    def _config(self, request_id: str) -> dict[str, Any]:
        return {"recursion_limit": self.recursion_limit}

    def _render_workflow_trace(self, state: InvestigationState) -> None:
        if not self.settings or not getattr(self.settings, "copilot_human_trace_enabled", False):
            return
        from src.core.copilot.trace import render_human_copilot_trace, trace_from_investigation_state

        render_human_copilot_trace(
            trace_from_investigation_state(state),
            settings=self.settings,
            output_logger=logger,
        )

    def _run_deterministic(
        self,
        state: InvestigationState,
        runtime: WorkflowNodeRuntime,
    ) -> InvestigationState:
        """Framework-unavailable fallback using the same bounded node contract."""
        current = dict(state)
        current.update(runtime.resolve_entities(current) or {})
        if self._after_resolution(current) == "clarification":
            current.update(runtime.clarification_response(current) or {})
            return current  # type: ignore[return-value]
        for name in ("route", "validate_task"):
            current.update(getattr(runtime, name)(current) or {})
        edge = self._after_task(current)
        if edge in {"clarification", "safe_failure"}:
            current.update(getattr(runtime, f"{edge}_response")(current) or {})
            return current  # type: ignore[return-value]
        current.update(getattr(runtime, "build_plan" if edge == "planner" else "build_direct_plan")(current) or {})
        current.update(runtime.validate_plan(current) or {})
        if self._after_plan_validation(current) == "fallback":
            current.update(runtime.build_fallback_plan(current) or {})
            current.update(runtime.validate_plan(current) or {})
        if self._after_plan_validation(current) != "execute":
            current.update(runtime.safe_failure_response(current) or {})
            return current  # type: ignore[return-value]
        for name in ("dispatch_specialists", "join_specialist_results", "build_evidence", "review_retrieval"):
            current.update(getattr(runtime, name)(current) or {})
        if self._after_retrieval_review(current) == "supplemental":
            current.update(runtime.supplemental_retrieval(current) or {})
            current.update(runtime.build_evidence(current) or {})
            current.update(runtime.review_retrieval(current) or {})
        for name in ("compose_context", "review_context", "synthesize"):
            current.update(getattr(runtime, name)(current) or {})
        if self._after_synthesis(current) == "memory":
            current.update(runtime.update_memory(current) or {})
        return current  # type: ignore[return-value]

    def graph_node_names(self) -> tuple[str, ...]:
        if self.graph is None:
            return self.required_nodes
        return tuple(self.graph.get_graph().nodes)

    def close(self) -> None:
        """Keep the service lifecycle hook stable; no persistent resource is owned."""
