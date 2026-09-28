"""Regression matrix for the remaining Phase 4 production-hardening contracts."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest
from pydantic import ValidationError

from src.config.settings import get_settings
from src.core.agent.phase4c_nodes import Phase4CWorkflowNodes
from src.core.agent.contracts import (
    StructuredAssetAggregateEvidence,
    StructuredAssetSearchEvidence,
    TaskSpec,
    ToolResult,
)
from src.core.agent.structured_presentation import render_structured_presentation
from src.core.context.structured_hardening import (
    deterministic_structured_fallback,
    select_structured_query_context,
    structured_reference_scope,
)
from src.core.context.models import EntityResolution
from src.core.context.structured_routing import SemanticIntentRouter
from src.core.context.synthesizer_prompt import SynthesizerPromptBuilder
from src.core.graph.organizational_neo4j import OrganizationalNeo4jGraphRepository
from src.core.graph.structured import (
    AssetAggregateRequest,
    AssetPredicate,
    AssetSearchFilters,
    AssetSearchRequest,
    StructuredQuerySpec,
    structured_query_identity,
)
from src.core.identity import RequestIdentity
from src.core.llm.providers.base import LLMProviderResult
from src.core.memory.episodes import MemoryContextKey
from src.core.memory.persistence import THREAD_STATE_SCHEMA_VERSION, ThreadMemoryState
from src.core.memory.product import _ref_to_wire, _refs_from_wire
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.store import MemoryStore
from src.core.memory.structured_query import StructuredAssetRef, StructuredQueryContext


def _search_context(role: str, request_id: str) -> StructuredQueryContext:
    query = StructuredQuerySpec.model_validate(
        {"mode": "search", "filters": {"role": role}}
    )
    identity = structured_query_identity(query, active_graph_version="graph-v1")
    return StructuredQueryContext.create(
        query_identity=identity,
        active_graph_version="graph-v1",
        query=query,
        matched_total=1,
        returned_count=1,
        result_refs=(StructuredAssetRef(ip="192.0.2.1"),),
        source_request_id=request_id,
        retrieved_at="2026-09-14T10:00:00+00:00",
        created_at="2026-09-14T10:00:00+00:00",
    )


def test_boolean_contract_keeps_legacy_flat_filters_and_supports_nested_ast() -> None:
    legacy = AssetSearchFilters(role="Linux Server", model_confidence_min=0.9)
    assert legacy.role == "Linux Server"
    assert legacy.predicate is None

    filters = AssetSearchFilters.model_validate({
        "predicate": {
            "all": [
                {"field": "role", "operator": "eq", "value": "Linux Server"},
                {
                    "any": [
                        {"field": "vendor", "operator": "eq", "value": "VMware"},
                        {"field": "vendor", "operator": "eq", "value": "Microsoft"},
                    ]
                },
            ]
        }
    })
    assert filters.predicate is not None
    assert filters.predicate.shape() == (3, 3)


@pytest.mark.parametrize(
    "predicate",
    (
        {"field": "password", "operator": "eq", "value": "secret"},
        {"field": "role", "operator": "contains", "value": "server"},
        {"field": "role", "operator": "gt", "value": "server"},
    ),
)
def test_boolean_contract_rejects_unknown_or_mismatched_field_operators(predicate) -> None:
    with pytest.raises(ValidationError):
        AssetSearchFilters.model_validate({"predicate": predicate})


def test_boolean_contract_rejects_excessive_depth_and_leaf_count() -> None:
    leaf = {"field": "role", "operator": "eq", "value": "Linux Server"}
    too_deep = {"all": [leaf, {"all": [leaf, {"all": [leaf, leaf]}]}]}
    with pytest.raises(ValidationError, match="nesting is too deep"):
        AssetSearchFilters.model_validate({"predicate": too_deep})

    too_many = {"all": [
        {"field": "vendor", "operator": "eq", "value": f"vendor-{index}"}
        for index in range(25)
    ]}
    with pytest.raises(ValidationError):
        AssetSearchFilters.model_validate({"predicate": too_many})


def test_boolean_values_are_parameters_and_never_enter_cypher() -> None:
    injection = "VMware' OR 1=1 //"
    request = AssetSearchRequest(filters=AssetSearchFilters(
        predicate=AssetPredicate.model_validate(
            {"field": "vendor", "operator": "eq", "value": injection}
        )
    ))
    clauses, params = OrganizationalNeo4jGraphRepository._structured_filter_clauses(request)
    cypher = " ".join(clauses)
    assert injection not in cypher
    assert injection.casefold() in params.values()
    assert "toLower(coalesce(a.vendor, ''))" in cypher


@pytest.mark.parametrize(
    ("fields", "valid"),
    ((["role"], True), (["role", "status"], True),
     (["role", "status", "vendor"], True), (["role", "role"], False),
     (["role", "status", "vendor", "product"], False)),
)
def test_multi_group_contract_is_bounded_and_unique(fields, valid) -> None:
    payload = {
        "operation": "group_count",
        "group_by_fields": fields,
        "filters": {"status": "confirmed"},
    }
    if valid:
        request = AssetAggregateRequest.model_validate(payload)
        assert [item.value for item in request.group_fields] == fields
    else:
        with pytest.raises(ValidationError):
            AssetAggregateRequest.model_validate(payload)


def test_single_group_legacy_and_new_shape_have_the_same_identity() -> None:
    old = StructuredQuerySpec.model_validate({
        "mode": "aggregate", "operation": "group_count", "group_by": "role"
    })
    new = StructuredQuerySpec.model_validate({
        "mode": "aggregate", "operation": "group_count", "group_by_fields": ["role"]
    })
    assert structured_query_identity(old, active_graph_version="v1") == structured_query_identity(
        new, active_graph_version="v1"
    )


def test_deterministic_compiler_handles_complex_and_multi_group_requests() -> None:
    complex_result = deterministic_structured_fallback(
        "Find CONFIRMED Linux Server assets with model confidence above 90% and "
        "either vendor VMware or vendor Microsoft."
    )
    assert complex_result is not None
    assert complex_result.query.filters.role is None
    assert complex_result.query.semantic_class == "Linux Server"
    assert complex_result.query.filters.predicate is not None
    predicate = complex_result.query.filters.predicate
    assert predicate.all
    assert any(
        child.operator is not None and child.operator.value == "in"
        for child in predicate.all
    )

    grouped = deterministic_structured_fallback(
        "Group all assets by role and status. For each group show the exact count."
    )
    assert grouped is not None
    assert [item.value for item in grouped.query.group_by_fields] == ["role", "status"]
    assert grouped.query.filters.role is None


@pytest.mark.parametrize(
    "message",
    (
        "Find Linux Server assets with high confidence.",
        "Find Linux Server assets owned by Alice.",
    ),
)
def test_deterministic_compiler_does_not_drop_or_invent_material_selectors(message: str) -> None:
    assert deterministic_structured_fallback(message) is None


@pytest.mark.parametrize(
    "message",
    (
        "Find CONFIRMED Linux Server assets with model confidence above 90% and "
        "either vendor VMware or vendor Microsoft.",
        "Group all assets by role and status.",
    ),
)
def test_supported_structured_prompts_use_semantic_router_as_primary(message: str) -> None:
    aggregate = message.startswith("Group")
    payload = {
        "intent": "asset_aggregate" if aggregate else "asset_search",
        "scope": "none",
        "direction": "none",
        "depth": 0,
        "requires_graph": True,
        "requires_detection": False,
        "requires_asset_profile": False,
        "requires_knowledge": False,
        "structured_query": (
            {
                "mode": "aggregate",
                "filters": {},
                "operation": "group_count",
                "group_by_fields": ["role", "status"],
            }
            if aggregate
            else {
                "mode": "search",
                "filters": {
                    "status": "confirmed",
                    "role": "Linux Server",
                    "model_confidence_min": 0.9,
                    "predicate": {
                        "field": "vendor",
                        "operator": "in",
                        "values": ["VMware", "Microsoft"],
                    },
                },
            }
        ),
        "structured_result_reference": None,
        "entity_binding": "none",
        "requires_multiple_entities": False,
        "is_followup": False,
        "reason": "Bounded structured Asset query.",
    }

    class RecordingModel:
        calls = 0

        def chat(self, *_args, **_kwargs):
            self.calls += 1
            return LLMProviderResult(
                text=json.dumps(payload),
                provider="fake",
                model="fake",
                finish_reason="stop",
                usage={},
                status_code=200,
            )

    settings = replace(get_settings(), intent_router_enabled=True)
    model = RecordingModel()
    decision = SemanticIntentRouter(settings, model).classify(
        message,
        EntityResolution(status="none", entity_mode="none"),
        SessionRoutingState(),
        request_id="deterministic-router-test",
    )
    assert model.calls == 1
    assert decision.router_called is True
    assert decision.decision_source == "semantic_router"
    assert decision.fallback_used is False
    assert decision.structured_query is not None


def test_same_turn_pronouns_do_not_bind_previous_set_but_prior_turn_pronouns_do() -> None:
    previous = _search_context("Database Server", "r0")
    same_turn = deterministic_structured_fallback(
        "Find all assets last detected after 2026-09-10T08:00:00+00:00 and "
        "group them by their primary role and status.",
        prior_query=previous.query,
    )
    assert same_turn is not None
    assert same_turn.reference.kind == "none"
    assert same_turn.query.filters.role is None
    assert structured_reference_scope("Find Linux Server assets and show their confidence") == "same_turn"

    prior_turn = deterministic_structured_fallback(
        "Which of them have model confidence under 90%?", prior_query=previous.query
    )
    assert prior_turn is not None
    assert prior_turn.reference.kind == "set_query"
    assert prior_turn.query.filters.role == "database server"


def test_structured_lineage_selects_base_or_latest_and_survives_persistence() -> None:
    base = _search_context("Linux Server", "r0")
    latest = _search_context("Database Server", "r1")
    routing = SessionRoutingState(
        structured_query_context=latest,
        structured_query_lineage=(base, latest),
    )
    assert select_structured_query_context(routing, "Group the original set by vendor") == base
    assert select_structured_query_context(routing, "Use the latest set") == latest

    identity = RequestIdentity.resolve(
        user_id="owner", conversation_id="chat-lineage", session_id="session-lineage"
    )
    state = ThreadMemoryState.from_routing_state(identity, routing)
    restored = ThreadMemoryState.from_payload(
        state.to_payload(), thread_key=identity.thread_key, user_id=identity.user_id,
        conversation_id=identity.conversation_id, session_id=identity.session_id,
        updated_at=state.updated_at, revision=state.revision,
        schema_version=THREAD_STATE_SCHEMA_VERSION,
    ).to_routing_state()
    assert [item.source_request_id for item in restored.structured_query_lineage] == ["r0", "r1"]


def test_ranked_ties_never_invent_a_unique_winner() -> None:
    task = TaskSpec(
        request="Find the Firewall with the highest model confidence and analyze it.",
        intent="asset_search", scope="none", direction="none", entities=(),
        required_capabilities=("graph.search_assets",),
        structured_query=StructuredQuerySpec.model_validate({
            "mode": "search", "filters": {"role": "Firewall"},
            "sort": "model_confidence", "direction": "desc", "limit": 3,
        }),
    )
    two_tied = (
        {"ip": "192.0.2.1", "model_confidence": 0.99},
        {"ip": "192.0.2.2", "model_confidence": 0.99},
        {"ip": "192.0.2.3", "model_confidence": 0.95},
    )
    assert Phase4CWorkflowNodes._select_candidates(
        task, two_tied, retrieval_truncated=True, matched_total=9
    ) == (("192.0.2.1", "192.0.2.2"), "exact_top_two_tie")

    three_tied = tuple(
        {"ip": f"192.0.2.{index}", "model_confidence": 0.99}
        for index in range(1, 4)
    )
    selected, reason = Phase4CWorkflowNodes._select_candidates(
        task, three_tied, retrieval_truncated=True, matched_total=9
    )
    assert selected == ()
    assert reason == "ranked_top_tie_requires_secondary_criterion"


def test_synth_contract_states_previous_set_used_even_when_current_matches_zero() -> None:
    previous = _search_context("Linux Server", "r0")
    current_query = StructuredQuerySpec.model_validate({
        "mode": "search", "filters": {"role": "Linux Server", "mapping_confidence_max": 0.1}
    })
    current_identity = structured_query_identity(current_query, active_graph_version="graph-v1")
    evidence = StructuredAssetSearchEvidence(
        capability="graph.search_assets", query_identity=current_identity,
        normalized_filters=current_query.filters.model_dump(mode="json", exclude_none=True),
        active_graph_version="graph-v1", sort="graph_key", direction="asc",
        matched_total=0, returned_count=0, truncated=False, rows=(),
        retrieved_at="2026-09-14T10:01:00+00:00",
    )
    result = ToolResult(
        status="ok", entities=(), source_capability="graph.search_assets",
        retrieved_at=evidence.retrieved_at, freshness="current", completeness="complete",
        structured_asset_set=evidence,
    )
    task = TaskSpec(
        request="Which of them have mapping confidence under 10%?", intent="asset_search",
        scope="none", direction="none", entities=(),
        required_capabilities=("graph.search_assets",), structured_query=current_query,
    )

    context = SynthesizerPromptBuilder().build_context(
        task, (result,), structured_context=previous,
        structured_lineage=(previous,), structured_reference_kind="set_query",
    )
    contract, _modules = SynthesizerPromptBuilder().render_contract(context)

    assert context.continuity.previous_structured_set_available is True
    assert context.continuity.previous_structured_set_used is True
    assert context.continuity.previous_result_count == 1
    assert context.continuity.current_result_count == 0
    assert '"previous_structured_set_used":true' in contract


def test_aggregate_presentation_distinguishes_exact_counts_from_bounded_members() -> None:
    evidence = StructuredAssetAggregateEvidence(
        capability="graph.aggregate_assets", query_identity="structured-asset-set:v1:" + "0" * 64,
        normalized_filters={}, active_graph_version="graph-v1", operation="group_count",
        group_by=None, group_by_fields=("role", "status"), count=27,
        groups=({
            "group_values": {"role": "Linux Server", "status": "confirmed"},
            "count": 27, "percentage_of_total": 100.0,
            "member_ips": tuple(f"192.0.2.{index}" for index in range(1, 21)),
            "member_ips_truncated": True,
        },), truncated=False, retrieved_at="2026-09-14T10:00:00+00:00",
    )
    result = ToolResult(
        status="ok", entities=(), source_capability="graph.aggregate_assets",
        retrieved_at=evidence.retrieved_at, freshness="current", completeness="complete",
        structured_asset_set=evidence,
    )

    answer = render_structured_presentation((result,))

    assert answer is not None
    assert "exact filtered total is 27 assets" in answer
    assert "bounded returned identities" in answer
    assert "each group count remains exact" in answer


def test_synth_output_rule_forbids_unique_rank_claim_for_equal_primary_scores() -> None:
    text = open(
        "app/prompts/synthesizer/output_constraints.md", encoding="utf-8"
    ).read().casefold()
    assert "equal primary-score ties" in text
    assert "never describe one tied asset as uniquely highest or lowest" in text


@pytest.mark.parametrize("ref_type", ("chat", "asset"))
def test_product_typed_evidence_refs_round_trip_losslessly(ref_type: str) -> None:
    internal = _refs_from_wire([{"type": ref_type, "id": "backend/id:42"}])
    assert len(internal) == 1
    assert _ref_to_wire(internal[0]) == {"type": ref_type, "id": "backend/id:42"}


@pytest.mark.parametrize("target", (1, 2, 4, 6))
def test_recent_raw_setting_is_an_exact_message_count(target: int) -> None:
    settings = replace(
        get_settings(),
        conversation_recent_raw_messages=target,
        memory_relevant_turn_limit=8,
        memory_relevant_turn_token_budget=10_000,
        memory_context_token_budget=12_000,
    )
    memory = MemoryStore(20)
    key = MemoryContextKey(topic_family="general")
    for index in range(3):
        memory.record_turn(
            "session", f"user-{index}", f"assistant-{index}", key,
            request_id=f"request-{index}",
        )
    package = memory.compose_memory_context(
        "session", settings, context_key=key, request_id="current"
    )
    raw_count = sum(
        source == "raw"
        for turn in package.relevant_turns
        for source in (turn.user_source_representation, turn.assistant_source_representation)
    )
    assert raw_count == target


def test_oversized_recent_raw_turn_downgrades_to_bounded_digest_before_omission() -> None:
    settings = replace(
        get_settings(),
        conversation_recent_raw_messages=2,
        memory_relevant_turn_limit=4,
        memory_relevant_turn_token_budget=150,
        memory_context_token_budget=180,
    )
    memory = MemoryStore(20)
    key = MemoryContextKey(topic_family="general")
    memory.record_turn(
        "session", "newest user detail " * 200, "newest assistant detail " * 200,
        key, request_id="latest",
    )

    package = memory.compose_memory_context(
        "session", settings, context_key=key, request_id="current"
    )

    assert [turn.request_id for turn in package.relevant_turns] == ["latest"]
    assert package.relevant_turns[0].source_representation == "digest"
    assert package.relevant_turns[0].estimated_tokens <= 150
    assert package.estimated_tokens <= 180
