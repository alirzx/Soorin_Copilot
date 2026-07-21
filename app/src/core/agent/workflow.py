"""Small bounded LangGraph shell around the stable direct Copilot request path."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from src.core.agent.contracts import InvestigationState, TaskSpec
from src.core.agent.reviewer import EvidenceReviewer


logger = logging.getLogger(__name__)
DirectExecutor = Callable[..., dict[str, Any]]


class BoundedCopilotWorkflow:
    """Use StateGraph when installed; retain an offline-compatible deterministic runner."""

    recursion_limit = 12

    def __init__(self) -> None:
        try:
            from langgraph.graph import END, START, StateGraph
        except ModuleNotFoundError:
            self._state_graph_type = None
            self._start = None
            self._end = None
            self.runtime = "deterministic_compat"
        else:
            self._state_graph_type = StateGraph
            self._start = START
            self._end = END
            self.runtime = "langgraph"
        logger.info(
            "event=langgraph_initialized runtime=%s bounded=true recursion_limit=%s",
            self.runtime,
            self.recursion_limit,
        )

    @staticmethod
    def _stage(name: str) -> Callable[[InvestigationState], dict[str, Any]]:
        def node(state: InvestigationState) -> dict[str, Any]:
            return {"stages": [*(state.get("stages") or []), name]}

        return node

    def run(
        self,
        *,
        message: str,
        session_id: str | None,
        ui_context: dict[str, Any] | None,
        request_id: str,
        trace_id: str | None,
        stream_sink: Any,
        direct_executor: DirectExecutor,
        typed_executor: DirectExecutor | None = None,
    ) -> dict[str, Any]:
        initial: InvestigationState = {
            "request_id": request_id,
            "trace_id": trace_id or uuid4().hex[:16],
            "session_id": session_id or "",
            "message": message,
            "ui_context": ui_context,
            "iteration_count": 0,
            "errors": [],
            "stages": [],
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

        def execute_phase2(state: InvestigationState) -> dict[str, Any]:
            try:
                response = selected_executor(
                    state["message"],
                    state.get("session_id") or None,
                    ui_context=state.get("ui_context"),
                    request_id=state["request_id"],
                    trace_id=state["trace_id"],
                    stream_sink=stream_sink,
                )
            except Exception as exc:
                logger.error(
                    "event=workflow_failed request_id=%s trace_id=%s session_id=%s error_class=%s safe_error_code=phase2_execution_failed",
                    state["request_id"],
                    state["trace_id"],
                    state.get("session_id") or "",
                    type(exc).__name__,
                )
                raise
            phase2_state = response.pop("_phase2_state", {})
            return {
                "stages": [*(state.get("stages") or []), "execute_phase2"],
                "final_response": response,
                "iteration_count": 1,
                **phase2_state,
            }

        reviewer = EvidenceReviewer()

        def build_evidence(state: InvestigationState) -> dict[str, Any]:
            if state.get("evidence_pack") is not None:
                return {"stages": [*(state.get("stages") or []), "build_evidence"]}
            results = state.get("tool_results") or []
            return {
                "stages": [*(state.get("stages") or []), "build_evidence"],
                "evidence_pack": reviewer.build_pack(state["task"], results),
            }

        def review(state: InvestigationState) -> dict[str, Any]:
            if state.get("review_decision") is not None:
                return {"stages": [*(state.get("stages") or []), "review_evidence"]}
            return {
                "stages": [*(state.get("stages") or []), "review_evidence"],
                "review_decision": reviewer.review(
                    state["task"],
                    state.get("tool_results") or [],
                ),
            }

        if self._state_graph_type is None:
            state = initial
            for name in ("resolve", "route", "validate_task", "select_workflow"):
                state.update(self._stage(name)(state))
            state.update(execute_phase2(state))
            state.update(build_evidence(state))
            state.update(review(state))
            state.update(self._stage("synthesize")(state))
            result = state["final_response"]
        else:
            graph = self._state_graph_type(InvestigationState)
            for name in ("resolve", "route", "validate_task", "select_workflow"):
                graph.add_node(name, self._stage(name))
            graph.add_node("execute_phase2", execute_phase2)
            graph.add_node("build_evidence", build_evidence)
            graph.add_node("review_evidence", review)
            graph.add_node("synthesize", self._stage("synthesize"))
            ordered = (
                "resolve",
                "route",
                "validate_task",
                "select_workflow",
                "execute_phase2",
                "build_evidence",
                "review_evidence",
                "synthesize",
            )
            graph.add_edge(self._start, ordered[0])
            for source, target in zip(ordered, ordered[1:]):
                graph.add_edge(source, target)
            graph.add_edge(ordered[-1], self._end)
            final = graph.compile().invoke(initial, config={"recursion_limit": self.recursion_limit})
            result = final["final_response"]
        logger.info(
            "event=agent_workflow_complete request_id=%s trace_id=%s runtime=%s mode=phase2_typed bounded=true recursion_limit=%s executor=%s",
            request_id,
            initial["trace_id"],
            self.runtime,
            self.recursion_limit,
            "typed" if typed_executor is not None else "compatibility_alias",
        )
        return result
