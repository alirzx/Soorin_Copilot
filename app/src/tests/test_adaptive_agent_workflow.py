"""Focused contracts and safety-harness tests for the bounded Investigator."""

from __future__ import annotations

from dataclasses import replace
import json
import time
from types import SimpleNamespace

import pytest

from src.config.settings import get_settings
from src.core.agent.adaptive_evidence import (
    evidence_reference_for_result,
    evidence_semantic_fingerprint,
    equivalent_evidence_for_step,
    references_semantically_equivalent,
)
from src.core.agent.action_validator import AgentActionValidationError, AgentActionValidator
from src.core.agent.agent_loop import build_observation, evaluate_progress, update_ledger
from src.core.agent.context_identity import canonical_action_fingerprint, context_result_identity
from src.core.agent.contracts import (
    AgentCapabilityRequest,
    AgentContinueDecision,
    AgentLoopBudget,
    AgentLoopState,
    AgentObservation,
    CapabilitySpec,
    EvidenceFact,
    EvidenceGap,
    EvidenceLedger,
    EvidenceReference,
    PlanStep,
    PostSearchRequirements,
    RequestConstraints,
    RetryPolicy,
    StructuredAssetSearchEvidence,
    TaskEnvelope,
    TaskSpec,
    ToolResult,
)
from src.core.agent.investigator import Investigator, InvestigatorError
from src.core.agent.investigator_context import InvestigatorContextBuilder, InvestigatorContextError
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.agent.plan_validator import PlanValidator
from src.core.agent.registry import CapabilityRegistry, EntityInput
from src.core.agent.task_mapping import select_orchestration_mode
from src.core.graph.structured import StructuredQuerySpec, structured_query_identity
from src.core.context.synthesizer_prompt import SynthesizerPromptBuilder
from src.core.observability.llm_usage import LLMUsageCall, ProductUsageReporter


def _spec(name: str, *, cardinality: tuple[int, int] = (1, 1), max_depth: int = 0) -> CapabilitySpec:
    return CapabilitySpec(
        name=name,
        version="1",
        input_schema=EntityInput,
        output_schema=ToolResult,
        required_entity_cardinality=cardinality,
        read_only=True,
        timeout_seconds=1,
        retry_policy=RetryPolicy(),
        planner_visible=True,
        side_effect_class="read_only",
        maximum_graph_depth=max_depth,
        allowed_arguments=("entities", "scope", "direction", "depth", "relationship_mode", "views", "detail", "max_context_tokens", "purpose"),
        allowed_scopes=("two_hop",) if name == "graph.get_neighbors" else (),
        allowed_depths=(2,) if name == "graph.get_neighbors" else (),
    )


@pytest.fixture
def registry() -> CapabilityRegistry:
    value = CapabilityRegistry()
    handler = lambda payload: ToolResult(  # noqa: E731
        status="ok",
        entities=tuple(payload.entities),
        source_capability="test",
        retrieved_at="now",
        freshness="current",
        completeness="complete",
    )
    value.register(_spec("asset.get_profile"), handler)
    value.register(_spec("asset.get_detection"), handler)
    value.register(_spec("graph.get_neighbors", max_depth=2), handler)
    return value


def _task() -> TaskSpec:
    return TaskSpec(
        request="Investigate 10.0.0.1",
        intent="asset_investigation",
        scope="two_hop",
        direction="both",
        entities=("10.0.0.1",),
        required_capabilities=("asset.get_profile",),
        graph_depth=2,
        relationship_mode="neighbors",
        workflow_mode="multi_step",
        orchestration_mode="adaptive",
    )


def _budget(**changes: object) -> AgentLoopBudget:
    base = AgentLoopBudget(
        max_investigator_turns=4,
        max_llm_calls=6,
        max_capabilities_per_decision=2,
        max_total_capability_calls=6,
        max_deepened_entities=2,
        max_graph_depth=2,
        max_technical_failures=2,
        deadline_monotonic=time.monotonic() + 60,
        llm_calls=1,
    )
    return replace(base, **changes)


def _ledger(*, fingerprint: str = "") -> EvidenceLedger:
    return EvidenceLedger(
        authorized_entities=("10.0.0.1",),
        gaps=(EvidenceGap(
            gap_id="profile-gap",
            dimension="asset_identity",
            importance="required",
            status="open",
            temporal_requirement="current",
            authorized_capabilities=("asset.get_profile",),
            authority_requirement="product_current",
            entities=("10.0.0.1",),
        ),),
        action_fingerprints=(fingerprint,) if fingerprint else (),
    )


def _decision(capability: str = "asset.get_profile", entity: str = "10.0.0.1") -> AgentContinueDecision:
    return AgentContinueDecision(
        kind="CONTINUE",
        evidence_gap_id="profile-gap",
        capability_requests=(AgentCapabilityRequest(capability, {"entities": [entity]}),),
    )


