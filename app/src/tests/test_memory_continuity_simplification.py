"""Regression invariants for deterministic conversation continuity."""

from __future__ import annotations

from src.core.agent.task_mapping import (
    derive_request_constraints,
    derive_task_envelope,
    derive_turn_policy,
    enforce_task_envelope,
    materialize_turn_policy_target,
    task_spec_from_route,
)
from src.core.agent.registry import _provider_result
from src.core.context.entities import EntityResolver
from src.core.context.compaction import current_evidence_projections
from src.core.context.models import GraphProviderResult
from src.core.context.product_views import build_product_view
from src.core.context.router import DeterministicFallbackRouter
from src.core.memory.episodes import (
    EntityVisit,
    MemoryContextKey,
    MemoryContextPackage,
    RelevantTurn,
    WorkingFact,
)
from src.core.memory.persistence import ThreadMemoryState
from src.core.memory.product import ProductMemoryContractError, _memory_from_wire
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.store import MemoryStore, extract_working_facts


IP_A = "192.168.20.103"
IP_B = "192.168.20.120"
IP_C = "192.168.20.149"


def test_explicit_remember_turn_preserves_multiple_exact_statements() -> None:
    facts = extract_working_facts(
        "Remember, I'm Alireza Hashemi. "
        'Also remember for this investigation that the analyst label for '
        f'{IP_A} is "vxidalira legacy integration server".'
    )

    assert len(facts) == 2
    assert facts[0].key == "analyst_name"
    assert facts[0].value == "Alireza Hashemi"
    assert facts[1].key.startswith("remembered_statement:")
    assert facts[1].value == (
        f'the analyst label for {IP_A} is "vxidalira legacy integration server"'
    )
    assert facts[1].fact_type == "user_provided"


def test_explicit_remember_uses_one_exact_statement_when_segmentation_is_uncertain() -> None:
    facts = extract_working_facts(
        "Remember that the legacy integration server is temporarily isolated and "
        "should remain excluded from the migration."
    )

    assert len(facts) == 1
    assert facts[0].value == (
        "the legacy integration server is temporarily isolated and should remain "
        "excluded from the migration"
    )


def test_timeline_round_trip_is_backward_compatible_and_preserves_revisits() -> None:
    timeline = (
        EntityVisit(sequence=1, ordered_entity_ids=(IP_A,), task_family="asset_investigation"),
        EntityVisit(sequence=2, ordered_entity_ids=(IP_B, IP_A), task_family="asset_comparison"),
        EntityVisit(sequence=3, ordered_entity_ids=(IP_C,), task_family="asset_investigation"),
        EntityVisit(sequence=4, ordered_entity_ids=(IP_A,), task_family="asset_investigation"),
    )
    state = ThreadMemoryState(
        thread_key="thread-1",
        user_id="user-1",
        conversation_id="conversation-1",
        session_id="session-1",
        entity_timeline=timeline,
    )

    restored = ThreadMemoryState.from_payload(
        state.to_payload(),
        thread_key=state.thread_key,
        user_id=state.user_id,
        conversation_id=state.conversation_id,
        session_id=state.session_id,
        updated_at="now",
        revision=1,
        schema_version=state.schema_version,
    )

    assert restored.entity_timeline == timeline
    assert ThreadMemoryState.from_payload(
        {},
        thread_key=state.thread_key,
        user_id=state.user_id,
        conversation_id=state.conversation_id,
        session_id=state.session_id,
        updated_at="now",
        revision=0,
        schema_version=state.schema_version,
    ).entity_timeline == ()


def test_ordinal_references_resolve_from_timeline_not_active_cursor() -> None:
    state = SessionRoutingState(
        active_entities=(IP_A,),
        entity_timeline=(
            EntityVisit(sequence=1, ordered_entity_ids=(IP_A,), task_family="asset_investigation"),
            EntityVisit(sequence=2, ordered_entity_ids=(IP_B,), task_family="asset_investigation"),
            EntityVisit(sequence=3, ordered_entity_ids=(IP_C,), task_family="asset_investigation"),
        ),
    )
    resolver = EntityResolver()

    first = resolver.resolve("What did we conclude about the first asset?", routing_state=state)
    second = resolver.resolve("What did we conclude about the second asset?", routing_state=state)
    previous = resolver.resolve("What did we conclude about the previous asset?", routing_state=state)
    last = resolver.resolve("What did we conclude about the last investigated asset?", routing_state=state)

    assert [item.value for item in first.entities] == [IP_A]
    assert [item.value for item in second.entities] == [IP_B]
    assert [item.value for item in previous.entities] == [IP_B]
    assert [item.value for item in last.entities] == [IP_C]


