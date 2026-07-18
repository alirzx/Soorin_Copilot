"""Small bounded LangGraph shell around the stable direct Copilot request path."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

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
        stream_sink: Any,
        direct_executor: DirectExecutor,
    ) -> dict[str, Any]:
        initial: InvestigationState = {
            "request_id": request_id,
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

        def execute_direct(state: InvestigationState) -> dict[str, Any]:
            response = direct_executor(
                state["message"],
                state.get("session_id") or None,
                ui_context=state.get("ui_context"),
                request_id=state["request_id"],
                stream_sink=stream_sink,
            )
            return {
                "stages": [*(state.get("stages") or []), "execute_direct"],
                "final_response": response,
                "iteration_count": 1,
            }

        reviewer = EvidenceReviewer()

        def build_evidence(state: InvestigationState) -> dict[str, Any]:
            results = state.get("tool_results") or []
            return {
                "stages": [*(state.get("stages") or []), "build_evidence"],
                "evidence_pack": reviewer.build_pack(state["task"], results),
            }

        def review(state: InvestigationState) -> dict[str, Any]:
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
            state.update(execute_direct(state))
            state.update(build_evidence(state))
            state.update(review(state))
            state.update(self._stage("synthesize")(state))
            result = state["final_response"]
        else:
            graph = self._state_graph_type(InvestigationState)
            for name in ("resolve", "route", "validate_task", "select_workflow"):
                graph.add_node(name, self._stage(name))
            graph.add_node("execute_direct", execute_direct)
            graph.add_node("build_evidence", build_evidence)
            graph.add_node("review_evidence", review)
            graph.add_node("synthesize", self._stage("synthesize"))
            ordered = (
                "resolve",
                "route",
                "validate_task",
                "select_workflow",
                "execute_direct",
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
            "event=agent_workflow_complete request_id=%s runtime=%s mode=direct bounded=true recursion_limit=%s",
            request_id,
            self.runtime,
            self.recursion_limit,
        )
        return result
