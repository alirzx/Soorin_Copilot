"""Offline tests for the active bounded Phase 2 investigation foundation."""

from __future__ import annotations

import logging
import json
import tempfile
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from src.core.agent.contracts import (
    CapabilitySpec,
    EvidenceFact,
    ExecutionPlan,
    PlanStep,
    RetryPolicy,
    TaskSpec,
    ToolResult,
)
from src.core.agent.events import WorkflowEventContext, WorkflowEventLogger
from src.core.agent.executor import CapabilityExecutor
from src.core.agent.plan_validator import PlanValidationError, PlanValidator
from src.core.agent.planner import BoundedPlanner, PlannerError
from src.core.agent.registry import CapabilityRegistry, _provider_result, build_capability_registry
from src.core.agent.reviewer import EvidenceReviewer
from src.core.agent.task_mapping import compile_direct_plan, compile_supplemental_plan
from src.core.agent.workflow import BoundedCopilotWorkflow
from src.core.context.models import AssetProfileProviderResult, DetectionProviderResult, ProviderProvenance
from src.config.settings import get_settings
from src.core.observability import EvidenceSnapshotWriter


class Input(BaseModel):
    entities: list[str] = Field(default_factory=list, max_length=2)
    depth: int = Field(default=0, ge=0, le=2)


def result(capability: str, entities=(), *, status="ok", limitations=()):
    return ToolResult(
        status=status,
        entities=tuple(entities),
        source_capability=capability,
        retrieved_at="now",
        freshness="current" if status == "ok" else "unknown",
        completeness="complete" if status == "ok" else "unknown",
        facts=(EvidenceFact(capability, "fixture", {"ok": True}),) if status == "ok" else (),
        limitations=tuple(limitations),
    )


def registry_with(handlers, *, cardinality=(1, 2), timeout=1.0):
    registry = CapabilityRegistry()
    for name, handler in handlers.items():
        registry.register(
            CapabilitySpec(
                name=name,
                version="1.0",
                description=f"Fixture {name}",
                input_schema=Input,
                output_schema=ToolResult,
                required_entity_cardinality=cardinality,
                read_only=True,
                timeout_seconds=timeout,
                retry_policy=RetryPolicy(),
                planner_visible=True,
                maximum_graph_depth=2,
            ),
            handler,
        )
    return registry


def task(*capabilities, entities=("192.0.2.10",), mode="direct"):
    return TaskSpec(
        request="Investigate the asset",
        intent="asset_investigation",
        scope="node_summary",
        direction="both",
        entities=tuple(entities),
        required_capabilities=tuple(capabilities),
        workflow_mode=mode,
        graph_depth=0,
        recommended_steps=6,
    )


