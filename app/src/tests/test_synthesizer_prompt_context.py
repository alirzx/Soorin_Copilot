"""Offline regressions for deterministic Synthesizer prompt context."""

from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field

import src.config.settings as settings_module
from src.core.agent.contracts import (
    CapabilitySpec,
    ExecutionPlan,
    PlanStep,
    PostSearchEnrichmentSummary,
    RequestConstraints,
    RetryPolicy,
    TaskSpec,
    ToolResult,
)
from src.core.agent.executor import CapabilityExecutor
from src.core.agent.plan_validator import PlanValidator
from src.core.agent.registry import CapabilityRegistry
from src.core.agent.task_mapping import compile_direct_plan, task_spec_from_route
import src.core.context.synthesizer_prompt as synthesizer_prompt_module
from src.core.context.synthesizer_prompt import PromptModuleRegistry, SynthesizerPromptBuilder
from src.core.copilot.service import CopilotService
from src.core.llm.providers.base import LLMProviderResult
from src.core.memory.retrieval import LongTermMemorySelection
from src.core.memory.retrieval import LazyCrossEncoderReranker
from src.core.memory.episodes import MemoryContextKey, MemoryContextPackage
from src.core.memory.store import MemoryStore
from src.core.graph.structured import StructuredQuerySpec


def _task(**overrides) -> TaskSpec:
    values = {
        "request": "Investigate 192.0.2.10",
        "intent": "asset_investigation",
        "scope": "node_summary",
        "direction": "both",
        "entities": ("192.0.2.10",),
        "required_capabilities": ("graph.get_summary",),
    }
    values.update(overrides)
    return TaskSpec(**values)


def _route(*, entities: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        materialized_entities=entities,
        scope="node_summary",
        direction="both",
        depth=0,
        intent="asset_investigation",
        use_asset_profile=True,
        use_detection=True,
        use_graph=True,
        use_knowledge=True,
        requires_multiple_entities=False,
        relationship_mode="none",
        decision_source="semantic_router",
        matched_signals=(),
        followup_detected=True,
    )


def test_new_static_prompt_is_default_and_legacy_has_no_scope_refusal(monkeypatch, tmp_path: Path) -> None:
    legacy = Path("app/prompts/system_prompt.md").read_text(encoding="utf-8")
    static = Path("app/prompts/synthesizer/synthesizer_static_prompt.md").read_text(encoding="utf-8")
    assert static.strip()
    assert "I can assist only with cybersecurity" not in legacy
    assert "I can assist only with cybersecurity" not in static

    monkeypatch.setattr(settings_module, "ENV_PATH", tmp_path / "missing.env")
    monkeypatch.setattr(settings_module, "LEGACY_ENV_PATH", tmp_path / "missing-legacy.env")
    monkeypatch.delenv("SOORIN_SYSTEM_PROMPT_PATH", raising=False)
    settings_module.get_settings.cache_clear()
    try:
        assert settings_module.get_settings().system_prompt_path == "app/prompts/synthesizer/synthesizer_static_prompt.md"
    finally:
        settings_module.get_settings.cache_clear()


def test_static_prompt_uses_user_facing_memory_language() -> None:
    static = Path("app/prompts/synthesizer/synthesizer_static_prompt.md").read_text(encoding="utf-8")

    assert "our earlier discussion" in static
    assert "do not expose internal workflow, storage, retrieval" in static.casefold()
    assert "Translate evidence limitations into honest plain language" in static


@pytest.mark.parametrize(
    "user_request",
    (
        "Summarize the SOC triage implications",
        "Explain the NOC reliability impact",
        "Assess this NDR network behavior",
        "Review Active Directory and Kerberos exposure",
        "Explain the DNS routing pattern",
        "Summarize SIEM coverage gaps",
        "Describe the asset inventory server roles",
        "Relate this threat intelligence to detection engineering",
    ),
)
def test_synthesizer_trusts_validated_domain_route_for_varied_wording(user_request: str) -> None:
    static = Path("app/prompts/synthesizer/synthesizer_static_prompt.md").read_text(
        encoding="utf-8"
    )
    task = _task(request=user_request)
    rendered = SynthesizerPromptBuilder().render_messages(
        static_core=static,
        context=SynthesizerPromptBuilder().build_context(task, ()),
        dynamic_evidence="",
        history=[],
        user_message=user_request,
    )

    assert "I can assist only with cybersecurity" not in rendered.messages[0]["content"]
    assert rendered.messages[-1] == {"role": "user", "content": user_request}