def _product_result(
    capability: str = "asset.get_profile",
    *,
    views: tuple[str, ...] = ("identity",),
    value: str = "domain-controller",
    status: str = "ok",
    completeness: str = "complete",
    truncated: bool = False,
    projection_usable: bool = True,
    source_payload_complete: bool = True,
    step_id: str = "step-1",
) -> ToolResult:
    payload = {"views": {view: {"classification": value, "view": view} for view in views}}
    return ToolResult(
        status=status,
        entities=("10.0.0.1",),
        source_capability=capability,
        retrieved_at="2026-09-28T00:00:00Z",
        freshness="current",
        completeness=completeness,
        facts=(EvidenceFact(capability, "bounded", value),),
        provider="product_api",
        raw_payload={"request_id": "request-a", "latency": 10},
        view_payload=payload,
        selected_views=views,
        detail="standard",
        purpose="asset_summary",
        projection_schema_version="product-view-v1",
        source_payload_complete=source_payload_complete,
        projection_usable=projection_usable,
        projection_truncated=truncated,
        truncated=truncated,
        step_id=step_id,
    )


def test_investigator_parses_all_decision_kinds_and_rejects_malformed() -> None:
    continued = Investigator.parse(
        '{"kind":"CONTINUE","evidence_gap_id":"g1","capability_requests":'
        '[{"capability":"asset.get_profile","arguments":{"entities":["10.0.0.1"]}}]}'
    )
    assert continued.kind == "CONTINUE"
    assert Investigator.parse('{"kind":"FINISH","stop_reason":"evidence_sufficient"}').kind == "FINISH"
    assert Investigator.parse(
        '{"kind":"CLARIFY","clarification_code":"entity_required",'
        '"clarification_summary":"Select one asset."}'
    ).kind == "CLARIFY"
    with pytest.raises(InvestigatorError, match="one JSON object"):
        Investigator.parse("```json\n{}\n```")
    with pytest.raises(InvestigatorError):
        Investigator.parse('{"kind":"CONTINUE","evidence_gap_id":"g1","capability_requests":[]}')


@pytest.mark.parametrize(
    ("decision", "constraints", "budget", "code"),
    [
        (_decision("unknown.tool"), RequestConstraints(), _budget(), "unknown_capability"),
        (_decision(entity="10.0.0.9"), RequestConstraints(), _budget(), "entity_authority_violation"),
        (_decision(), RequestConstraints(allow_live=False, memory_only=True), _budget(), "live_capability_forbidden_by_request"),
        (_decision(), RequestConstraints(), _budget(capability_calls=6), "tool_budget_exhausted"),
    ],
)
def test_action_validator_rejects_unauthorized_actions(
    registry: CapabilityRegistry,
    decision: AgentContinueDecision,
    constraints: RequestConstraints,
    budget: AgentLoopBudget,
    code: str,
) -> None:
    validator = AgentActionValidator(registry, PlanValidator(registry))
    with pytest.raises(AgentActionValidationError) as caught:
        validator.validate(
            decision,
            task=_task(),
            constraints=constraints,
            ledger=_ledger(),
            budget=budget,
            turn=1,
        )
    assert caught.value.code == code


def test_action_validator_rejects_graph_expansion_and_repeated_action(registry: CapabilityRegistry) -> None:
    task = replace(
        _task(),
        required_capabilities=("graph.get_neighbors",),
        graph_depth=1,
    )
    ledger = replace(
        _ledger(),
        gaps=(replace(
            _ledger().gaps[0],
            authorized_capabilities=("graph.get_neighbors",),
        ),),
    )
    validator = AgentActionValidator(registry, PlanValidator(registry))
    expanded = AgentContinueDecision(
        kind="CONTINUE",
        evidence_gap_id="profile-gap",
        capability_requests=(AgentCapabilityRequest(
            "graph.get_neighbors",
            {"entities": ["10.0.0.1"], "scope": "two_hop", "direction": "both", "depth": 2},
        ),),
    )
    with pytest.raises(AgentActionValidationError) as caught:
        validator.validate(expanded, task=task, constraints=RequestConstraints(), ledger=ledger, budget=_budget(), turn=1)
    assert caught.value.code == "graph_depth_authority_violation"

    valid = validator.validate(_decision(), task=_task(), constraints=RequestConstraints(), ledger=_ledger(), budget=_budget(), turn=1)
    with pytest.raises(AgentActionValidationError) as repeated:
        validator.validate(
            _decision(),
            task=_task(),
            constraints=RequestConstraints(),
            ledger=_ledger(fingerprint=valid.fingerprints[0]),
            budget=_budget(),
            turn=2,
        )
    assert repeated.value.code == "repeated_action"


