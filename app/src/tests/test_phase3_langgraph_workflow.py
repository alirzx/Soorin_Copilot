"""Focused offline tests for the bounded no-checkpointer LangGraph workflow."""

from __future__ import annotations

import logging
from threading import Thread
from typing import Any

from src.core.agent.contracts import (
    EvidencePack,
    ExecutionPlan,
    InvestigationState,
    ReviewDecision,
    TaskSpec,
)
from src.core.agent.workflow import BoundedCopilotWorkflow, RetryableWorkflowError


class FakeNodeRuntime:
    def __init__(
        self,
        *,
        mode: str = "direct",
        invalid_plan_once: bool = False,
        supplemental: bool = False,
        retry_route_once: bool = False,
    ) -> None:
        self.mode = mode
        self.invalid_plan_once = invalid_plan_once
        self.supplemental = supplemental
        self.retry_route_once = retry_route_once
        self.calls: list[str] = []
        self.validation_calls = 0
        self.review_calls = 0
        self.memory_writes = 0
        self.synthesis_calls = 0

    def _call(self, name: str) -> None:
        self.calls.append(name)

    def resolve_entities(self, state: InvestigationState) -> dict[str, Any]:
        self._call("resolve_entities")
        return {"resolved_entities": {"entities": ["192.0.2.10"]}, "next_edge": "route"}

    def route(self, state: InvestigationState) -> dict[str, Any]:
        self._call("route")
        if self.retry_route_once:
            self.retry_route_once = False
            raise RetryableWorkflowError("temporary router failure")
        return {"routing_result": {"intent": "fixture"}, "next_edge": "validate_task"}

    def validate_task(self, state: InvestigationState) -> dict[str, Any]:
        self._call("validate_task")
        task = TaskSpec(
            request=state["message"],
            intent="asset_investigation",
            scope="node_summary",
            direction="both",
            entities=("192.0.2.10",),
            required_capabilities=(),
            workflow_mode="multi_step" if self.mode == "planner" else "direct",
        )
        return {
            "task": task,
            "planner_called": self.mode == "planner",
            "next_edge": self.mode,
        }

    def build_direct_plan(self, state: InvestigationState) -> dict[str, Any]:
        self._call("build_direct_plan")
        return {"execution_plan": ExecutionPlan(state["task"], (), plan_id="direct")}

    def build_plan(self, state: InvestigationState) -> dict[str, Any]:
        self._call("build_plan")
        return {
            "execution_plan": ExecutionPlan(
                state["task"],
                (),
                plan_id="llm",
                source="llm",
                planner_called=True,
            )
        }

    def validate_plan(self, state: InvestigationState) -> dict[str, Any]:
        self._call("validate_plan")
        self.validation_calls += 1
        invalid = self.invalid_plan_once and self.validation_calls == 1
        return {
            "plan_validation_result": {
                "valid": not invalid,
                "fallback_allowed": invalid,
            },
            "next_edge": "fallback" if invalid else "execute",
        }

    def build_fallback_plan(self, state: InvestigationState) -> dict[str, Any]:
        self._call("build_fallback_plan")
        return {
            "execution_plan": ExecutionPlan(
                state["task"],
                (),
                plan_id="fallback",
                source="deterministic_fallback",
            ),
            "fallback_used": True,
        }

    def execute_capabilities(self, state: InvestigationState) -> dict[str, Any]:
        self._call("execute_capabilities")
        return {"tool_results": [], "capability_results": []}

    def dispatch_specialists(self, state: InvestigationState) -> dict[str, Any]:
        self._call("dispatch_specialists")
        return {"specialist_tool_results": [], "generic_tool_results": []}

    def join_specialist_results(self, state: InvestigationState) -> dict[str, Any]:
        self._call("join_specialist_results")
        return {"tool_results": [], "capability_results": []}

    def build_evidence(self, state: InvestigationState) -> dict[str, Any]:
        self._call("build_evidence")
        return {"evidence_pack": EvidencePack(state["task"], (), (), ())}

    def review_retrieval(self, state: InvestigationState) -> dict[str, Any]:
        self._call("review_retrieval")
        self.review_calls += 1
        if self.supplemental and self.review_calls == 1:
            decision = ReviewDecision(
                outcome="missing_required_evidence",
                supplemental_allowed=True,
                next_capability="knowledge.search",
                next_arguments={"entities": []},
            )
        else:
            decision = ReviewDecision(outcome="sufficient")
        return {"review_decision": decision}

    def supplemental_retrieval(self, state: InvestigationState) -> dict[str, Any]:
        self._call("supplemental_retrieval")
        return {"supplemental_retrieval_count": 1}

    def compose_context(self, state: InvestigationState) -> dict[str, Any]:
        self._call("compose_context")
        return {"composed_context": "bounded", "model_messages": []}

    def review_context(self, state: InvestigationState) -> dict[str, Any]:
        self._call("review_context")
        return {"context_review": {"decision": "synthesize"}, "next_edge": "synthesize"}

    def synthesize(self, state: InvestigationState) -> dict[str, Any]:
        self._call("synthesize")
        self.synthesis_calls += 1
        return {
            "final_response": {
                "session_id": state["session_id"],
                "answer": "ok",
                "provider": "fake",
                "model": "fake",
            },
            "synthesis_result": {"answer": "ok"},
            "workflow_status": "completed",
        }

    def update_memory(self, state: InvestigationState) -> dict[str, Any]:
        self._call("update_memory")
        self.memory_writes += 1
        return {
            "memory_update_result": {"completed": True},
            "terminal": True,
            "workflow_status": "completed",
        }

    def clarification_response(self, state: InvestigationState) -> dict[str, Any]:
        raise AssertionError("clarification was not expected")

    def safe_failure_response(self, state: InvestigationState) -> dict[str, Any]:
        self._call("safe_failure_response")
        return {
            "final_response": {
                "session_id": state["session_id"],
                "answer": "safe failure",
                "provider": "deterministic",
                "model": "guard",
            },
            "terminal": True,
            "workflow_status": "failed",
        }

    def apply_clarification(self, state: InvestigationState, value: Any) -> dict[str, Any]:
        return {
            "message": f"{state['original_message']}\nClarification: {value}",
            "workflow_status": "running",
            "terminal": False,
            "clarification": {},
            "resumed": True,
        }


