"""Focused offline regressions for deterministic memory-only continuity."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.config.settings import get_settings
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.agent.task_mapping import (
    classify_historical_recall,
    compile_direct_plan,
    derive_request_constraints,
    task_spec_from_route,
)
from src.core.context.entities import EntityResolver
from src.core.context.models import EntityResolution, IntentDecision, ResolvedEntity
from src.core.memory.episodes import MemoryContextKey
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.store import MemoryStore, extract_working_facts


def _asset_route(entity: str = "192.168.30.115") -> SimpleNamespace:
    return SimpleNamespace(
        materialized_entities=(entity,),
        intent="asset_investigation",
        scope="node_summary",
        direction="both",
        depth=0,
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


def test_t3_memory_only_preserves_active_entity_and_has_zero_live_steps() -> None:
    active = SessionRoutingState(active_ip="192.168.30.115")
    resolution = EntityResolver().resolve(
        "What did I tell you earlier? Memory only.",
        routing_state=active,
    )
    task = task_spec_from_route(_asset_route(), "What did I tell you earlier? Memory only.")

    assert [item.value for item in resolution.entities] == ["192.168.30.115"]
    assert task.intent == "memory_recall"
    assert task.evidence_mode == "memory_only"
    assert compile_direct_plan(task).steps == ()


def test_t4_no_live_reassessment_preserves_asset_investigation_and_has_zero_live_steps() -> None:
    task = task_spec_from_route(
        _asset_route(),
        "Reassess this asset without refreshing or calling live Product, Detection, Graph, or Knowledge.",
    )

    assert task.intent == "asset_investigation"
    assert task.entities == ("192.168.30.115",)
    assert task.evidence_mode == "no_live_refresh"
    assert compile_direct_plan(task).steps == ()


def test_memory_only_same_asset_keeps_the_active_episode() -> None:
    settings = get_settings()
    memory = MemoryStore(20)
    asset_key = MemoryContextKey(("192.168.30.115",), "asset_investigation", "none", "asset")
    memory.prepare_for_model(
        "episode",
        settings,
        SessionRoutingState(active_entities=asset_key.entities),
        context_key=asset_key,
    )
    memory.record_turn("episode", "Analyze the asset.", "Asset context recorded.", asset_key)
    episode_id = memory.repository.get_working("episode").episode_id  # type: ignore[union-attr]
    recall_task = task_spec_from_route(_asset_route(), "What was the previous contradiction? Memory only.")
    recall_key = MemoryContextKey.from_task(recall_task)

    snapshot = memory.prepare_for_model(
        "episode",
        settings,
        SessionRoutingState(active_entities=asset_key.entities),
        context_key=recall_key,
    )

    assert recall_key == asset_key
    assert not snapshot.episode_transition
    assert memory.repository.get_working("episode").episode_id == episode_id  # type: ignore[union-attr]


def test_router_none_cannot_erase_resolved_entity_for_memory_only_request() -> None:
    settings = get_settings()
    decision = IntentDecision(
        intent="general_knowledge",
        scope="none",
        direction="none",
        depth=0,
        requires_graph=False,
        entity_binding="none",
        classification_confidence=0.99,
        decision_source="semantic_router",
    )
    service = SimpleNamespace(
        settings=settings,
        intent_router=SimpleNamespace(classify=lambda *_args, **_kwargs: decision),
        fallback_router=SimpleNamespace(),
    )
    nodes = CopilotWorkflowNodes(service)
    entity = ResolvedEntity(type="ip", value="192.168.30.115", source="conversation")
    original = EntityResolution(
        status="resolved",
        entities=[entity],
        primary_entity=entity,
        entity_mode="single",
        candidate_count=1,
        valid_entity_count=1,
        reference_detected=True,
    )

    update = nodes.route(
        {
            "message": "What was the previous contradiction? Without refreshing.",
            "resolved_entities": original,
            "active_entity_state": SessionRoutingState(active_ip=entity.value),
            "recent_messages": [],
            "ui_context": None,
            "trace_id": "trace",
            "request_id": "request",
        }
    )

    assert update["routing_result"].materialized_entities == (entity.value,)
    assert update["routing_result"].binding_normalization_reason == "memory_request_preserves_resolved_entity"
    assert update["resolved_entities"].primary_entity == entity


def test_t4b_compaction_and_episode_archive_keep_exact_contradiction() -> None:
    settings = replace(
        get_settings(),
        conversation_summary_enabled=True,
        conversation_summary_trigger_tokens=10,
        conversation_recent_raw_messages=2,
        conversation_summary_max_tokens=120,
    )
    key = MemoryContextKey(("192.168.30.115",), "asset_investigation", "none", "asset")
    memory = MemoryStore(20)
    memory.prepare_for_model("t4b", settings, SessionRoutingState(active_entities=key.entities), context_key=key)
    memory.append("t4b", "user", "What contradiction did you find?")
    memory.append("t4b", "assistant", "The key contradiction is Windows XP fingerprint vs Chrome 150 / Windows NT 10.0.")
    memory.append("t4b", "user", "Please keep this investigation context.")
    memory.append("t4b", "assistant", "Next check: verify the endpoint identity with current evidence.")
    memory.compact_if_needed("t4b", settings, SessionRoutingState(active_entities=key.entities))

    snapshot = memory.prepare_for_model(
        "t4b",
        settings,
        SessionRoutingState(active_entities=key.entities),
        context_key=MemoryContextKey((), "general", "none", "none"),
    )
    archived = memory.repository.list_episodes("t4b")[-1]

    assert "Windows XP fingerprint vs Chrome 150 / Windows NT 10.0" in archived.compact_summary
    assert snapshot.episode_transition


def test_t4c_sticky_user_fact_survives_compaction_without_long_term_memory() -> None:
    settings = replace(
        get_settings(),
        conversation_summary_enabled=True,
        conversation_summary_trigger_tokens=10,
        conversation_recent_raw_messages=2,
        conversation_summary_max_tokens=120,
    )
    memory = MemoryStore(20)
    memory.append("t4c", "user", "Remember my name is alira hshmi.")
    memory.append("t4c", "assistant", "I will retain that within this conversation.")
    memory.append("t4c", "user", "Continue the cybersecurity discussion.")
    memory.append("t4c", "assistant", "Continuing the discussion.")

    snapshot = memory.prepare_for_model("t4c", settings, SessionRoutingState())
    rendered = "\n".join(item["content"] for item in snapshot.messages)

    assert "alira hshmi" in rendered
    assert "[SOORIN VALIDATED LONG-TERM MEMORY]" not in rendered
    assert snapshot.memory_context is not None
    assert snapshot.memory_context.long_term_memories == ()


def test_m1_explicit_investigation_facts_are_typed_and_use_no_tools() -> None:
    message = (
        "For this investigation only, remember: tag is ORION-115, owner validation is pending, "
        "and identity contradiction is Windows XP SP3 fingerprint versus Chrome 150 / Windows NT 10.0 user-agent. "
        "Keep these in this conversation only."
    )
    constraints = derive_request_constraints(message)
    facts = {item.key: item.value for item in extract_working_facts(message)}
    task = task_spec_from_route(_asset_route(), message, constraints)
    resolution = EntityResolver().resolve(
        message,
        routing_state=SessionRoutingState(active_entities=("192.168.30.115",)),
    )

    assert constraints.memory_write and constraints.memory_only
    assert constraints.allow_live is False
    assert facts == {
        "investigation_tag": "ORION-115",
        "owner_validation": "pending",
        "identity_contradiction": (
            "Windows XP SP3 fingerprint versus Chrome 150 / Windows NT 10.0 user-agent"
        ),
    }
    assert compile_direct_plan(task).steps == ()
    assert [item.value for item in resolution.entities] == ["192.168.30.115"]


def test_m2_deterministic_recall_bypasses_router_and_selects_working_facts() -> None:
    message = (
        "What investigation tag, owner validation status, and exact unresolved identity "
        "contradiction do we have for this asset?"
    )
    entity = ResolvedEntity(type="ip", value="192.168.30.115", source="conversation")
    resolution = EntityResolution(
        status="resolved",
        entities=[entity],
        primary_entity=entity,
        entity_mode="single",
        candidate_count=1,
        valid_entity_count=1,
        reference_detected=True,
    )
    service = SimpleNamespace(
        settings=get_settings(),
        intent_router=SimpleNamespace(
            classify=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("router called"))
        ),
        fallback_router=SimpleNamespace(),
    )
    update = CopilotWorkflowNodes(service).route(
        {
            "message": message,
            "resolved_entities": resolution,
            "active_entity_state": SessionRoutingState(active_entities=(entity.value,)),
            "recent_messages": [],
            "ui_context": None,
            "trace_id": "trace",
            "request_id": "request",
            "request_constraints": derive_request_constraints(message),
        }
    )

    assert update["routing_result"].semantic_router_called is False
    assert update["routing_result"].materialized_entities == (entity.value,)


def test_m3_typed_working_facts_survive_durable_state_restore() -> None:
    settings = get_settings()
    key = MemoryContextKey(("192.168.30.115",), "asset_investigation", "none", "asset")
    first = MemoryStore(20)
    facts = extract_working_facts(
        "Remember: tag is ORION-115 and owner validation is pending for this conversation."
    )
    first.upsert_working_facts("durable", key, facts)
    first.record_turn("durable", "Remember these facts.", "Stored.", key, request_id="turn-1")
    from src.core.identity import RequestIdentity
    from src.core.memory.persistence import ThreadMemoryState

    identity = RequestIdentity.resolve(
        session_id="durable",
        request_id="persist",
        user_id="user-a",
        conversation_id="conversation-a",
    )
    state = ThreadMemoryState.from_routing_state(
        identity,
        SessionRoutingState(active_entities=key.entities),
        **first.durable_components("durable", turn_limit=4, episode_limit=4),
    )
    restored = ThreadMemoryState.from_payload(
        state.to_payload(),
        thread_key=state.thread_key,
        user_id=state.user_id,
        conversation_id=state.conversation_id,
        session_id=state.session_id,
        updated_at=state.updated_at,
        revision=1,
        schema_version=state.schema_version,
    )
    second = MemoryStore(20)
    second.restore_durable_state(restored)
    package = second.compose_memory_context(
        "durable",
        settings,
        context_key=key,
        active_entities=key.entities,
    )

    assert {item.key: item.value for item in package.working_facts} == {
        "investigation_tag": "ORION-115",
        "owner_validation": "pending for this conversation",
    }


def test_m4_current_reassessment_keeps_live_capabilities_and_historical_baseline() -> None:
    message = "Check whether this asset is still showing the same identity contradiction."
    constraints = derive_request_constraints(message)
    task = task_spec_from_route(_asset_route(), message, constraints)

    assert constraints.require_current and constraints.allow_live
    assert task.evidence_mode == "current_verification"
    assert task.required_capabilities
    assert compile_direct_plan(task).steps


def test_m5_current_task_with_name_write_is_not_memory_only() -> None:
    message = (
        "Check the current state of this asset and also remember that my name is "
        "alira hshmi for this conversation."
    )
    constraints = derive_request_constraints(message)
    facts = extract_working_facts(message)

    assert constraints.require_current and constraints.allow_live
    assert constraints.memory_write and not constraints.memory_only
    assert [(item.key, item.value) for item in facts] == [("analyst_name", "alira hshmi")]


def test_m6_working_facts_are_conversation_scoped() -> None:
    key = MemoryContextKey(("192.168.30.115",), "asset_investigation", "none", "asset")
    memory = MemoryStore(20)
    memory.upsert_working_facts(
        "conversation-a",
        key,
        extract_working_facts("Remember my name is alira hshmi for this conversation."),
    )

    other = memory.compose_memory_context(
        "conversation-b",
        get_settings(),
        context_key=key,
        active_entities=key.entities,
    )

    assert other.working_facts == ()


@pytest.mark.parametrize(
    "message",
    (
        "Without performing any live lookup, what do you remember from this investigation?",
        "Without checking any current status, use only stored context from our previous investigation.",
        "Do not retrieve live data; tell me what investigation state you retained.",
        "Historical only: remind me what we knew about this asset.",
        "Based only on what we discussed, what did we conclude?",
    ),
)
def test_no_live_negation_wins_over_embedded_current_trigger_words(message: str) -> None:
    constraints = derive_request_constraints(message)
    assert not constraints.allow_live
    assert not constraints.require_current
    assert constraints.memory_only


def test_retained_state_recall_bypasses_router_and_resolves_active_asset() -> None:
    message = "Tell me what investigation state you retained after I signed back in."
    active = SessionRoutingState(active_entities=("192.168.30.115",))
    resolution = EntityResolver().resolve(message, routing_state=active)
    constraints = derive_request_constraints(message)

    assert classify_historical_recall(message) == "explicit_memory"
    assert constraints.memory_only
    assert [item.value for item in resolution.entities] == ["192.168.30.115"]
    assert resolution.reference_detected


def test_asset_scoped_working_facts_do_not_leak_when_switching_assets() -> None:
    settings = get_settings()
    memory = MemoryStore(20)
    asset_a = MemoryContextKey(("192.168.30.115",), "asset_investigation", "none", "asset")
    asset_b = MemoryContextKey(("192.168.30.116",), "asset_investigation", "none", "asset")
    facts = tuple(
        replace(fact, scope="entity", entity_ids=asset_a.entities)
        for fact in extract_working_facts(
            "Remember: tag is ORION-115 and owner validation is pending for this conversation."
        )
    )
    memory.upsert_working_facts("scoped", asset_a, facts)

    a_context = memory.compose_memory_context(
        "scoped", settings, context_key=asset_a, active_entities=asset_a.entities
    )
    b_context = memory.compose_memory_context(
        "scoped", settings, context_key=asset_b, active_entities=asset_b.entities
    )

    assert {item.key for item in a_context.working_facts} == {
        "investigation_tag", "owner_validation"
    }
    assert b_context.working_facts == ()


def test_asset_switch_archives_a_and_broad_historical_recall_reuses_a_context() -> None:
    settings = replace(
        get_settings(),
        conversation_summary_enabled=True,
        conversation_summary_trigger_tokens=1,
        memory_episode_context_limit=2,
    )
    memory = MemoryStore(20)
    asset_a = MemoryContextKey(("192.168.30.115",), "asset_investigation", "none", "asset")
    asset_b = MemoryContextKey(("192.168.30.116",), "asset_investigation", "none", "asset")
    memory.prepare_for_model("episodes", settings, SessionRoutingState(), context_key=asset_a)
    memory.record_turn(
        "episodes",
        "Analyze A.",
        "Profile conclusion for A and Detection conclusion for A.",
        asset_a,
        request_id="a-1",
    )
    memory.upsert_working_facts(
        "episodes",
        asset_a,
        tuple(
            replace(fact, scope="entity", entity_ids=asset_a.entities)
            for fact in extract_working_facts("Remember tag is ORION-115 for this conversation.")
        ),
    )
    switched = memory.prepare_for_model(
        "episodes", settings, SessionRoutingState(active_entities=asset_b.entities), context_key=asset_b
    )
    assert switched.episode_transition

    recalled = memory.prepare_for_model(
        "episodes", settings, SessionRoutingState(active_entities=asset_b.entities), context_key=asset_a
    )
    assert recalled.memory_context is not None
    rendered = "\n".join(item["content"] for item in recalled.messages)
    assert "ORION-115" in rendered
    assert "Profile conclusion for A" in rendered
    assert all(
        not set(item.entity_ids).intersection(asset_b.entities)
        for item in recalled.memory_context.working_facts
    )


@pytest.mark.parametrize(
    ("message", "classification"),
    (
        ("What have we concluded so far about this asset?", "historical_summary"),
        ("What do you remember from this investigation?", "explicit_memory"),
        ("What did we establish about the owner validation?", "historical_summary"),
        ("Give me the previous findings from this investigation.", "historical_summary"),
        ("What do we know so far about the identity contradiction?", "historical_summary"),
    ),
)
def test_natural_historical_recall_paraphrases_are_memory_only(
    message: str,
    classification: str,
) -> None:
    constraints = derive_request_constraints(message)

    assert classify_historical_recall(message) == classification
    assert constraints.memory_only
    assert not constraints.allow_live
    assert not constraints.require_current
    assert compile_direct_plan(task_spec_from_route(_asset_route(), message, constraints)).steps == ()


@pytest.mark.parametrize(
    "message",
    (
        "What have we concluded so far, and what is the current state?",
        "What do you remember, and verify it again now?",
        "Recheck the latest identity contradiction from our previous findings.",
        "Is this asset still showing the same identity contradiction?",
    ),
)
def test_explicit_current_semantics_override_historical_recall(message: str) -> None:
    constraints = derive_request_constraints(message)

    assert constraints.require_current
    assert constraints.allow_live
    assert not constraints.memory_only


@pytest.mark.parametrize(
    "message",
    (
        "What is this asset?",
        "What do we know about asset 192.168.30.115?",
        "Analyze this asset's identity.",
    ),
)
def test_ordinary_asset_questions_do_not_become_memory_only(message: str) -> None:
    constraints = derive_request_constraints(message)

    assert classify_historical_recall(message) == "none"
    assert constraints.allow_live
    assert not constraints.memory_only


def test_current_identity_contradiction_uses_minimum_product_evidence() -> None:
    message = "Is this asset still showing the same identity contradiction?"
    task = task_spec_from_route(_asset_route(), message)
    plan = compile_direct_plan(task)

    assert task.required_capabilities == ("asset.get_profile", "asset.get_detection")
    assert task.optional_capabilities == ()
    assert task.workflow_mode == "direct"
    assert [step.capability for step in plan.steps] == [
        "asset.get_profile",
        "asset.get_detection",
    ]


def test_identity_contradiction_preserves_explicit_topology_and_knowledge_needs() -> None:
    message = (
        "Verify the current identity contradiction using topology and explain why with runbook background."
    )
    task = task_spec_from_route(_asset_route(), message)

    assert task.required_capabilities == (
        "asset.get_profile",
        "asset.get_detection",
        "graph.get_summary",
    )
    assert task.optional_capabilities == ("knowledge.search",)