class TestPlanValidation:
    def setup_method(self):
        self.registry = registry_with(
            {
                "a": lambda payload: result("a", payload.entities),
                "b": lambda payload: result("b", payload.entities),
            }
        )
        self.validator = PlanValidator(self.registry)

    def test_valid_plan_normalizes_arguments_and_is_a_dag(self):
        current = task("a", "b")
        plan = ExecutionPlan(
            current,
            (
                PlanStep("s1", "a", arguments={"entities": [current.entities[0]]}),
                PlanStep("s2", "b", depends_on=("s1",), arguments={"entities": [current.entities[0]]}),
            ),
            plan_id="p1",
        )
        validated = self.validator.validate(plan)
        assert validated.validated
        assert validated.steps[1].depends_on == ("s1",)

    @pytest.mark.parametrize(
        ("steps", "code"),
        [
            ((PlanStep("s1", "unknown", arguments={"entities": ["192.0.2.10"]}),), "unknown_capability"),
            (
                (
                    PlanStep("s1", "a", depends_on=("s2",), arguments={"entities": ["192.0.2.10"]}),
                    PlanStep("s2", "b", depends_on=("s1",), arguments={"entities": ["192.0.2.10"]}),
                ),
                "dependency_cycle",
            ),
            (
                (PlanStep("s1", "a", arguments={"entities": ["198.51.100.1"]}),),
                "entity_authority_violation",
            ),
            (
                (
                    PlanStep("s1", "a", arguments={"entities": ["192.0.2.10"]}),
                    PlanStep("s2", "a", arguments={"entities": ["192.0.2.10"]}),
                ),
                "duplicate_capability_call",
            ),
        ],
    )
    def test_rejects_unsafe_or_structurally_invalid_plans(self, steps, code):
        with pytest.raises(PlanValidationError) as captured:
            self.validator.validate(ExecutionPlan(task("a"), steps, plan_id="p1"))
        assert captured.value.code == code

    def test_rejects_excessive_graph_depth_and_call_count(self):
        current = replace(task("a"), graph_depth=2)
        shallow_registry = registry_with(
            {"graph.a": lambda payload: result("graph.a", payload.entities)},
        )
        shallow_registry._specs["graph.a"] = replace(shallow_registry.get("graph.a"), maximum_graph_depth=1)
        with pytest.raises(PlanValidationError, match="depth"):
            PlanValidator(shallow_registry).validate(
                ExecutionPlan(current, (PlanStep("s1", "graph.a", arguments={"entities": [current.entities[0]], "depth": 2}),))
            )

    def test_reviewer_supplemental_arguments_are_validated_before_execution(self):
        current = task("a")
        proposed = compile_supplemental_plan(
            current,
            "a",
            {"entities": ["198.51.100.99"]},
            plan_id="p1",
        )

        assert not proposed.validated
        with pytest.raises(PlanValidationError) as captured:
            self.validator.validate(proposed)
        assert captured.value.code == "entity_authority_violation"

    def test_knowledge_second_call_requires_distinct_purpose_and_query(self):
        registry = registry_with(
            {"knowledge.search": lambda payload: result("knowledge.search")},
            cardinality=(0, 0),
        )
        current = task("knowledge.search", entities=())
        duplicate = ExecutionPlan(
            current,
            (
                PlanStep("k1", "knowledge.search", arguments={"query": "Kerberos risks", "purpose": "interpret_evidence"}),
                PlanStep("k2", "knowledge.search", arguments={"query": "Kerberos risks!", "purpose": "response_actions"}),
            ),
        )
        with pytest.raises(PlanValidationError) as captured:
            PlanValidator(registry).validate(duplicate)
        assert captured.value.code == "duplicate_knowledge_search"

        distinct = replace(
            duplicate,
            steps=(
                duplicate.steps[0],
                replace(duplicate.steps[1], arguments={"query": "Containment actions for ticket abuse", "purpose": "response_actions"}),
            ),
        )
        assert PlanValidator(registry).validate(distinct).validated