class ClarificationRuntime(FakeNodeRuntime):
    def resolve_entities(self, state: InvestigationState) -> dict[str, Any]:
        self._call("resolve_entities")
        if "Clarification:" not in state["message"]:
            return {
                "workflow_status": "clarification_required",
                "terminal": False,
                "clarification": {
                    "answer": "Which second asset should be compared?",
                    "code": "comparison_second_entity_required",
                },
            }
        return {"resolved_entities": {"entities": ["192.0.2.10", "192.0.2.11"]}}

    def clarification_response(self, state: InvestigationState) -> dict[str, Any]:
        self._call("clarification_response")
        return {
            "final_response": {
                "session_id": state["session_id"],
                "answer": "Which second asset should be compared?",
                "provider": "deterministic",
                "model": "clarification",
            },
            "terminal": True,
            "workflow_status": "clarification_required",
        }


def run_workflow(workflow: BoundedCopilotWorkflow, runtime: FakeNodeRuntime, request_id: str = "r1") -> dict[str, Any]:
    return workflow.run(
        message="Investigate 192.0.2.10",
        session_id=f"session-{request_id}",
        ui_context=None,
        request_id=request_id,
        trace_id=f"trace-{request_id}",
        node_runtime=runtime,
    )


def test_graph_contains_required_nodes_and_conditional_routes() -> None:
    workflow = BoundedCopilotWorkflow()
    names = set(workflow.graph_node_names())
    assert set(workflow.required_nodes) <= names
    graph = workflow.graph.get_graph()
    conditional_sources = {edge.source for edge in graph.edges if edge.conditional}
    assert {
        "resolve_entities",
        "validate_task",
        "validate_plan",
        "review_retrieval",
        "review_context",
        "synthesize",
    } <= conditional_sources


def test_direct_request_skips_planner_and_updates_memory_once() -> None:
    runtime = FakeNodeRuntime(mode="direct")
    result = run_workflow(BoundedCopilotWorkflow(), runtime)
    assert result["answer"] == "ok"
    assert "build_direct_plan" in runtime.calls
    assert "build_plan" not in runtime.calls
    assert runtime.memory_writes == 1