def test_investigator_role_and_usage_are_first_class() -> None:
    settings = replace(
        get_settings(),
        investigator_base_url="https://investigator.invalid",
        investigator_model="investigator-model",
    )
    assert settings.adaptive_agent_enabled is False
    assert settings.deployment_for_purpose("investigator").name == "investigator"
    assert settings.deployment_for_purpose("investigator").model == "investigator-model"
    payload = ProductUsageReporter._payload_for_request([LLMUsageCall.from_usage(
        request_id="r1",
        call_id="c1",
        purpose="investigator",
        model="investigator-model",
        usage={"input_tokens": 10, "output_tokens": 2},
    )])
    assert payload["models"] == [{
        "purpose": "investigator",
        "model": "investigator-model",
        "inputTokens": 10,
        "outputTokens": 2,
    }]


def test_orchestration_selection_is_feature_gated_and_keeps_simple_tasks_direct() -> None:
    complex_task = _task()
    assert select_orchestration_mode(
        complex_task,
        constraints=RequestConstraints(),
        adaptive_enabled=False,
        planner_selected=True,
    ) == "fixed"
    assert select_orchestration_mode(
        complex_task,
        constraints=RequestConstraints(),
        adaptive_enabled=True,
        planner_selected=True,
    ) == "adaptive"
    simple = replace(
        complex_task,
        request="Show the profile for 10.0.0.1",
        intent="asset_profile",
        workflow_mode="direct",
    )
    assert select_orchestration_mode(
        simple,
        constraints=RequestConstraints(),
        adaptive_enabled=True,
        planner_selected=False,
    ) == "direct"
    assert select_orchestration_mode(
        complex_task,
        constraints=RequestConstraints(allow_live=False, memory_only=True),
        adaptive_enabled=True,
        planner_selected=True,
    ) == "direct"


def test_investigator_context_excludes_raw_memory_and_enforces_hard_limit(registry: CapabilityRegistry) -> None:
    settings = replace(
        get_settings(),
        investigator_max_input_tokens=8000,
        investigator_hard_input_tokens=12000,
    )
    builder = InvestigatorContextBuilder(settings, registry)
    memory = SimpleNamespace(
        memories=(SimpleNamespace(
            freshness="active",
            memory=SimpleNamespace(
                memory_id="m1",
                entity_ids=("10.0.0.1",),
                evidence_refs=("evidence_class_asset_identity",),
                epistemic_status="historical",
                statement="RAW_SECRET_PROVIDER_PAYLOAD",
            ),
        ),),
    )
    context = builder.build(
        task=_task(),
        constraints=RequestConstraints(),
        envelope=TaskEnvelope(ordered_entities=("10.0.0.1",)),
        ledger=_ledger(),
        latest_observation=None,
        budget=_budget(),
        long_term_selection=memory,
        turn=1,
        system_prompt="system",
    )
    assert "RAW_SECRET_PROVIDER_PAYLOAD" not in context.context_json
    assert "raw_payload" not in context.context_json

    tiny = replace(settings, investigator_max_input_tokens=1, investigator_hard_input_tokens=1)
    with pytest.raises(InvestigatorContextError) as caught:
        InvestigatorContextBuilder(tiny, registry).build(
            task=_task(),
            constraints=RequestConstraints(),
            envelope=TaskEnvelope(ordered_entities=("10.0.0.1",)),
            ledger=_ledger(),
            latest_observation=None,
            budget=_budget(),
            turn=1,
            system_prompt="system",
        )
    assert caught.value.code == "context_budget_exhausted"


def test_current_gap_rejects_historical_memory_and_accepts_current_product_evidence() -> None:
    historical = ToolResult(
        status="ok",
        entities=("10.0.0.1",),
        source_capability="asset.get_profile",
        retrieved_at="2026-01-01T00:00:00Z",
        freshness="historical",
        completeness="complete",
        provider="long_term_memory",
        source_payload_complete=True,
        projection_usable=True,
    )
    ledger = update_ledger(_ledger(), _task(), (historical,))
    assert ledger.gaps[0].status == "unavailable"
    assert evaluate_progress(ledger, _budget()) == "answer_with_limitations"

    current = replace(
        historical,
        freshness="current",
        provider="product_api",
        retrieved_at="2026-09-28T00:00:00Z",
    )
    ledger = update_ledger(_ledger(), _task(), (current,))
    assert ledger.gaps[0].status == "satisfied"
    assert evaluate_progress(ledger, _budget()) == "evidence_sufficient"


@pytest.mark.parametrize(
    ("budget", "reason"),
    [
        (_budget(investigator_turns=4), "budget_exhausted"),
        (_budget(llm_calls=5), "llm_budget_exhausted"),
        (_budget(capability_calls=6), "tool_budget_exhausted"),
        (_budget(technical_failures=2), "technical_failure_ceiling"),
    ],
)
def test_progress_gate_enforces_each_runtime_budget(
    budget: AgentLoopBudget,
    reason: str,
) -> None:
    assert evaluate_progress(_ledger(), budget) == reason