def test_task_envelope_preserves_pair_comparison_through_current_fallback() -> None:
    message = "Verify whether those conclusions are still true now."
    state = SessionRoutingState(
        active_entities=(IP_B, IP_A),
        previous_intent="graph_relationships",
        previous_scope="multi_entity_comparison",
        last_providers=("graph",),
    )
    resolver = EntityResolver()
    raw = resolver.resolve(message, routing_state=state)
    constraints = derive_request_constraints(message)
    policy = derive_turn_policy(message, constraints, raw, state)
    resolved = materialize_turn_policy_target(raw, policy)
    envelope = derive_task_envelope(resolved, constraints, policy, state)
    fallback = DeterministicFallbackRouter().route(
        message,
        resolved,
        state,
        fallback_reason="router_timeout",
        constraints=constraints,
        turn_policy=policy,
    )

    preserved = enforce_task_envelope(fallback, envelope)
    task = task_spec_from_route(
        fallback, message, constraints, policy, envelope
    )

    assert envelope.ordered_entities == (IP_B, IP_A)
    assert envelope.comparison_required
    assert preserved.scope == "multi_entity_comparison"
    assert preserved.materialized_entities == (IP_B, IP_A)
    assert task.required_capabilities == ("graph.compare_assets",)
    assert task.workflow_mode == "direct"


def test_evidence_receipt_keeps_full_product_and_graph_baselines_immutable() -> None:
    product_payload = {"role": "server", "risk": 7}
    product_provider = type(
        "ProductProvider", (), {"status": "available", "provider": "asset_profile", "raw_payload": product_payload}
    )()
    product_view = build_product_view(
        product_payload,
        provider="asset_profile",
        views=("full",),
        detail="deep",
        max_context_tokens=1000,
        purpose="asset_summary",
    )
    product = _provider_result(
        "asset.get_profile", (IP_A,), product_provider, evidence_view=product_view
    )
    graph_context = {
        "target_ip": IP_A,
        "requested_scope": "node_summary",
        "direction": "both",
        "depth": 0,
        "complete_for_user_request": True,
        "retrieval_truncated": False,
        "inbound_total": 1,
        "nodes": [{"id": IP_A}],
    }
    graph_provider = GraphProviderResult(
        provider="graph", status="available", context=graph_context
    )
    graph = _provider_result("graph.get_summary", (IP_A,), graph_provider)

    # Presentation compaction mutates this legacy provider dict after acquisition.
    graph_context["complete_for_user_request"] = False
    graph_context["inbound_total"] = 999
    projections = current_evidence_projections((product, graph), owner_id="user-1")

    assert product.evidence_receipt is not None
    assert graph.evidence_receipt is not None
    assert projections[0].view == "full"
    assert projections[0].payload == product_payload
    assert projections[1].complete
    assert projections[1].payload["counts"]["inbound_total"] == 1


def test_durable_turn_digests_restore_when_the_local_transcript_is_unavailable() -> None:
    key = SessionRoutingState(active_entities=(IP_A,))
    memory = MemoryStore(20)
    context_key = MemoryContextKey((IP_A,), "asset_investigation", "none", "asset")
    memory.record_turn(
        "session-digest",
        "What is the retained risk for this asset?",
        "The retained assessment is that the asset is monitored.",
        context_key,
        request_id="digest-turn",
    )
    state = ThreadMemoryState(
        thread_key="thread-digest",
        user_id="user-digest",
        conversation_id="conversation-digest",
        session_id="session-digest",
        **memory.durable_components("session-digest", turn_limit=5, episode_limit=2),
    )
    restored = MemoryStore(20)
    restored.restore_durable_state(state)

    package = restored.compose_memory_context(
        "session-digest",
        type("Settings", (), {
            "memory_relevant_turn_limit": 5,
            "memory_relevant_turn_token_budget": 900,
            "memory_episode_context_limit": 2,
            "memory_episode_context_token_budget": 300,
            "memory_context_token_budget": 1400,
            "memory_context_long_term_token_budget": 500,
        })(),
        context_key=context_key,
        active_entities=key.active_entities,
    )

    assert package.relevant_turns[0].retrieval_reason == "active_topic"
    assert package.relevant_turns[0].user_content.startswith("What is the retained")
    assert state.recent_turn_references[0].workflow_status == "completed"


def test_product_contract_failures_have_a_distinct_error_type() -> None:
    try:
        _memory_from_wire({"memory": {}})
    except ProductMemoryContractError:
        return
    raise AssertionError("invalid Product memory wire response must not be a local persistence error")


def test_memory_context_sections_state_their_source_and_authority() -> None:
    package = MemoryContextPackage(
        working_facts=(
            WorkingFact(key="remembered_statement:test", value="operator note"),
        ),
        relevant_turns=(
            RelevantTurn(
                request_id="turn", context_key=MemoryContextKey(), user_content="prior user",
                assistant_content="prior assistant", created_at="now", retrieval_reason="recent", estimated_tokens=4,
            ),
        ),
    )
    rendered = "\n".join(message["content"] for message in package.model_messages())

    assert "USER-PROVIDED WORKING FACTS" in rendered
    assert "SHORT-TERM RECENT TURN CONTEXT" in rendered
    assert "not independent current operational evidence" in rendered