def test_missing_new_prompt_uses_unchanged_legacy_compatibility_fallback(tmp_path: Path) -> None:
    service = object.__new__(CopilotService)
    service.settings = SimpleNamespace(system_prompt_path=str(tmp_path / "missing.md"))
    loaded = service._load_system_prompt()  # noqa: SLF001 - focused compatibility boundary.
    assert loaded == Path("app/prompts/system_prompt.md").read_text(encoding="utf-8").strip()


@pytest.mark.parametrize(
    "user_text",
    (
        "Before making any live provider calls, tell me which asset and investigation context you remember from before the restart. Clearly distinguish conversation memory, episodic memory, and long-term memory.",
        "Continue with the same asset we were investigating before the restart. Summarize what you remember about that investigation without refreshing any evidence.",
        "What do you know about asset 192.168.0.149 from prior investigations or long-term memory? Do not use live Product, Graph, Detection, or Knowledge evidence.",
    ),
)
def test_memory_only_requests_compile_to_valid_zero_tool_direct_plan(user_text: str) -> None:
    entities = ("192.168.0.149",) if "192.168.0.149" in user_text else ("192.0.2.10",)
    task = task_spec_from_route(_route(entities=entities), user_text)
    plan = compile_direct_plan(task)
    validated = PlanValidator(CapabilityRegistry()).validate(plan)

    assert task.intent == "memory_recall"
    assert task.evidence_mode == "memory_only"
    assert task.temporal_mode == "historical"
    assert task.workflow_mode == "direct"
    assert task.required_capabilities == ()
    assert task.optional_capabilities == ()
    assert validated.steps == ()


def test_memory_only_workflow_bypasses_router_and_calls_only_synthesizer() -> None:
    class OfflineLLM:
        def __init__(self) -> None:
            self.calls = []
            self.responses = ["No active validated long-term memory is available for this asset."]

        def chat(self, messages, **kwargs):
            self.calls.append({"messages": messages, **kwargs})
            return LLMProviderResult(
                text=self.responses.pop(0),
                provider="fake",
                model="fake",
                deployment="fake",
            )

    class NoLiveProduct:
        def _unexpected(self, name):
            raise AssertionError(f"live_product_method_called:{name}")

        def get_asset_profile(self, *_args, **_kwargs):
            return self._unexpected("get_asset_profile")

        def get_asset_detection(self, *_args, **_kwargs):
            return self._unexpected("get_asset_detection")

        def post_json(self, *_args, **_kwargs):
            return self._unexpected("post_json")

    configured = replace(
        settings_module.get_settings(),
        system_prompt_path="app/prompts/synthesizer/synthesizer_static_prompt.md",
        llm_usage_reporting_enabled=False,
        planner_enabled=True,
        long_term_memory_enabled=False,
    )
    llm = OfflineLLM()
    service = CopilotService(
        configured,
        llm,  # type: ignore[arg-type]
        MemoryStore(4),
        product_client=NoLiveProduct(),  # type: ignore[arg-type]
    )
    response = service.chat(
        "What do you know about asset 192.168.0.149 from prior investigations or long-term memory? Do not use live Product, Graph, Detection, or Knowledge evidence.",
        session_id="memory-only-session",
        request_id="memory-only-request",
    )

    assert response["answer"].startswith("No active validated long-term memory")
    assert [call["purpose"] for call in llm.calls] == ["chat"]
    final_system = llm.calls[-1]["messages"][0]["content"]
    assert '"evidence_mode":"memory_only"' in final_system
    assert "Users may legitimately narrow scope" in final_system


