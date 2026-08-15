"""Focused offline regressions for deterministic memory-only continuity."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from src.config.settings import get_settings
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.agent.task_mapping import compile_direct_plan, task_spec_from_route
from src.core.context.entities import EntityResolver
from src.core.context.models import EntityResolution, IntentDecision, ResolvedEntity
from src.core.memory.episodes import MemoryContextKey
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.store import MemoryStore


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