def test_structured_search_candidates_open_only_bounded_post_search_gaps() -> None:
    task = replace(
        _task(),
        post_search_requirements=PostSearchRequirements(
            mode="focal_deepening",
            entity_capabilities=("asset.get_profile",),
            requires_focal_graph=True,
            max_assets=2,
        ),
    )
    structured = StructuredAssetSearchEvidence(
        capability="graph.search_assets",
        query_identity="query-1",
        normalized_filters={"risk": "high"},
        active_graph_version="v1",
        sort="risk_score",
        direction="desc",
        matched_total=2,
        returned_count=2,
        truncated=False,
        rows=({"ip": "10.0.0.2"}, {"ip": "10.0.0.3"}),
        retrieved_at="2026-09-28T00:00:00Z",
    )
    result = ToolResult(
        status="ok",
        entities=(),
        source_capability="graph.search_assets",
        retrieved_at="2026-09-28T00:00:00Z",
        freshness="current",
        completeness="complete",
        structured_asset_set=structured,
        source_payload_complete=True,
        projection_usable=True,
    )
    ledger = update_ledger(EvidenceLedger(), task, (result,))
    assert ledger.structured_candidates == ("10.0.0.2", "10.0.0.3")
    assert {gap.dimension for gap in ledger.gaps} == {"post_search_profile", "post_search_graph"}
    assert all(gap.status == "open" for gap in ledger.gaps)


def test_synthesizer_contract_receives_adaptive_stop_metadata() -> None:
    budget = _budget(investigator_turns=2, llm_calls=3)
    loop = AgentLoopState(
        turn=2,
        budget=budget,
        ledger=_ledger(),
        stop_reason="llm_budget_exhausted",
        started_monotonic=time.monotonic() - 1,
    )
    builder = SynthesizerPromptBuilder()
    context = builder.build_context(_task(), (), agent_loop_state=loop)
    rendered, _modules = builder.render_contract(context)
    assert '\"orchestration_mode\":\"adaptive\"' in rendered
    assert '\"stop_reason\":\"llm_budget_exhausted\"' in rendered
    assert '\"turn_count\":2' in rendered
    assert '\"unresolved_gap_count\":1' in rendered
    assert '\"budget_exhausted\":true' in rendered


def test_semantic_evidence_fingerprint_ignores_operational_metadata_but_tracks_facts() -> None:
    first = _product_result(value="domain-controller")
    operationally_different = replace(
        first,
        retrieved_at="2027-01-01T00:00:00Z",
        valid_at="2027-01-01T00:00:00Z",
        latency_ms=999,
        cache_status="hit",
        step_id="different-request-step",
        raw_payload={"request_id": "request-b", "latency": 999},
    )
    changed = _product_result(value="workstation")
    assert evidence_semantic_fingerprint(first) == evidence_semantic_fingerprint(operationally_different)
    assert evidence_semantic_fingerprint(first) != evidence_semantic_fingerprint(changed)


def test_context_and_action_identity_preserve_direction_and_only_commute_comparison() -> None:
    forward = context_result_identity(
        "graph.get_relationship", ("10.0.0.1", "10.0.0.2"),
        scope="one_hop", direction="outbound", depth=1,
    )
    reverse = context_result_identity(
        "graph.get_relationship", ("10.0.0.2", "10.0.0.1"),
        scope="one_hop", direction="outbound", depth=1,
    )
    assert forward != reverse
    assert context_result_identity(
        "graph.compare_assets", ("10.0.0.1", "10.0.0.2"), direction="both", depth=1,
    ) == context_result_identity(
        "graph.compare_assets", ("10.0.0.2", "10.0.0.1"), direction="both", depth=1,
    )
    comparison_a = PlanStep("a", "graph.compare_assets", {"entities": ["10.0.0.1", "10.0.0.2"]})
    comparison_b = PlanStep("b", "graph.compare_assets", {"entities": ["10.0.0.2", "10.0.0.1"]})
    relationship_b = PlanStep("b", "graph.get_relationship", {"entities": ["10.0.0.2", "10.0.0.1"]})
    assert canonical_action_fingerprint(comparison_a) == canonical_action_fingerprint(comparison_b)
    assert canonical_action_fingerprint(comparison_a) != canonical_action_fingerprint(relationship_b)