class TestPlanner:
    class FakeLLM:
        def __init__(self, outputs):
            self.outputs = list(outputs)
            self.purposes = []

        def chat(self, messages, **kwargs):
            assert messages[0]["role"] == "system"
            self.purposes.append(kwargs["purpose"])
            return SimpleNamespace(text=self.outputs.pop(0))

    @staticmethod
    def capability(name="a"):
        return CapabilitySpec(
            name=name,
            version="1.0",
            description="fixture",
            input_schema=Input,
            output_schema=ToolResult,
            required_entity_cardinality=(1, 1),
            read_only=True,
            timeout_seconds=1,
            retry_policy=RetryPolicy(),
            planner_visible=True,
            maximum_graph_depth=2,
        )

    def test_valid_multi_step_plan_uses_structured_output(self):
        llm = self.FakeLLM([
            '{"goal":"check","target_entities":["192.0.2.10"],"steps":[{"step_id":"s1","capability":"a","arguments":{"entities":["192.0.2.10"]},"depends_on":[],"required":true,"expected_evidence":"fixture"}],"stop_condition":"done"}'
        ])
        plan = BoundedPlanner(llm).plan(task("a", mode="multi_step"), (self.capability(),), request_id="r1")
        assert plan.source == "llm"
        assert plan.planner_called
        assert llm.purposes == ["planner"]

    def test_malformed_output_fails_after_one_planner_call(self):
        llm = self.FakeLLM([
            "not json",
            '{"goal":"check","target_entities":["192.0.2.10"],"steps":[],"stop_condition":"done"}',
        ])
        planner = BoundedPlanner(llm)
        with pytest.raises(PlannerError) as captured:
            planner.plan(task("a", mode="multi_step"), (self.capability(),), request_id="r1")
        assert captured.value.code == "planner_schema_invalid"
        assert not planner.last_repair_used
        assert llm.purposes == ["planner"]

    def test_extra_prose_fails_without_second_planner_call(self):
        valid = (
            '{"goal":"check","target_entities":["192.0.2.10"],'
            '"steps":[{"step_id":"s1","capability":"a",'
            '"arguments":{"entities":["192.0.2.10"],"metadata":{"nested":true}},'
            '"depends_on":[],"required":true,"expected_evidence":"fixture"}],'
            '"stop_condition":"done"}'
        )
        llm = self.FakeLLM([f"Planner result: {valid}", valid])

        with pytest.raises(PlannerError) as captured:
            BoundedPlanner(llm).plan(
                task("a", mode="multi_step"),
                (self.capability(),),
                request_id="r1",
            )

        assert captured.value.code == "planner_schema_invalid"
        assert llm.purposes == ["planner"]

    def test_direct_task_never_calls_planner(self):
        llm = self.FakeLLM([])
        with pytest.raises(PlannerError) as captured:
            BoundedPlanner(llm).plan(task("a"), (self.capability(),), request_id="r1")
        assert captured.value.code == "planner_not_required"
        assert llm.purposes == []


class TestExecutor:
    def test_independent_steps_overlap_and_dependencies_wait(self):
        timeline = []

        def slow(name):
            def run(payload):
                timeline.append((name, "start", time.perf_counter()))
                time.sleep(0.12)
                timeline.append((name, "end", time.perf_counter()))
                return result(name, payload.entities)
            return run

        registry = registry_with({"a": slow("a"), "b": slow("b"), "c": slow("c")})
        current = task("a", "b", "c")
        plan = ExecutionPlan(
            current,
            (
                PlanStep("s1", "a", arguments={"entities": [current.entities[0]]}),
                PlanStep("s2", "b", arguments={"entities": [current.entities[0]]}),
                PlanStep("s3", "c", depends_on=("s1", "s2"), arguments={"entities": [current.entities[0]]}),
            ),
            validated=True,
            plan_id="p1",
        )
        started = time.perf_counter()
        results = CapabilityExecutor(registry, max_concurrency=3).execute(plan)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.33
        starts = {name: stamp for name, event, stamp in timeline if event == "start"}
        ends = {name: stamp for name, event, stamp in timeline if event == "end"}
        assert starts["c"] >= max(ends["a"], ends["b"])
        assert [item.status for item in results] == ["ok", "ok", "ok"]

    def test_partial_results_and_safe_failures_are_preserved(self):
        def broken(payload):
            raise RuntimeError("private provider detail")

        registry = registry_with(
            {
                "a": lambda payload: result("a", payload.entities, status="partial", limitations=("bounded",)),
                "b": broken,
            }
        )
        current = task("a", "b")
        plan = PlanValidator(registry).validate(
            ExecutionPlan(
                current,
                (
                    PlanStep("s1", "a", arguments={"entities": [current.entities[0]]}),
                    PlanStep("s2", "b", arguments={"entities": [current.entities[0]]}),
                ),
                plan_id="p1",
            )
        )
        results = CapabilityExecutor(registry).execute(plan)
        assert {item.status for item in results} == {"partial", "unavailable"}
        assert any(item.safe_error_code == "RuntimeError" for item in results)

    def test_product_concurrency_group_is_serialized(self):
        active = 0
        max_active = 0
        guard = threading.Lock()

        def product_call(name):
            def run(payload):
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with guard:
                    active -= 1
                return result(name, payload.entities)

            return run

        registry = registry_with({"profile": product_call("profile"), "detection": product_call("detection")})
        registry._specs["profile"] = replace(registry.get("profile"), concurrency_group="product")
        registry._specs["detection"] = replace(registry.get("detection"), concurrency_group="product")
        current = task("profile", "detection")
        plan = PlanValidator(registry).validate(
            ExecutionPlan(
                current,
                (
                    PlanStep("s1", "profile", arguments={"entities": [current.entities[0]]}),
                    PlanStep("s2", "detection", arguments={"entities": [current.entities[0]]}),
                ),
                plan_id="p1",
            )
        )

        results = CapabilityExecutor(registry, max_concurrency=2).execute(plan)

        assert [item.status for item in results] == ["ok", "ok"]
        assert max_active == 1

    def test_cancellation_prevents_calls(self):
        calls = []
        registry = registry_with({"a": lambda payload: calls.append(payload) or result("a", payload.entities)})
        plan = PlanValidator(registry).validate(compile_direct_plan(task("a"), plan_id="p1"))
        cancellation = threading.Event()
        cancellation.set()
        results = CapabilityExecutor(registry).execute(plan, cancellation=cancellation)
        assert calls == []
        assert results[0].safe_error_code == "cancelled"

    def test_failed_dependency_skips_optional_downstream_step(self):
        downstream_calls = []

        def broken(payload):
            raise RuntimeError("provider failed")

        registry = registry_with(
            {
                "a": broken,
                "b": lambda payload: downstream_calls.append(payload) or result("b", payload.entities),
            }
        )
        current = task("a", "b")
        plan = PlanValidator(registry).validate(
            ExecutionPlan(
                current,
                (
                    PlanStep("s1", "a", arguments={"entities": [current.entities[0]]}),
                    PlanStep(
                        "s2",
                        "b",
                        depends_on=("s1",),
                        arguments={"entities": [current.entities[0]]},
                        requirement="optional",
                    ),
                ),
                plan_id="p1",
            )
        )

        results = CapabilityExecutor(registry).execute(plan)

        assert downstream_calls == []
        assert results[-1].safe_error_code == "dependency_failed"