def test_planner_request_is_validated_before_execution() -> None:
    runtime = FakeNodeRuntime(mode="planner")
    run_workflow(BoundedCopilotWorkflow(), runtime)
    assert runtime.calls.index("build_plan") < runtime.calls.index("validate_plan")
    assert runtime.calls.index("validate_plan") < runtime.calls.index("dispatch_specialists")


def test_invalid_plan_uses_one_validated_deterministic_fallback() -> None:
    runtime = FakeNodeRuntime(mode="planner", invalid_plan_once=True)
    run_workflow(BoundedCopilotWorkflow(), runtime)
    assert runtime.calls.count("build_fallback_plan") == 1
    assert runtime.calls.count("validate_plan") == 2
    assert runtime.calls.count("dispatch_specialists") == 1


def test_supplemental_retrieval_is_bounded_to_one_round() -> None:
    runtime = FakeNodeRuntime(supplemental=True)
    run_workflow(BoundedCopilotWorkflow(), runtime)
    assert runtime.calls.count("supplemental_retrieval") == 1
    assert runtime.review_calls == 2


def test_one_workflow_run_synthesizes_and_updates_memory_once() -> None:
    runtime = FakeNodeRuntime()
    result = run_workflow(BoundedCopilotWorkflow(), runtime, "single-run")
    assert result["answer"] == "ok"
    assert runtime.synthesis_calls == 1
    assert runtime.memory_writes == 1


def test_workflow_creates_no_sqlite_wal_or_shm_files(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    runtime = FakeNodeRuntime()
    run_workflow(BoundedCopilotWorkflow(), runtime, "no-persistence")
    assert not list(tmp_path.rglob("*.sqlite3"))
    assert not list(tmp_path.rglob("*.sqlite3-wal"))
    assert not list(tmp_path.rglob("*.sqlite3-shm"))


def test_clarification_responds_without_persistent_resume_state() -> None:
    workflow = BoundedCopilotWorkflow()
    runtime = ClarificationRuntime()
    response = workflow.run(
        message="Compare 192.0.2.10 with it",
        session_id="interrupt-session",
        ui_context=None,
        request_id="interrupt",
        trace_id="trace-interrupt",
        node_runtime=runtime,
    )
    assert response["answer"] == "Which second asset should be compared?"
    assert runtime.synthesis_calls == 0


def test_transient_node_failure_retries_once_and_logs(caplog: Any) -> None:
    runtime = FakeNodeRuntime(retry_route_once=True)
    with caplog.at_level(logging.INFO, logger="src.core.agent.workflow"):
        run_workflow(BoundedCopilotWorkflow(), runtime, "retry")
    assert runtime.calls.count("route") == 2
    assert "event=langgraph_node_retried" in caplog.text


def test_graph_compiles_without_a_checkpointer() -> None:
    workflow = BoundedCopilotWorkflow()
    assert workflow.graph is not None
    assert "checkpointer" not in workflow._config("no-checkpointer")
    assert getattr(workflow.graph, "checkpointer", None) is None


def test_concurrent_requests_keep_state_and_results_isolated() -> None:
    workflow = BoundedCopilotWorkflow()
    runtimes = {name: FakeNodeRuntime() for name in ("a", "b")}
    results: dict[str, dict[str, Any]] = {}

    def execute(name: str) -> None:
        results[name] = run_workflow(workflow, runtimes[name], name)

    threads = [Thread(target=execute, args=(name,)) for name in runtimes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results["a"]["session_id"] == "session-a"
    assert results["b"]["session_id"] == "session-b"
    assert runtimes["a"].memory_writes == runtimes["b"].memory_writes == 1


def test_every_executed_node_emits_start_completion_and_trace_record(caplog: Any) -> None:
    runtime = FakeNodeRuntime()
    workflow = BoundedCopilotWorkflow()
    with caplog.at_level(logging.INFO, logger="src.core.agent.workflow"):
        run_workflow(workflow, runtime, "logs")
    for node in runtime.calls:
        assert f"event=langgraph_node_started" in caplog.text
        assert f"node={node}" in caplog.text
    assert "event=langgraph_node_completed" in caplog.text
    assert "langgraph_checkpoint" not in caplog.text