@pytest.mark.parametrize(
    ("capability", "available", "requested"),
    [
        ("asset.get_profile", ("identity", "security"), ("identity",)),
        ("asset.get_detection", ("overview", "evidence"), ("overview",)),
    ],
)
def test_product_view_superset_is_reused_but_incomplete_projection_is_not(
    registry: CapabilityRegistry,
    capability: str,
    available: tuple[str, ...],
    requested: tuple[str, ...],
) -> None:
    prior_gap = replace(
        _ledger().gaps[0],
        authorized_capabilities=(capability,),
        dimension="asset_identity" if capability.endswith("profile") else "detection_classification",
    )
    prior_step = PlanStep(
        "prior", capability,
        {"entities": ["10.0.0.1"], "views": list(available), "detail": "standard"},
    )
    prior_result = _product_result(capability, views=available, step_id="prior")
    reference = evidence_reference_for_result(prior_result, (prior_gap,), step=prior_step)
    new_gap = replace(prior_gap, gap_id="new-gap", status="open")
    ledger = EvidenceLedger(
        authorized_entities=("10.0.0.1",),
        gaps=(new_gap,),
        evidence_references=(reference,),
    )
    task = replace(_task(), required_capabilities=(capability,))
    decision = AgentContinueDecision(
        kind="CONTINUE",
        evidence_gap_id="new-gap",
        capability_requests=(AgentCapabilityRequest(
            capability,
            {"entities": ["10.0.0.1"], "views": list(requested), "detail": "standard"},
        ),),
    )
    with pytest.raises(AgentActionValidationError) as caught:
        AgentActionValidator(registry, PlanValidator(registry)).validate(
            decision,
            task=task,
            constraints=RequestConstraints(),
            ledger=ledger,
            budget=_budget(),
            turn=2,
        )
    assert caught.value.code == "equivalent_evidence_already_available"

    missing_view = "network" if capability.endswith("profile") else "similarity"
    distinct = replace(
        decision,
        capability_requests=(AgentCapabilityRequest(
            capability,
            {"entities": ["10.0.0.1"], "views": [missing_view], "detail": "standard"},
        ),),
    )
    assert AgentActionValidator(registry, PlanValidator(registry)).validate(
        distinct,
        task=task,
        constraints=RequestConstraints(),
        ledger=ledger,
        budget=_budget(),
        turn=2,
    ).plan.steps

    unusable_results = (
        replace(prior_result, completeness="partial"),
        replace(prior_result, truncated=True, projection_truncated=True),
        replace(prior_result, projection_usable=False),
        replace(prior_result, source_payload_complete=False),
    )
    for unusable in unusable_results:
        unusable_ref = evidence_reference_for_result(unusable, (new_gap,), step=prior_step)
        validated = AgentActionValidator(registry, PlanValidator(registry)).validate(
            decision,
            task=task,
            constraints=RequestConstraints(),
            ledger=replace(ledger, evidence_references=(unusable_ref,)),
            budget=_budget(),
            turn=2,
        )
        assert validated.plan.steps


def _graph_result(version: str = "v1") -> ToolResult:
    context = {
        "active_graph_version": version,
        "requested_scope": "two_hop",
        "direction": "both",
        "depth": 2,
        "relationship_mode": "neighbors",
        "retrieval_complete": True,
        "requested_scope_complete": True,
        "complete_for_user_request": True,
        "retrieval_truncated": False,
    }
    return ToolResult(
        status="ok",
        entities=("10.0.0.1",),
        source_capability="graph.get_neighbors",
        retrieved_at="now",
        freshness="current",
        completeness="complete",
        raw_payload=context,
        source_payload_complete=True,
        projection_usable=True,
        step_id="graph-prior",
    )


def test_graph_equivalence_requires_exact_action_and_same_projection_version() -> None:
    task = replace(_task(), required_capabilities=("graph.get_neighbors",))
    gap = replace(
        _ledger().gaps[0],
        authorized_capabilities=("graph.get_neighbors",),
        authority_requirement="graph_projection",
    )
    prior_step = PlanStep(
        "graph-prior", "graph.get_neighbors",
        {"entities": ["10.0.0.1"], "scope": "two_hop", "direction": "both", "depth": 2, "relationship_mode": "neighbors"},
    )
    reference = evidence_reference_for_result(_graph_result("v1"), (gap,), step=prior_step)
    assert equivalent_evidence_for_step(prior_step, gap, task, (reference,)) is not None
    changed_direction = replace(prior_step, arguments={**prior_step.arguments, "direction": "inbound"})
    assert equivalent_evidence_for_step(changed_direction, gap, task, (reference,)) is None
    changed_entity = replace(prior_step, arguments={**prior_step.arguments, "entities": ["10.0.0.2"]})
    assert equivalent_evidence_for_step(changed_entity, gap, task, (reference,)) is None
    changed_scope = replace(prior_step, arguments={**prior_step.arguments, "scope": "one_hop", "depth": 1})
    assert equivalent_evidence_for_step(changed_scope, gap, task, (reference,)) is None
    next_version = evidence_reference_for_result(_graph_result("v2"), (gap,), step=prior_step)
    assert not references_semantically_equivalent(reference, next_version)
    assert reference.semantic_fingerprint != next_version.semantic_fingerprint