class TestEvidenceAndReview:
    def test_product_conversion_preserves_raw_payload_and_metadata(self):
        raw = {"asset": {"hostname": "dc-1"}, "signals": [1, 2]}
        provider = AssetProfileProviderResult(
            provider="asset_profile",
            status="available",
            ip="192.0.2.10",
            raw_payload=raw,
            provenance=ProviderProvenance("product_asset_profile", "available"),
            cache_hit=True,
            fetched_at="now",
            latency_ms=7,
        )
        converted = _provider_result("asset.get_profile", ("192.0.2.10",), provider)
        assert converted.raw_payload is raw
        assert converted.provider_result is provider
        assert converted.cache_status == "hit"
        assert converted.latency_ms == 7

    def test_unknown_provider_status_fails_closed(self):
        provider = SimpleNamespace(
            status="mystery",
            provider="graph",
            context={"target_ip": "192.0.2.10"},
            limitations=(),
            latency_ms=0,
        )

        converted = _provider_result("graph.get_summary", ("192.0.2.10",), provider)

        assert converted.status == "invalid"
        assert converted.completeness == "unknown"
        assert converted.safe_error_code == "invalid_provider_status"

    def test_graph_completeness_prioritizes_the_requested_scope(self):
        summary = SimpleNamespace(
            status="available",
            provider="graph",
            context={
                "retrieval_complete": False,
                "requested_scope_complete": True,
                "complete_for_user_request": True,
                "serialized_context_complete_for_retrieved_subset": True,
            },
            limitations=(),
        )
        converted = _provider_result("graph.get_summary", ("192.0.2.10",), summary)
        assert converted.completeness == "complete"
        assert not converted.truncated
        assert any("Broader graph retrieval" in item for item in converted.limitations)

        incomplete = _provider_result(
            "graph.get_neighbors",
            ("192.0.2.10",),
            SimpleNamespace(
                **{**vars(summary), "context": {**summary.context, "complete_for_user_request": False}}
            ),
        )
        assert incomplete.completeness == "partial"
        assert incomplete.truncated

        serialization = _provider_result(
            "graph.get_summary",
            ("192.0.2.10",),
            SimpleNamespace(
                **{
                    **vars(summary),
                    "context": {
                        **summary.context,
                        "serialized_context_complete_for_retrieved_subset": False,
                    },
                }
            ),
        )
        assert serialization.completeness == "partial"

    def test_complete_product_payload_does_not_request_supplemental_projection(self):
        calls = []
        settings = SimpleNamespace(
            detection_cache_enabled=False,
            product_read_timeout_seconds=1,
            rag_qdrant_timeout_seconds=1,
            rag_top_k=3,
            agent_request_timeout_seconds=1,
            graph_full_neighbors_hard_max=10,
        )

        class Provider:
            def __init__(self, result_type, name):
                self.settings = settings
                self.result_type = result_type
                self.name = name

            def fetch(self, ip, request_id, session_id=""):
                calls.append((self.name, ip, request_id, session_id))
                return self.result_type(
                    provider=self.name,
                    status="available",
                    ip=ip,
                    raw_payload={
                        "identity": {"hostname": "dc-1", "role": "server"},
                        "risk": {"score": 7},
                        "services": ["kerberos", "ldap"],
                        "events": [{"rule": "fixture-rule", "token": "must-not-appear"}],
                    },
                    provenance=ProviderProvenance(f"fixture_{self.name}", "available"),
                )

        profile = Provider(AssetProfileProviderResult, "asset_profile")
        detection = Provider(DetectionProviderResult, "detection")
        registry = build_capability_registry(
            asset_profile_provider=profile,
            detection_provider=detection,
            graph_provider=SimpleNamespace(settings=settings),
            knowledge_service=SimpleNamespace(settings=settings),
        )
        base = {"entities": ["192.0.2.10"], "request_id": "r1", "session_id": "s1"}
        first = registry.execute(
            "asset.get_profile",
            {**base, "views": ["overview", "identity_role"], "purpose": "summary"},
        )
        deep_task = TaskSpec(
            request="Prepare a deep report",
            intent="asset_investigation",
            scope="node_summary",
            direction="both",
            entities=("192.0.2.10",),
            required_capabilities=("asset.get_profile",),
            detail_level="deep",
        )
        decision = EvidenceReviewer().review(deep_task, [first], allow_supplemental=True)
        assert not decision.supplemental_allowed
        assert decision.next_capability is None
        assert len(calls) == 1
        assert first.selected_views == ("overview", "identity_role")
        assert first.view_payload == first.raw_payload
        assert "must-not-appear" in json.dumps(first.view_payload)
        assert not first.projection_truncated
        assert first.projection_omitted_count == 0

    def test_task_steps_are_recommendations_not_plan_security_limits(self):
        current = task("a")
        assert current.recommended_steps == 6
        assert current.max_steps == current.recommended_steps

    def test_reviewer_authorizes_one_missing_capability_and_then_limits(self):
        current = task("a", "b")
        reviewer = EvidenceReviewer()
        first = reviewer.review(current, [result("a", current.entities)], allow_supplemental=True)
        assert first.outcome == "missing_required_evidence"
        assert first.supplemental_allowed
        assert first.next_capability == "b"
        final = reviewer.review(
            current,
            [result("a", current.entities), result("b", current.entities, status="partial", limitations=("bounded",))],
            allow_supplemental=False,
        )
        assert final.outcome == "answer_with_limitations"
        assert not final.supplemental_allowed

    def test_evidence_pack_contains_ids_coverage_and_review(self):
        current = task("a")
        plan = ExecutionPlan(current, (), plan_id="p1", goal="goal")
        reviewer = EvidenceReviewer()
        decision = reviewer.review(current, [result("a", current.entities)])
        pack = reviewer.build_pack(
            current,
            [result("a", current.entities)],
            plan=plan,
            request_id="r1",
            trace_id="t1",
            review=decision,
        )
        assert pack.request_id == "r1"
        assert pack.trace_id == "t1"
        assert pack.provider_coverage == {"a": "ok"}
        assert pack.review_outcome == "sufficient"


