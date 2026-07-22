"""Shared execution boundary for deterministic specialist subgraphs."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any, Callable, TypedDict

from src.core.agent.contracts import ExecutionPlan, ToolResult
from src.core.agent.events import WorkflowEventContext, WorkflowEventLogger


logger = logging.getLogger(__name__)


class SpecialistState(TypedDict, total=False):
    request_id: str
    trace_id: str
    session_id: str
    workflow_id: str
    specialist: str
    execution_plan: ExecutionPlan
    route: Any
    selected_steps: tuple[Any, ...]
    tool_results: list[ToolResult]
    specialist_result: Any
    status: str


class BoundedSpecialistSubgraph:
    """Select and execute one validated capability domain without an LLM."""

    name = "specialist"
    capability_prefixes: tuple[str, ...] = ()

    def __init__(self, executor: Any) -> None:
        self.executor = executor
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(SpecialistState)
        graph.add_node("select_validated_steps", self._node("select_validated_steps", self._select_steps))
        graph.add_node("execute_validated_steps", self._node("execute_validated_steps", self._execute_steps))
        graph.add_node("normalize_result", self._node("normalize_result", self._normalize))
        graph.add_edge(START, "select_validated_steps")
        graph.add_edge("select_validated_steps", "execute_validated_steps")
        graph.add_edge("execute_validated_steps", "normalize_result")
        graph.add_edge("normalize_result", END)
        self.graph = graph.compile(name=f"soorin_{self.name}_specialist")

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        return dict(self.graph.invoke(state))

    @staticmethod
    def _events(state: SpecialistState) -> WorkflowEventLogger:
        return WorkflowEventLogger(
            logger,
            WorkflowEventContext(
                request_id=state.get("request_id", ""),
                trace_id=state.get("trace_id", ""),
                session_id=state.get("session_id", ""),
            ),
        )

    def _node(
        self,
        node: str,
        handler: Callable[[SpecialistState], dict[str, Any]],
    ) -> Callable[[SpecialistState], dict[str, Any]]:
        def execute(state: SpecialistState) -> dict[str, Any]:
            events = self._events(state)
            started = time.perf_counter()
            events.emit(
                "specialist_subgraph_node_started",
                workflow_id=state.get("workflow_id"),
                specialist=self.name,
                subgraph_node=node,
                attempt=1,
                status="running",
            )
            try:
                update = handler(state)
            except Exception as exc:
                events.emit(
                    "specialist_subgraph_node_failed",
                    level=logging.ERROR,
                    workflow_id=state.get("workflow_id"),
                    specialist=self.name,
                    subgraph_node=node,
                    attempt=1,
                    status="failed",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error_type=type(exc).__name__,
                )
                raise
            events.emit(
                "specialist_subgraph_node_completed",
                workflow_id=state.get("workflow_id"),
                specialist=self.name,
                subgraph_node=node,
                attempt=1,
                status=str(update.get("status") or state.get("status") or "completed"),
                latency_ms=int((time.perf_counter() - started) * 1000),
                result_count=len(update.get("tool_results") or state.get("tool_results") or ()),
            )
            return update

        return execute

    def _select_steps(self, state: SpecialistState) -> dict[str, Any]:
        plan = state["execution_plan"]
        if not plan.validated:
            raise ValueError("Specialists accept only a validated parent plan.")
        selected = tuple(
            step
            for step in plan.steps
            if any(step.capability.startswith(prefix) for prefix in self.capability_prefixes)
        )
        return {"selected_steps": selected, "status": "completed" if selected else "skipped"}

    def _execute_steps(self, state: SpecialistState) -> dict[str, Any]:
        steps = tuple(state.get("selected_steps") or ())
        if not steps:
            return {"tool_results": [], "status": "skipped"}
        plan = state["execution_plan"]
        selected_ids = {step.id for step in steps}
        bounded_steps = tuple(
            replace(step, depends_on=tuple(item for item in step.depends_on if item in selected_ids))
            for step in steps
        )
        capabilities = tuple(dict.fromkeys(step.capability for step in bounded_steps))
        subplan = replace(
            plan,
            task=replace(plan.task, required_capabilities=capabilities),
            steps=bounded_steps,
            maximum_allowed_calls=min(plan.maximum_allowed_calls, len(bounded_steps)),
            validated=True,
        )
        results = self.executor.execute(
            subplan,
            base_payload={
                "request_id": state.get("request_id", ""),
                "session_id": state.get("session_id", ""),
                "route": state.get("route"),
            },
        )
        status = (
            "completed"
            if len(results) == len(steps)
            and all(item.status in {"ok", "empty", "not_found"} for item in results)
            else "completed_with_limitations"
        )
        return {"tool_results": results, "status": status}

    def _normalize(self, state: SpecialistState) -> dict[str, Any]:
        return {"specialist_result": self.normalize_result(state), "status": state.get("status", "skipped")}

    def normalize_result(self, state: SpecialistState) -> Any:
        raise NotImplementedError
