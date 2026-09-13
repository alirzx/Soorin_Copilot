"""Phase 4B.3 structured Asset-set evidence contract tests."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from src.config.settings import get_settings
from src.core.agent.contracts import RequestConstraints, TaskSpec
from src.core.agent.evidence import apply_context_inclusion, context_package_from_evidence
from src.core.agent.evidence_policy import EvidenceRequirementPolicy, MemorySufficiencyGate
from src.core.agent.executor import CapabilityExecutor
from src.core.agent.plan_validator import PlanValidator
from src.core.agent.registry import build_capability_registry
from src.core.agent.reviewer import EvidenceReviewer
from src.core.agent.specialists import GraphAnalysisSpecialist
from src.core.agent.structured_evidence import structured_query_identity
from src.core.agent.task_mapping import compile_direct_plan, task_spec_from_route
from src.core.context.models import GraphProviderResult, ProviderProvenance, RouteDecision
from src.core.context.composer import ContextComposer, PROVIDER_SEMANTICS
from src.core.context.models import EntityResolution, approx_tokens
from src.core.context.synthesizer_prompt import SynthesizerPromptBuilder
from src.core.graph.structured import StructuredQuerySpec


class _UnusedProvider:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(product_read_timeout_seconds=1, rag_qdrant_timeout_seconds=1, rag_top_k=3)
        self.calls = 0

    def fetch(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("Product provider must not be called")

    def search(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("Knowledge provider must not be called")


class _StructuredGraphProvider:
    settings = SimpleNamespace(
        agent_request_timeout_seconds=2,
        graph_asset_search_max_limit=200,
        graph_full_neighbors_hard_max=500,
    )

    def __init__(self, *, version: str = "graph-v7", empty: bool = False) -> None:
        self.version = version
        self.empty = empty
        self.search_calls = 0
        self.aggregate_calls = 0

    def search_assets(self, request):
        self.search_calls += 1
        rows = [] if self.empty else [
            {
                "graph_key": "asset-1",
                "graph_version": self.version,
                "ip": "192.0.2.10",
                "asset_name": "dc-01",
                "status": "CONFIRMED",
                "role": "Domain Controller",
            }
        ]
        matched = 0 if self.empty else 12
        return self._result(
            status="not_found" if self.empty else "available",
            context={
                "active_graph_version": self.version,
                "filters": request.filters.model_dump(mode="json", exclude_none=True),
                "rows": rows,
                "returned_count": len(rows),
                "matched_total": matched,
                "truncated": bool(matched > len(rows)),
                "sort": request.sort.value,
                "direction": request.direction.value,
                "retrieved_at": "2026-09-11T00:00:00Z",
                "retrieval_complete": matched == len(rows),
                "serialized_context_complete_for_retrieved_subset": True,
            },
        )

    def aggregate_assets(self, request):
        self.aggregate_calls += 1
        groups = (
            [
                {
                    "value": "CONFIRMED",
                    "count": 9,
                    "member_ips": ["192.0.2.10", "192.0.2.11"],
                    "member_ips_truncated": True,
                },
                {
                    "value": "REVIEW",
                    "count": 3,
                    "member_ips": ["192.0.2.12"],
                    "member_ips_truncated": True,
                },
            ]
            if request.group_by
            else []
        )
        return self._result(context={
            "active_graph_version": self.version,
            "filters": request.filters.model_dump(mode="json", exclude_none=True),
            "operation": request.operation.value,
            "group_by": request.group_by.value if request.group_by else None,
            "count": 12,
            "groups": groups,
            "truncated": False,
            "retrieved_at": "2026-09-11T00:00:00Z",
            "retrieved_node_count": 12,
            "retrieval_complete": True,
            "serialized_context_complete_for_retrieved_subset": True,
        })

    @staticmethod
    def _result(*, context, status="available"):
        return GraphProviderResult(
            provider="graph",
            status=status,
            context=context,
            provenance=ProviderProvenance(
                source="neo4j_structured_asset_projection",
                status=status,
            ),
            limitations=[
                "Enrichment is a discovery projection, not live Product profile or detection evidence."
            ],
        )


def _registry(*, version: str = "graph-v7", empty: bool = False):
    unused = _UnusedProvider()
    return build_capability_registry(
        asset_profile_provider=unused,
        detection_provider=unused,
        graph_provider=_StructuredGraphProvider(version=version, empty=empty),
        knowledge_service=unused,
    )


def _task(query: StructuredQuerySpec) -> TaskSpec:
    route = RouteDecision(
        use_graph=True,
        reason="structured_asset_set",
        structured_query=query,
        intent="asset_search" if query.mode.value == "search" else "asset_aggregate",
        scope="none",
        direction="none",
        entity_binding="none",
        resolved_entity_binding="none",
    )
    return task_spec_from_route(route, "structured request", RequestConstraints())


def test_search_tool_result_preserves_typed_query_counts_rows_and_provenance() -> None:
    result = _registry().execute("graph.search_assets", {
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
        "sort": "asset_name",
        "direction": "asc",
        "limit": 50,
    })

    evidence = result.structured_asset_set
    assert result.evidence_type == "graph_asset_search"
    assert result.total_count == 12
    assert result.included_count == 1
    assert result.omitted_count == 11
    assert result.truncated and result.completeness == "partial"
    assert evidence is not None and evidence.mode == "search"
    assert evidence.normalized_filters == {
        "role": "Domain Controller",
        "status": "CONFIRMED",
    }
    assert evidence.active_graph_version == "graph-v7"
    assert evidence.matched_total == 12 and evidence.returned_count == 1
    assert evidence.rows[0]["ip"] == "192.0.2.10"
    assert evidence.provenance == "neo4j_active_organizational_projection"
    assert result.normalized_query_hash == result.context_identity == evidence.query_identity


def test_aggregate_tool_result_and_evidence_pack_preserve_typed_groups() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "filters": {"role": "Domain Controller"},
        "operation": "group_count",
        "group_by": "status",
        "limit": 10,
    })
    result = _registry().execute("graph.aggregate_assets", {
        "filters": query.filters.model_dump(mode="json", exclude_none=True),
        "operation": "group_count",
        "group_by": "status",
        "limit": 10,
    })
    pack = EvidenceReviewer().build_pack(_task(query), [result])
    evidence = result.structured_asset_set

    assert result.evidence_type == "graph_asset_aggregate"
    assert evidence is not None and evidence.mode == "aggregate"
    assert evidence.operation == "group_count" and evidence.group_by == "status"
    assert evidence.count == 12 and sum(group["count"] for group in evidence.groups) == 12
    assert evidence.groups[0]["member_ips"] == ["192.0.2.10", "192.0.2.11"]
    assert evidence.groups[0]["member_ips_truncated"] is True
    assert evidence.groups[0]["percentage_of_total"] == 75.0
    assert evidence.active_graph_version == "graph-v7"
    assert pack.structured_asset_sets == (evidence,)
    assert pack.tool_results[0].structured_asset_set is evidence


def test_structured_query_identity_is_canonical_and_graph_version_bound() -> None:
    first = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"role": "Domain Controller", "status": "CONFIRMED"},
        "sort": "graph_key",
        "direction": "asc",
        "limit": 10,
    })
    reordered = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
        "direction": "asc",
        "sort": "graph_key",
        "limit": 50,
    })
    different = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"vendor": "VMware"},
    })

    identity = structured_query_identity(first, active_graph_version="graph-v7")
    assert identity == structured_query_identity(reordered, active_graph_version="graph-v7")
    assert identity != structured_query_identity(different, active_graph_version="graph-v7")
    assert identity != structured_query_identity(first, active_graph_version="graph-v8")


def test_successful_empty_search_remains_current_typed_evidence() -> None:
    result = _registry(empty=True).execute("graph.search_assets", {
        "filters": {"status": "CONFIRMED"},
    })
    evidence = result.structured_asset_set

    assert result.status == "not_found"
    assert result.total_count == result.included_count == result.omitted_count == 0
    assert not result.truncated
    assert evidence is not None
    assert evidence.matched_total == evidence.returned_count == 0
    assert evidence.rows == ()


def test_requirement_policy_uses_current_structured_classes_without_product_requirements() -> None:
    search = _task(StructuredQuerySpec.model_validate({"mode": "search", "filters": {"vendor": "VMware"}}))
    aggregate = _task(StructuredQuerySpec.model_validate({"mode": "aggregate", "operation": "count"}))

    search_requirements = EvidenceRequirementPolicy().derive(search)
    aggregate_requirements = EvidenceRequirementPolicy().derive(aggregate)

    assert [(item.capability, item.evidence_class, item.freshness_class) for item in search_requirements.requirements] == [
        ("graph.search_assets", "graph_asset_search", "current_verification")
    ]
    assert [(item.capability, item.evidence_class, item.freshness_class) for item in aggregate_requirements.requirements] == [
        ("graph.aggregate_assets", "graph_asset_aggregate", "current_verification")
    ]
    assert MemorySufficiencyGate().evaluate(search_requirements, ())[0].decision == "memory_unavailable"
    assert not any(item.capability.startswith("asset.") for item in search_requirements.requirements)


def test_existing_topology_requirement_class_is_unchanged() -> None:
    task = TaskSpec(
        request="Show neighbors",
        intent="graph_neighbors",
        scope="one_hop",
        direction="both",
        entities=("192.0.2.10",),
        required_capabilities=("graph.get_neighbors",),
    )
    requirements = EvidenceRequirementPolicy().derive(task)
    assert requirements.requirements[0].evidence_class == "graph_neighbors"


def _complete_search_result():
    result = _registry().execute("graph.search_assets", {
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
    })
    evidence = result.structured_asset_set
    assert evidence is not None and evidence.mode == "search"
    complete = replace(evidence, matched_total=1, truncated=False)
    return replace(
        result,
        total_count=1,
        included_count=1,
        omitted_count=0,
        truncated=False,
        completeness="complete",
        structured_asset_set=complete,
    )


def test_reviewer_accepts_zero_entity_search_aggregate_and_successful_empty() -> None:
    search_query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
    })
    aggregate_query = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "filters": {"role": "Domain Controller"},
        "operation": "group_count",
        "group_by": "status",
        "limit": 10,
    })
    aggregate = _registry().execute("graph.aggregate_assets", {
        "filters": {"role": "Domain Controller"},
        "operation": "group_count",
        "group_by": "status",
        "limit": 10,
    })
    empty = _registry(empty=True).execute("graph.search_assets", {
        "filters": {"status": "CONFIRMED"},
    })
    empty_query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED"},
    })

    assert EvidenceReviewer().review(_task(search_query), [_complete_search_result()]).outcome == "sufficient"
    assert EvidenceReviewer().review(_task(aggregate_query), [aggregate]).outcome == "sufficient"
    empty_review = EvidenceReviewer().review(_task(empty_query), [empty], allow_supplemental=True)
    assert empty_review.outcome == "sufficient"
    assert not empty_review.supplemental_allowed
    assert _task(search_query).entities == ()


def test_reviewer_marks_valid_truncation_partial_without_losing_answerability() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
    })
    result = _registry().execute("graph.search_assets", {
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
    })
    review = EvidenceReviewer().review(_task(query), [result])

    assert review.outcome == "answer_with_limitations"
    assert not review.missing_capabilities
    assert any("incomplete or truncated" in item for item in review.material_limitations)


def test_reviewer_rejects_wrong_query_identity_mode_missing_and_failed_results() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
    })
    task = _task(query)
    result = _complete_search_result()
    evidence = result.structured_asset_set
    assert evidence is not None

    wrong_identity = replace(
        result,
        structured_asset_set=replace(evidence, query_identity="wrong"),
    )
    wrong_mode = replace(
        result,
        structured_asset_set=replace(evidence, mode="aggregate"),  # type: ignore[arg-type]
    )
    for invalid in (wrong_identity, wrong_mode):
        decision = EvidenceReviewer().review(task, [invalid])
        assert decision.outcome == "missing_required_evidence"
        assert decision.missing_capabilities == ("graph.search_assets",)

    missing = EvidenceReviewer().review(task, [], allow_supplemental=True)
    assert missing.outcome == "missing_required_evidence"
    assert missing.next_capability == "graph.search_assets"
    assert missing.next_arguments == {
        "filters": {"role": "Domain Controller", "status": "CONFIRMED"},
    }
    failed = EvidenceReviewer().review(
        task,
        [replace(result, status="unavailable", structured_asset_set=None)],
        allow_supplemental=True,
    )
    assert failed.outcome == "safe_failure"


def _compose(result, task: TaskSpec, *, window: int = 32768):
    pack = EvidenceReviewer().build_pack(task, [result])
    package = context_package_from_evidence(
        pack,
        EntityResolution(status="none"),
    )
    settings = replace(
        get_settings(),
        llm_context_window_tokens=window,
        llm_context_safety_margin_tokens=256,
        llm_reserved_output_tokens=1024,
    )
    composer = ContextComposer(settings)
    text = composer.compose(
        package,
        base_input_tokens=100,
        reserved_output_tokens=1024,
        request_id="phase4b3-context",
    )
    return composer, text


def test_search_context_serializes_empty_one_and_bounded_large_results() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED"},
        "sort": "asset_name",
        "direction": "asc",
    })
    empty = _registry(empty=True).execute("graph.search_assets", {
        "filters": {"status": "CONFIRMED"},
        "sort": "asset_name",
        "direction": "asc",
    })
    _composer, empty_text = _compose(empty, _task(query))
    assert "SOORIN_STRUCTURED_ASSET_SET_CONTEXT_JSON" in empty_text
    assert '"matched_total":0' in empty_text and '"assets":[]' in empty_text

    one = _complete_search_result()
    one_query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
    })
    _composer, one_text = _compose(one, _task(one_query))
    assert "192.0.2.10" in one_text
    assert "neo4j_active_organizational_projection" in one_text
    assert "not live Product profile or detection truth" in one_text

    evidence = one.structured_asset_set
    assert evidence is not None and evidence.mode == "search"
    rows = tuple(
        {
            **evidence.rows[0],
            "graph_key": f"asset-{index}",
            "ip": f"198.51.100.{index}",
            "asset_name": "asset-" + ("x" * 1000),
            "role": "role-" + ("y" * 1000),
            "product": "product-" + ("z" * 1000),
        }
        for index in range(1, 121)
    )
    large_evidence = replace(
        evidence,
        matched_total=500,
        returned_count=len(rows),
        truncated=True,
        rows=rows,
    )
    large = replace(
        one,
        total_count=500,
        included_count=len(rows),
        omitted_count=380,
        truncated=True,
        completeness="partial",
        structured_asset_set=large_evidence,
    )
    composer, large_text = _compose(large, _task(one_query))
    assert approx_tokens(composer.last_parts["graph"]) <= 1800
    assert approx_tokens(large_text) <= composer.last_budget["max_dynamic_tokens"]
    assert large_text.count('"graph_key"') <= 20
    assert composer.last_inclusion[large.context_identity][0]
    assert '"context_truncated":true' in large_text
    assert '"rows_retrieved":120' in large_text
    assert '"rows_omitted_from_model_context":' in large_text


def test_aggregate_context_is_compact_and_preserves_count_and_groups() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "filters": {"role": "Domain Controller"},
        "operation": "group_count",
        "group_by": "status",
    })
    result = _registry().execute("graph.aggregate_assets", {
        "filters": {"role": "Domain Controller"},
        "operation": "group_count",
        "group_by": "status",
    })
    _composer, text = _compose(result, _task(query))

    assert '"count":12' in text
    assert '"group_by":"status"' in text
    assert '"value":"CONFIRMED"' in text
    assert '"member_ips":["192.0.2.10","192.0.2.11"]' in text
    assert '"percentage_of_total":75.0' in text
    assert approx_tokens(text) < 950


def test_graph_provider_semantics_include_structured_asset_projection_boundary() -> None:
    semantics = PROVIDER_SEMANTICS["graph"]

    assert "structured organizational Asset projection" in semantics["description"]
    assert "not live Product profile or detection truth" in semantics["limitation"]
    assert "Topology does not prove" in semantics["limitation"]


def test_aggregate_context_falls_back_to_count_when_first_group_does_not_fit() -> None:
    result = _registry().execute("graph.aggregate_assets", {
        "filters": {"role": "Domain Controller"},
        "operation": "group_count",
        "group_by": "status",
    })
    evidence = result.structured_asset_set
    assert evidence is not None and evidence.mode == "aggregate"
    evidence = replace(
        evidence,
        groups=({"value": "x" * 4000, "count": evidence.count},),
    )

    text = ""
    context: dict[str, object] = {}
    for token_budget in range(1, 701):
        context = {"structured_asset_set": evidence}
        text = ContextComposer()._compose_structured_asset_set(
            SimpleNamespace(context=context),
            token_budget=token_budget,
            request_id="aggregate-count-only",
        )
        if text:
            break

    assert text
    assert '"count":12' in text
    assert '"groups_in_model_context":0' in text
    assert '"groups_omitted_from_model_context":1' in text
    assert '"context_truncated":true' in text
    assert '"groups":[]' in text
    assert context["model_context_included_count"] == 0
    assert context["model_context_omitted_count"] == 1


def test_synthesizer_selects_asset_search_module_with_partial_set_rules() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED"},
    })
    result = _registry().execute("graph.search_assets", {
        "filters": {"status": "CONFIRMED"},
    })
    review = EvidenceReviewer().review(_task(query), [result])
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(_task(query), (result,), review=review)
    prompt, modules = builder.render_contract(context)

    assert context.task_category == "asset_search"
    assert "task.asset_search" in modules
    assert "Lead with what was found" in prompt
    assert "only part of the current results was returned" in prompt
    assert "not conversational focal entities" in prompt
    assert "Current Product evidence remains authoritative" in prompt
    assert "Never expose query syntax or implementation vocabulary" in prompt
    assert "Do not describe retrieval/context machinery" in prompt
    assert context.graph.truncated


def test_synthesizer_selects_compact_asset_aggregate_module_without_llm_call() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "filters": {"role": "Domain Controller"},
        "operation": "count",
    })
    result = _registry().execute("graph.aggregate_assets", {
        "filters": {"role": "Domain Controller"},
        "operation": "count",
    })
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(_task(query), (result,))
    rendered = builder.render_messages(
        static_core="Ground every claim.",
        context=context,
        dynamic_evidence='{"count":12,"mode":"aggregate"}',
        history=[],
        user_message="How many Domain Controllers do we have?",
    )

    assert context.task_category == "asset_aggregate"
    assert "task.asset_aggregate" in rendered.selected_module_names
    assert "Treat zero as a valid observed count" in rendered.dynamic_prompt
    assert "additional groups were not shown" in rendered.dynamic_prompt
    assert "without echoing backend query syntax" in rendered.dynamic_prompt
    assert "retrieval mechanics" in rendered.dynamic_prompt
    assert "precomputed percentages exactly" in rendered.dynamic_prompt
    assert "bounded member sample" in rendered.dynamic_prompt
    assert "Observed, Inferred, Hypothesis, or Unknown" in rendered.dynamic_prompt
    assert any('"count":12' in message["content"] for message in rendered.messages)


class _SynthStub:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, messages) -> str:
        self.calls += 1
        assert any("SOORIN_STRUCTURED_ASSET_SET_CONTEXT_JSON" in item["content"] for item in messages)
        return "grounded structured answer"


def _run_bounded_pipeline(query: StructuredQuerySpec):
    product = _UnusedProvider()
    knowledge = _UnusedProvider()
    graph = _StructuredGraphProvider()
    registry = build_capability_registry(
        asset_profile_provider=product,
        detection_provider=product,
        graph_provider=graph,
        knowledge_service=knowledge,
    )
    task = _task(query)
    plan = PlanValidator(registry).validate(compile_direct_plan(task))
    specialist = GraphAnalysisSpecialist(CapabilityExecutor(registry)).run({
        "request_id": "phase4b3-e2e",
        "session_id": "phase4b3",
        "workflow_id": "phase4b3-workflow",
        "execution_plan": plan,
    })
    results = list(specialist["tool_results"])
    reviewer = EvidenceReviewer()
    review = reviewer.review(task, results, allow_supplemental=True)
    pack = reviewer.build_pack(task, results, plan=plan, review=review)
    package = context_package_from_evidence(pack, EntityResolution(status="none"))
    composer = ContextComposer(replace(get_settings(), llm_context_window_tokens=32768))
    dynamic = composer.compose(
        package,
        base_input_tokens=200,
        reserved_output_tokens=1024,
        request_id="phase4b3-e2e",
    )
    reviewed_results = tuple(apply_context_inclusion(results, composer.last_inclusion))
    builder = SynthesizerPromptBuilder()
    synth_context = builder.build_context(task, reviewed_results, review=review)
    rendered = builder.render_messages(
        static_core="Ground every claim in current evidence.",
        context=synth_context,
        dynamic_evidence=dynamic,
        history=[],
        user_message=task.request,
    )
    synth = _SynthStub()
    answer = synth.invoke(rendered.messages)
    return task, plan, results, review, composer, synth, answer, product, knowledge, graph


def test_bounded_search_pipeline_reaches_synth_with_one_graph_call_and_no_fanout() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
        "limit": 50,
    })
    task, plan, results, review, composer, synth, answer, product, knowledge, graph = _run_bounded_pipeline(query)

    assert task.entities == () and len(plan.steps) == 1
    assert len(results) == 1 and results[0].source_capability == "graph.search_assets"
    assert review.outcome == "answer_with_limitations"
    assert composer.last_budget["total_dynamic_tokens"] <= composer.last_budget["max_dynamic_tokens"]
    assert synth.calls == 1 and answer == "grounded structured answer"
    assert graph.search_calls == 1 and graph.aggregate_calls == 0
    assert product.calls == knowledge.calls == 0


def test_bounded_aggregate_pipeline_reaches_synth_without_product_or_qdrant_calls() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "filters": {"role": "Domain Controller"},
        "operation": "count",
    })
    task, plan, results, review, composer, synth, answer, product, knowledge, graph = _run_bounded_pipeline(query)

    assert task.entities == () and len(plan.steps) == 1
    assert len(results) == 1 and results[0].source_capability == "graph.aggregate_assets"
    assert review.outcome == "sufficient"
    assert '"count":12' in composer.last_parts["graph"]
    assert synth.calls == 1 and answer == "grounded structured answer"
    assert graph.aggregate_calls == 1 and graph.search_calls == 0
    assert product.calls == knowledge.calls == 0