class TestWorkflowAndLogging:
    @pytest.mark.parametrize("force_compatibility_runner", [False, True])
    def test_exactly_one_typed_executor_runs_in_each_workflow_runtime(self, force_compatibility_runner):
        calls = []

        def typed(message, session_id, **kwargs):
            calls.append("typed")
            return {"session_id": session_id, "answer": "ok", "provider": "fake", "model": "fake"}

        def compatibility(*args, **kwargs):
            calls.append("compatibility")
            raise AssertionError("compatibility path should not run")

        workflow = BoundedCopilotWorkflow()
        if force_compatibility_runner:
            workflow._state_graph_type = None
        elif workflow.runtime != "langgraph":
            pytest.skip("LangGraph is not installed in this environment.")

        response = workflow.run(
            message="hello",
            session_id="s1",
            ui_context=None,
            request_id="r1",
            stream_sink=None,
            typed_executor=typed,
            direct_executor=compatibility,
        )
        assert response["answer"] == "ok"
        assert calls == ["typed"]

    @pytest.mark.parametrize("force_compatibility_runner", [False, True])
    def test_compatibility_alias_runs_once_when_no_typed_executor_is_supplied(self, force_compatibility_runner):
        calls = []

        def compatibility(message, session_id, **kwargs):
            calls.append((message, kwargs.get("trace_id")))
            return {"session_id": session_id, "answer": "ok", "provider": "fake", "model": "fake"}

        workflow = BoundedCopilotWorkflow()
        if force_compatibility_runner:
            workflow._state_graph_type = None
        elif workflow.runtime != "langgraph":
            pytest.skip("LangGraph is not installed in this environment.")

        response = workflow.run(
            message="hello",
            session_id="s1",
            ui_context=None,
            request_id="r1",
            stream_sink=None,
            typed_executor=None,
            direct_executor=compatibility,
        )

        assert response["answer"] == "ok"
        assert len(calls) == 1
        assert calls[0][0] == "hello"
        assert calls[0][1]

    def test_event_logger_propagates_ids_and_drops_secret_fields(self, caplog):
        events = WorkflowEventLogger(
            logging.getLogger("phase2-test"),
            WorkflowEventContext("r1", "t1", "s1"),
        )
        with caplog.at_level(logging.INFO, logger="phase2-test"):
            events.emit(
                "step_completed",
                plan_id="p1",
                step_id="s1",
                status="ok",
                latency_ms=3,
                api_key="must-not-appear",
                raw_payload={"secret": "must-not-appear"},
            )
        output = caplog.text
        assert "request_id=r1" in output
        assert "trace_id=t1" in output
        assert "plan_id=p1" in output
        assert "must-not-appear" not in output

    def test_evidence_snapshot_is_bounded_private_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                get_settings(),
                evidence_snapshot_enabled=True,
                evidence_snapshot_mode="redacted",
                evidence_snapshot_root=directory,
                evidence_snapshot_max_bytes=100_000,
            )
            path = EvidenceSnapshotWriter(settings).write(
                "request-1",
                {
                    "tool-results": {
                        "raw_payload": {"hostname": "dc-1", "api_key": "must-not-appear"},
                        "reasoning_content": "must-not-appear",
                    }
                },
            )
            assert path is not None
            snapshot = next(Path(path).glob("*.json"))
            assert snapshot.stat().st_mode & 0o777 == 0o600
            content = snapshot.read_text(encoding="utf-8")
            assert "dc-1" not in content
            assert "must-not-appear" not in content
            assert json.loads(content)["raw_payload"] == "[OMITTED]"