def test_structured_and_knowledge_equivalence_reuse_existing_identity_rules() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search", "filters": {}, "sort": "graph_key", "direction": "asc", "limit": 5,
    })
    task = replace(
        _task(),
        entities=(),
        required_capabilities=("graph.search_assets",),
        structured_query=query,
    )
    query_id = structured_query_identity(query, active_graph_version="v1")
    structured = StructuredAssetSearchEvidence(
        capability="graph.search_assets",
        query_identity=query_id,
        normalized_filters={},
        active_graph_version="v1",
        sort="graph_key",
        direction="asc",
        matched_total=1,
        returned_count=1,
        truncated=False,
        rows=({"ip": "10.0.0.2"},),
        retrieved_at="now",
    )
    structured_result = ToolResult(
        status="ok", entities=(), source_capability="graph.search_assets",
        retrieved_at="now", freshness="current", completeness="complete",
        source_payload_complete=True, projection_usable=True,
        structured_asset_set=structured, context_identity=query_id,
        normalized_query_hash=query_id, semantic_query_id=structured.semantic_query_id,
    )
    structured_gap = EvidenceGap(
        "search-gap", "graph_asset_search", "required", "open", "current",
        ("graph.search_assets",), "graph_projection",
    )
    prior_search = PlanStep("search-1", "graph.search_assets", {"limit": 5})
    structured_ref = evidence_reference_for_result(structured_result, (structured_gap,), step=prior_search)
    compatible_search = PlanStep("search-2", "graph.search_assets", {"limit": 10})
    assert equivalent_evidence_for_step(
        compatible_search, structured_gap, task, (structured_ref,)
    ) is not None
    v2_structured = replace(structured, active_graph_version="v2", query_identity=structured_query_identity(query, active_graph_version="v2"))
    v2_ref = evidence_reference_for_result(
        replace(structured_result, structured_asset_set=v2_structured, context_identity=v2_structured.query_identity),
        (structured_gap,),
        step=prior_search,
    )
    assert not references_semantically_equivalent(structured_ref, v2_ref)

    knowledge_gap = EvidenceGap(
        "knowledge-gap", "knowledge_background", "optional", "open", "either",
        ("knowledge.search",), "knowledge_reference",
    )
    prior_knowledge = PlanStep(
        "knowledge-1", "knowledge.search",
        {"query": "Kerberos containment procedure", "purpose": "investigation_procedure"},
    )
    knowledge_result = ToolResult(
        status="ok", entities=(), source_capability="knowledge.search",
        retrieved_at="now", freshness="current", completeness="complete",
        facts=(EvidenceFact("knowledge.search", "chunk", "bounded guidance"),),
        purpose="investigation_procedure", normalized_query_hash="knowledge-1",
    )
    knowledge_ref = evidence_reference_for_result(knowledge_result, (knowledge_gap,), step=prior_knowledge)
    equivalent_knowledge = replace(
        prior_knowledge,
        id="knowledge-2",
        arguments={"query": "Kerberos containment procedure!", "purpose": "investigation_procedure"},
    )
    distinct_knowledge = replace(
        prior_knowledge,
        id="knowledge-3",
        arguments={"query": "DNS investigation workflow", "purpose": "interpret_evidence"},
    )
    assert equivalent_evidence_for_step(
        equivalent_knowledge, knowledge_gap, _task(), (knowledge_ref,)
    ) is not None
    assert equivalent_evidence_for_step(
        distinct_knowledge, knowledge_gap, _task(), (knowledge_ref,)
    ) is None
    distinct_query_same_purpose = replace(
        prior_knowledge,
        id="knowledge-4",
        arguments={"query": "DNS investigation workflow", "purpose": "investigation_procedure"},
    )
    # Existing Planner policy intentionally treats a repeated purpose as a duplicate,
    # even when the lexical query differs; adaptive reuse shares that exact rule.
    assert equivalent_evidence_for_step(
        distinct_query_same_purpose, knowledge_gap, _task(), (knowledge_ref,)
    ) is not None


def test_historical_reference_only_covers_historical_memory_authority() -> None:
    result = replace(
        _product_result(),
        provider="long_term_memory",
        freshness="historical",
    )
    current_gap = _ledger().gaps[0]
    historical_gap = replace(
        current_gap,
        gap_id="historical-gap",
        temporal_requirement="historical",
        authority_requirement="memory_historical",
    )
    reference = evidence_reference_for_result(result, (current_gap, historical_gap))
    assert current_gap.gap_id not in reference.covered_gap_ids
    assert historical_gap.gap_id in reference.covered_gap_ids