def test_prompt_builder_selects_stable_relevant_modules_and_message_order() -> None:
    builder = SynthesizerPromptBuilder()
    task = _task(response_depth="brief")
    context = builder.build_context(task, ())
    rendered = builder.render_messages(
        static_core="STATIC",
        context=context,
        dynamic_evidence="EVIDENCE",
        history=[{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}],
        user_message="current",
    )
    repeated = builder.render_messages(
        static_core="STATIC",
        context=context,
        dynamic_evidence="EVIDENCE",
        history=[{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}],
        user_message="current",
    )

    assert rendered == repeated
    assert rendered.selected_module_names[:3] == (
        "task.graph_summary",
        "temporal.current",
        "evidence_mode.normal",
    )
    assert "evidence.graph" not in rendered.selected_module_names
    assert "evidence.profile" not in rendered.selected_module_names
    assert [message["role"] for message in rendered.messages] == [
        "system", "user", "user", "assistant", "user"
    ]
    assert rendered.messages[0]["content"].startswith("STATIC\n\n[SOORIN SYNTHESIZER TASK CONTRACT]")
    assert rendered.messages[1]["content"] == (
        "[BEGIN UNTRUSTED EVIDENCE CONTEXT]\n"
        "EVIDENCE\n"
        "[END UNTRUSTED EVIDENCE CONTEXT]"
    )


def test_grouped_prompt_registry_loads_required_sections_and_rejects_duplicate_headers() -> None:
    registry = PromptModuleRegistry()

    assert "resolved entity binding" in registry.get("tasks", "asset_investigation")
    with pytest.raises(ValueError, match="duplicate"):
        PromptModuleRegistry._parse(Path("duplicate.md"), "# current\nA\n# current\nB")


def test_grouped_prompt_registry_rejects_missing_required_section(tmp_path: Path) -> None:
    module_dir = tmp_path / "synthesizer"
    shutil.copytree("app/prompts/synthesizer", module_dir)
    (module_dir / "tasks.md").write_text("# asset_investigation\nOnly one section.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="synthesizer_prompt_module_missing:tasks"):
        PromptModuleRegistry(module_dir)


def test_prompt_builder_selects_externalized_modules_for_current_execution() -> None:
    builder = SynthesizerPromptBuilder()
    task = _task(
        request="Re-check current classification against the previously validated state.",
        required_capabilities=("asset.get_detection",),
        evidence_mode="current_verification",
        temporal_mode="compare_previous_current",
    )
    result = ToolResult("ok", task.entities, "asset.get_detection", "now", "current", "complete")
    context = builder.build_context(task, (result,), delta_contexts=({"delta": {}},))
    contract, modules = builder.render_contract(context)

    assert "execution.current_retrieval_completed" in modules
    assert "execution.baseline_available" in modules
    assert "temporal.compare_previous_current" in modules
    assert not hasattr(synthesizer_prompt_module, "TASK_MODULES")
    assert "Live retrieval actually occurred" in contract
    assert "Hide internal orchestration" in contract


def test_rendered_contract_uses_internal_term_hiding_and_historical_modules() -> None:
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(
        _task(
            intent="memory_recall",
            required_capabilities=(),
            evidence_mode="memory_only",
            temporal_mode="historical",
        ),
        (),
    )
    contract, modules = builder.render_contract(context)

    assert "task.memory_recall" in modules
    assert "temporal.historical" in modules
    assert "memory.historical_memory_only" in modules
    assert "ordinary user-facing responses" in contract
    assert "never claim" in contract.casefold()
    assert "exhaustive" in contract.casefold()
    assert "absence" in contract.casefold()


def test_bounded_thread_recall_coverage_is_machine_readable_in_synth_contract() -> None:
    package = MemoryContextPackage(
        thread_recall_requested=True,
        thread_recall_complete=False,
        sources_considered=("relevant_turns", "episodes"),
        omitted=("older_relevant_turn_over_budget",),
    )
    selection = LongTermMemorySelection(
        status="available",
        inventory_available=False,
        limitations=("long_term_memory_inventory_unavailable",),
    )
    context = SynthesizerPromptBuilder().build_context(
        _task(
            intent="memory_recall",
            required_capabilities=(),
            evidence_mode="memory_only",
            temporal_mode="historical",
        ),
        (),
        snapshot=SimpleNamespace(memory_context=package, recent_message_count=1),
        long_term_selection=selection,
    )
    contract, _ = SynthesizerPromptBuilder().render_contract(context)
    metadata = json.loads(
        contract.split("[SOORIN SYNTHESIZER TASK CONTRACT]\n", 1)[1].split(
            "\n[SELECTED INSTRUCTIONS]", 1
        )[0]
    )

    assert metadata["memory"]["thread_recall_requested"] is True
    assert metadata["memory"]["thread_recall_complete"] is False
    assert metadata["memory"]["memory_context_truncated"] is True
    assert metadata["memory"]["inventory_available"] is False
    assert metadata["memory"]["sources_omitted"] == ["older_relevant_turn_over_budget"]


def test_no_live_refresh_preserves_underlying_task_identity() -> None:
    task = task_spec_from_route(
        _route(entities=("192.0.2.10",)),
        "Analyze this asset using already supplied evidence, but do not refresh it or use live evidence.",
    )

    assert task.intent == "asset_investigation"
    assert task.scope == "node_summary"
    assert task.evidence_mode == "no_live_refresh"
    assert task.required_capabilities == ()


def test_no_live_refresh_preserves_comparison_scope() -> None:
    route = _route(entities=("192.0.2.10", "192.0.2.11"))
    route.scope = "multi_entity_comparison"
    route.requires_multiple_entities = True
    route.relationship_mode = "compare"

    task = task_spec_from_route(
        route,
        "Compare these assets using the already supplied evidence; do not use live refresh.",
    )

    assert task.intent == "asset_investigation"
    assert task.scope == "multi_entity_comparison"
    assert task.evidence_mode == "no_live_refresh"
    assert task.entities == ("192.0.2.10", "192.0.2.11")


def test_provider_state_is_partial_when_one_graph_capability_is_unavailable() -> None:
    builder = SynthesizerPromptBuilder()
    results = (
        ToolResult("ok", ("192.0.2.10",), "graph.get_summary", "now", "current", "complete"),
        ToolResult("unavailable", ("192.0.2.10",), "graph.get_neighbors", "now", "unknown", "unknown"),
    )

    assert builder.build_context(_task(), results).graph.status == "partial"


def test_multi_source_investigation_is_not_collapsed_to_detection_explanation() -> None:
    context = SynthesizerPromptBuilder().build_context(
        _task(
            required_capabilities=(
                "asset.get_profile",
                "asset.get_detection",
                "graph.get_summary",
            )
        ),
        (),
    )
    assert context.task_category == "asset_investigation"
    assert {"defender_ir", "noc_operational"} <= set(context.selected_analytical_lenses)


def test_detection_focused_task_is_detection_explanation() -> None:
    context = SynthesizerPromptBuilder().build_context(
        _task(required_capabilities=("asset.get_detection",)),
        (),
    )
    assert context.task_category == "detection_explanation"


def test_reranker_loader_is_pinned_to_cpu_without_loading_a_real_model(monkeypatch) -> None:
    loader = MagicMock(return_value=object())
    module = ModuleType("sentence_transformers")
    module.CrossEncoder = loader
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr("src.core.memory.retrieval.importlib.util.find_spec", lambda _name: object())

    reranker = LazyCrossEncoderReranker("BAAI/bge-reranker-v2-m3")
    reranker._ensure_loaded()  # noqa: SLF001 - validates loader construction only.

    loader.assert_called_once_with(
        "BAAI/bge-reranker-v2-m3",
        device="cpu",
        local_files_only=True,
    )


def test_prompt_builder_validates_required_inputs() -> None:
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(_task(), ())
    with pytest.raises(ValueError, match="static_core"):
        builder.render_messages(
            static_core="",
            context=context,
            dynamic_evidence="",
            history=[],
            user_message="hello",
        )
    with pytest.raises(ValueError, match="user_message"):
        builder.render_messages(
            static_core="static",
            context=context,
            dynamic_evidence="",
            history=[],
            user_message="",
        )


@pytest.mark.parametrize(
    ("selection", "expected"),
    (
        (LongTermMemorySelection(status="empty"), (0, 0, 0)),
        (
            LongTermMemorySelection(
                status="empty",
                candidate_record_count=2,
                active_record_count=0,
                selected_count=0,
            ),
            (2, 0, 0),
        ),
        (
            LongTermMemorySelection(
                status="ok",
                candidate_record_count=1,
                active_record_count=3,
                selected_count=1,
            ),
            (1, 3, 1),
        ),
    ),
)
def test_ltm_candidate_active_and_selected_states_are_distinct(selection, expected) -> None:
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(
        _task(evidence_mode="memory_only", temporal_mode="historical", intent="memory_recall", required_capabilities=()),
        (),
        long_term_selection=selection,
    )
    assert (
        context.memory.ltm_candidate_count,
        context.memory.ltm_active_count,
        context.memory.ltm_selected_count,
    ) == expected
    assert context.memory.ltm_status == selection.status
    contract, modules = builder.render_contract(context)
    if expected[2]:
        assert "memory.ltm_available" in modules
        assert "memory.no_active_ltm" not in modules
    else:
        assert "memory.no_active_ltm" in modules
    assert f'"ltm_candidate_count":{expected[0]}' in contract


def test_raw_selected_turn_marks_working_memory_available_without_summary() -> None:
    snapshot = SimpleNamespace(
        recent_message_count=2,
        memory_context=SimpleNamespace(
            working_summary="",
            relevant_turns=(SimpleNamespace(),),
            episode_summaries=(),
        ),
    )
    context = SynthesizerPromptBuilder().build_context(_task(), (), snapshot=snapshot)
    assert context.memory.working_available
    assert context.memory.ltm_status == "not_supplied"


def test_delta_and_bounded_negative_rules_are_explicit_and_fail_closed() -> None:
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(_task(), ())
    contract, _ = builder.render_contract(context)
    static = Path("app/prompts/synthesizer/synthesizer_static_prompt.md").read_text(encoding="utf-8")

    assert context.current_vs_historical_relationship == "compatible_baseline_unavailable"
    assert not context.deterministic_delta_available
    assert "Use new/changed/appeared/disappeared only" in contract
    assert "not observed never means categorically absent" in contract
    assert "omissions/truncation are not negative findings" in static
    assert "compatible_baseline_unavailable" in contract


def test_mixed_current_memory_write_contract_is_grounded_in_execution_facts() -> None:
    task = _task(
        request="Check its current state and remember that my name is X.",
        required_capabilities=("asset.get_profile", "asset.get_detection"),
        evidence_mode="current_verification",
    )
    results = (
        ToolResult(
            "ok", task.entities, "asset.get_profile", "2026-08-16T10:00:00+00:00",
            "current", "complete", provider="asset_profile", context_included=True,
        ),
        ToolResult(
            "ok", task.entities, "asset.get_detection", "2026-08-16T10:00:01+00:00",
            "current", "complete", provider="detection", context_included=True,
        ),
    )
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(
        task,
        results,
        request_constraints=RequestConstraints(
            allow_live=True,
            require_current=True,
            memory_write=True,
        ),
        accepted_working_fact_count=1,
    )
    contract, modules = builder.render_contract(context)

    assert context.execution.live_retrieval_performed
    assert context.execution.capability_call_count == 2
    assert context.execution.successful_current_evidence_count == 2
    assert "execution.current_retrieval_completed" in modules
    assert "execution.working_memory_write" in modules
    assert "Never claim that live retrieval was prohibited" in contract
    assert "do not discuss long-term-memory availability" in contract


def test_memory_recall_keeps_asset_episode_identity_but_general_topic_detaches() -> None:
    asset = _task()
    recall = _task(
        request="Summarize what you remember without refreshing evidence.",
        intent="memory_recall",
        required_capabilities=(),
        evidence_mode="memory_only",
        temporal_mode="historical",
    )
    neighbor = _task(intent="graph_neighbors", scope="one_hop", graph_depth=1)
    general = _task(
        request="What is Kerberos?",
        intent="general_knowledge",
        scope="none",
        entities=(),
        required_capabilities=(),
    )

    assert MemoryContextKey.from_task(asset) == MemoryContextKey.from_task(recall)
    assert MemoryContextKey.from_task(asset) == MemoryContextKey.from_task(neighbor)
    assert MemoryContextKey.from_task(general) != MemoryContextKey.from_task(asset)


class _Input(BaseModel):
    entities: list[str] = Field(default_factory=list, max_length=2)


def test_timeout_result_reports_observed_latency_instead_of_zero() -> None:
    registry = CapabilityRegistry()

    def blocking(_payload) -> ToolResult:
        time.sleep(0.05)
        return ToolResult(
            status="ok",
            entities=("192.0.2.10",),
            source_capability="asset.get_profile",
            retrieved_at="now",
            freshness="current",
            completeness="complete",
        )

    registry.register(
        CapabilitySpec(
            name="asset.get_profile",
            version="1",
            input_schema=_Input,
            output_schema=ToolResult,
            required_entity_cardinality=(1, 1),
            read_only=True,
            timeout_seconds=0.01,
            retry_policy=RetryPolicy(),
            planner_visible=True,
            concurrency_group="product",
        ),
        blocking,
    )
    task = _task(required_capabilities=("asset.get_profile",))
    plan = PlanValidator(registry).validate(
        ExecutionPlan(
            task=task,
            steps=(PlanStep("profile", "asset.get_profile", arguments={"entities": ["192.0.2.10"]}),),
            plan_id="timeout-test",
            target_entities=task.entities,
        )
    )
    result = CapabilityExecutor(registry, total_timeout_seconds=1).execute(plan)[0]

    assert result.status == "unavailable"
    assert result.safe_error_code == "capability_timeout"
    assert result.latency_ms >= 1


def test_exact_zero_guard_carries_the_executed_selector_scope_into_synthesis() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"classification_summary": "Domain Controller"},
    })
    task = _task(
        request="classification summary exactly Domain Controller",
        intent="asset_search",
        scope="none",
        direction="none",
        entities=(),
        required_capabilities=("graph.search_assets",),
        structured_query=query,
    )
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(task, ())
    contract, _ = builder.render_contract(context)

    assert context.structured_query_scope["filters"] == {
        "classification_summary": "Domain Controller"
    }
    assert context.structured_query_scope["exact_zero_scope_only"] is True
    assert "An exact zero-result applies only to the exact selectors" in contract
    assert "does not prove that no matching `suggested_type`, `role`, or `roles` value exists" in contract


def test_set_enrichment_coverage_is_explicit_in_synth_contract() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"role": "Domain Controller"},
    })
    task = _task(
        request="Find Domain Controllers and show their services and classification state.",
        intent="asset_search",
        scope="none",
        direction="none",
        entities=(),
        required_capabilities=("graph.search_assets",),
        structured_query=query,
    )
    summary = PostSearchEnrichmentSummary(
        mode="set_enrichment",
        matched_total=55,
        returned_count=10,
        target_count=5,
        completed_count=3,
        partial=True,
        target_entities=tuple(f"192.0.2.{index}" for index in range(1, 6)),
        completed_entities=("192.0.2.1", "192.0.2.2", "192.0.2.3"),
        requested_capabilities=("asset.get_profile", "asset.get_detection"),
    )
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(task, (), post_search_enrichment=summary)
    contract, _ = builder.render_contract(context)

    assert context.post_search_enrichment["matched_total"] == 55
    assert context.post_search_enrichment["target_count"] == 5
    assert context.post_search_enrichment["completed_count"] == 3
    assert context.post_search_enrichment["partial"] is True
    assert '"post_search_enrichment"' in contract
    assert "exact matched total separate from the number checked" in contract