def test_observation_is_delta_oriented_and_semantic_repeat_is_not_progress() -> None:
    detection_gap = EvidenceGap(
        "detection-gap", "detection_classification", "required", "open", "current",
        ("asset.get_detection",), "product_current", ("10.0.0.1",),
    )
    initial = replace(_ledger(), gaps=(*_ledger().gaps, detection_gap))
    task = replace(_task(), required_capabilities=("asset.get_profile", "asset.get_detection"))
    profile = _product_result(step_id="profile-step")
    profile_step = PlanStep("profile-step", "asset.get_profile", {"entities": ["10.0.0.1"], "views": ["identity"]})
    first = update_ledger(initial, task, (profile,), action_steps=(profile_step,))
    detection = _product_result("asset.get_detection", views=("overview",), step_id="detection-step")
    detection_step = PlanStep("detection-step", "asset.get_detection", {"entities": ["10.0.0.1"], "views": ["overview"]})
    second = update_ledger(first, task, (detection,), action_steps=(detection_step,))
    delta = build_observation(
        turn=2,
        requests=(AgentCapabilityRequest("asset.get_detection", detection_step.arguments),),
        results=(detection,),
        previous=first,
        current=second,
    )
    assert len(delta.new_evidence_references) == 1
    assert delta.new_evidence_references[0] not in {
        item.reference_id for item in first.evidence_references
    }
    assert delta.new_coverage == ("detection-gap",)
    assert delta.material_progress is True

    repeated = update_ledger(second, task, (detection,), action_steps=(detection_step,))
    repeat_delta = build_observation(
        turn=3,
        requests=(AgentCapabilityRequest("asset.get_detection", detection_step.arguments),),
        results=(detection,),
        previous=second,
        current=repeated,
    )
    assert repeat_delta.result_references == ()
    assert repeat_delta.material_progress is False

    contradicted = replace(second, contradictions=("asset.get_detection:contradiction:1",))
    contradiction_delta = build_observation(
        turn=3,
        requests=(),
        results=(),
        previous=second,
        current=contradicted,
    )
    assert contradiction_delta.new_contradictions
    assert contradiction_delta.material_progress is True


def test_investigator_context_compacts_reference_index_and_preserves_authority(
    registry: CapabilityRegistry,
) -> None:
    gap = _ledger().gaps[0]
    base = evidence_reference_for_result(_product_result(), (gap,))
    references = tuple(
        replace(
            base,
            reference_id=f"agent-evidence-reference-v1:{index:064x}",
            context_identity=f"asset.get_profile:authorized-{index}",
            material_limitation_flags=("bounded-fixture-" + ("x" * 80),),
        )
        for index in range(24)
    )
    ledger = replace(_ledger(), evidence_references=references)
    latest = AgentObservation(
        turn=2,
        capability_requests=(AgentCapabilityRequest("asset.get_profile", {"entities": ["10.0.0.1"]}),),
        result_references=(references[-1].reference_id,),
        status_summary=(("asset.get_profile", "ok"),),
        new_coverage=(),
        new_contradictions=(),
        remaining_gap_ids=(gap.gap_id,),
        material_progress=True,
        tool_call_count=1,
        new_evidence_references=(references[-1].reference_id,),
    )
    settings = replace(
        get_settings(),
        investigator_max_input_tokens=2600,
        investigator_hard_input_tokens=12000,
    )
    context = InvestigatorContextBuilder(settings, registry).build(
        task=_task(),
        constraints=RequestConstraints(),
        envelope=TaskEnvelope(ordered_entities=("10.0.0.1",), freshness_requirement="current"),
        ledger=ledger,
        latest_observation=latest,
        budget=_budget(),
        turn=3,
        system_prompt="system",
    )
    payload = json.loads(context.context_json)
    assert context.compacted is True
    assert context.input_tokens_after < context.input_tokens_before
    assert context.input_tokens_after <= settings.investigator_max_input_tokens
    assert payload["task"]["intent"] == "asset_investigation"
    assert payload["task_envelope"]["authorized_entities"] == ["10.0.0.1"]
    assert payload["evidence_ledger"]["gaps"][0]["gap_id"] == "profile-gap"
    assert payload["latest_observation"]["turn"] == 2
    assert payload["capability_catalog"]
    assert "raw_payload" not in context.context_json
    assert "model_output" not in context.context_json


def test_equivalent_action_reuses_gap_coverage_without_provider_call(
    registry: CapabilityRegistry,
) -> None:
    prior_gap = _ledger().gaps[0]
    prior_step = PlanStep(
        "prior", "asset.get_profile",
        {"entities": ["10.0.0.1"], "views": ["identity", "security"], "detail": "standard"},
    )
    reference = evidence_reference_for_result(
        _product_result(views=("identity", "security"), step_id="prior"),
        (prior_gap,),
        step=prior_step,
    )
    open_gap = replace(prior_gap, gap_id="new-gap", status="open")
    ledger = replace(_ledger(), gaps=(open_gap,), evidence_references=(reference,))
    decision = AgentContinueDecision(
        kind="CONTINUE",
        evidence_gap_id="new-gap",
        capability_requests=(AgentCapabilityRequest(
            "asset.get_profile",
            {"entities": ["10.0.0.1"], "views": ["identity"], "detail": "standard"},
        ),),
    )

    class Executor:
        calls = 0

        def execute(self, *_args: object, **_kwargs: object) -> list[ToolResult]:
            self.calls += 1
            return []

    executor = Executor()
    validator = PlanValidator(registry)
    service = SimpleNamespace(
        settings=get_settings(),
        _capability_runtime_snapshot=lambda: (registry, validator, executor),
    )
    nodes = CopilotWorkflowNodes(service)
    loop = AgentLoopState(
        turn=1,
        budget=replace(_budget(), investigator_turns=1, llm_calls=2),
        ledger=ledger,
        latest_decision=decision,
        started_monotonic=time.monotonic(),
    )
    state = {
        "request_id": "repeat-request",
        "trace_id": "repeat-trace",
        "session_id": "repeat-session",
        "task": _task(),
        "request_constraints": RequestConstraints(),
        "agent_loop_state": loop,
        "agent_decision": decision,
    }
    first = nodes.validate_agent_action(state)
    assert first["next_edge"] == "retry"
    assert executor.calls == 0
    reused_loop = first["agent_loop_state"]
    assert reused_loop.ledger.gaps[0].status == "satisfied"
    assert "new-gap" in reused_loop.ledger.evidence_references[0].covered_gap_ids
    assert reused_loop.observations[-1].new_coverage == ("new-gap",)
    assert reused_loop.observations[-1].tool_call_count == 0
    stopped = nodes.evaluate_agent_progress({**state, "agent_loop_state": reused_loop})
    assert stopped["agent_loop_state"].stop_reason == "evidence_sufficient"
    assert executor.calls == 0


def test_repeated_failed_action_is_bounded_and_stops_without_provider_call(
    registry: CapabilityRegistry,
) -> None:
    gap = _ledger().gaps[0]
    decision = AgentContinueDecision(
        kind="CONTINUE",
        evidence_gap_id=gap.gap_id,
        capability_requests=(AgentCapabilityRequest(
            "asset.get_profile",
            {"entities": ["10.0.0.1"], "views": ["identity"], "detail": "standard"},
        ),),
    )
    plan_validator = PlanValidator(registry)
    validated = AgentActionValidator(registry, plan_validator).validate(
        decision,
        task=_task(),
        constraints=RequestConstraints(),
        ledger=_ledger(),
        budget=_budget(),
        turn=1,
    )
    step = validated.plan.steps[0]
    failed_result = _product_result(
        views=tuple(step.arguments["views"]),
        status="unavailable",
        completeness="unknown",
        projection_usable=False,
        source_payload_complete=False,
        step_id=step.id,
    )
    failed_reference = evidence_reference_for_result(
        failed_result,
        (gap,),
        step=step,
        action_fingerprint=validated.fingerprints[0],
    )
    ledger = replace(_ledger(), evidence_references=(failed_reference,))

    class Executor:
        calls = 0

        def execute(self, *_args: object, **_kwargs: object) -> list[ToolResult]:
            self.calls += 1
            return []

    executor = Executor()
    service = SimpleNamespace(
        settings=get_settings(),
        _capability_runtime_snapshot=lambda: (registry, plan_validator, executor),
    )
    nodes = CopilotWorkflowNodes(service)
    loop = AgentLoopState(
        turn=1,
        budget=replace(_budget(), investigator_turns=1, llm_calls=2),
        ledger=ledger,
        latest_decision=decision,
        started_monotonic=time.monotonic(),
    )
    state = {
        "request_id": "failed-repeat-request",
        "trace_id": "failed-repeat-trace",
        "session_id": "failed-repeat-session",
        "task": _task(),
        "request_constraints": RequestConstraints(),
        "agent_loop_state": loop,
        "agent_decision": decision,
    }
    first = nodes.validate_agent_action(state)
    assert first["next_edge"] == "retry"
    assert first["agent_loop_state"].observations[-1].rejected_actions == ("repeated_failed_action",)
    second_loop = replace(
        first["agent_loop_state"],
        turn=2,
        budget=replace(first["agent_loop_state"].budget, investigator_turns=2, llm_calls=3),
        latest_decision=decision,
    )
    second = nodes.validate_agent_action({**state, "agent_loop_state": second_loop})
    assert second["next_edge"] == "retry"
    stopped = nodes.evaluate_agent_progress({**state, "agent_loop_state": second["agent_loop_state"]})
    assert stopped["agent_loop_state"].stop_reason == "no_useful_action"
    assert executor.calls == 0
