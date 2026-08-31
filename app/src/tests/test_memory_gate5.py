"""Focused offline tests for Gate 5 durable memory and shared UI contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
from types import SimpleNamespace

from src.core.identity import RequestIdentity
from src.core.context.compaction import fingerprint
from src.core.memory.episodes import (
    BaselineProjection,
    EpisodeRecord,
    InvestigationBaseline,
    MemoryContextKey,
    WorkingFact,
    WorkingMemory,
)
from src.core.memory.persistence import (
    MAX_THREAD_STATE_BYTES,
    THREAD_STATE_SCHEMA_VERSION,
    LocalChatMessage,
    ThreadMemoryState,
)
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.sqlite import LocalSQLiteDatabase, SQLiteThreadStateStore
from src.core.memory.store import MemoryStore
from src.web.chat_backend import (
    ConversationController,
    LegacyDirectBackend,
    LocalSimulationBackend,
)
from src.web.local_simulation import LocalConversation


def settings(**overrides):
    values = {
        "conversation_summary_enabled": True,
        "conversation_summary_trigger_tokens": 20,
        "conversation_summary_max_tokens": 100,
        "conversation_recent_raw_messages": 2,
        "memory_relevant_turn_limit": 2,
        "memory_relevant_turn_token_budget": 200,
        "memory_episode_context_limit": 1,
        "memory_episode_context_token_budget": 100,
        "memory_context_token_budget": 300,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def context(ip: str, topic: str = "asset_investigation") -> MemoryContextKey:
    return MemoryContextKey((ip,), topic, "none", "topology")


def test_working_fact_retention_limit_preserves_newest_facts() -> None:
    memory = MemoryStore(10, max_working_facts=2)
    facts = tuple(
        WorkingFact(key=f"key-{index}", value=f"value-{index}")
        for index in range(3)
    )
    memory.upsert_working_facts("session-a", context("192.0.2.10"), facts)
    working = memory.repository.get_working("session-a")
    assert working is not None
    assert [fact.key for fact in working.working_facts] == ["key-1", "key-2"]


def identity(user: str = "user-a", conversation: str = "conversation-a") -> RequestIdentity:
    return RequestIdentity.resolve(
        user_id=user,
        conversation_id=conversation,
        session_id=f"session-{conversation}",
        request_id="request-current",
    )


def message(request_id: str, role: str, content: str, position: int) -> LocalChatMessage:
    return LocalChatMessage(
        message_id=f"{request_id}-{role}",
        conversation_id="conversation-a",
        request_id=request_id,
        role=role,
        content=content,
        status="completed",
        position=position,
        created_at="2026-08-07T00:00:00+00:00",
    )


def test_relevant_turns_are_deterministic_entity_preferred_and_bounded() -> None:
    memory = MemoryStore(20)
    memory.record_turn("s", "old unrelated", "old answer", context("192.0.2.1"), request_id="r1")
    memory.record_turn("s", "active details", "active answer", context("192.0.2.2"), request_id="r2")
    memory.record_turn("s", "new unrelated", "new answer", context("192.0.2.3"), request_id="r3")

    first = memory.compose_memory_context(
        "s", settings(memory_relevant_turn_limit=1),
        context_key=context("192.0.2.2"), active_entities=("192.0.2.2",), request_id="q",
    )
    second = memory.compose_memory_context(
        "s", settings(memory_relevant_turn_limit=1),
        context_key=context("192.0.2.2"), active_entities=("192.0.2.2",), request_id="q",
    )
    assert first == second
    assert [item.request_id for item in first.relevant_turns] == ["r2"]
    assert first.estimated_tokens <= 300


def test_general_topic_detaches_asset_turns() -> None:
    memory = MemoryStore(10)
    memory.record_turn("s", "asset question", "asset answer", context("192.0.2.2"), request_id="r1")
    package = memory.compose_memory_context(
        "s", settings(),
        context_key=MemoryContextKey(topic_family="general"),
        active_entities=("192.0.2.2",),
    )
    assert package.relevant_turns == ()


def test_working_summary_turns_and_episode_survive_store_recreation(tmp_path: Path) -> None:
    request_identity = identity()
    memory = MemoryStore(20)
    key = context("192.0.2.2")
    memory.record_turn("session-conversation-a", "question " * 20, "answer " * 20, key, request_id="r1")
    memory.repository.add_episode(
        EpisodeRecord(
            episode_id="episode-closed",
            session_id="session-conversation-a",
            context_key=key,
            compact_summary="bounded closed investigation",
            limitations=("partial graph view",),
            turn_count=1,
        )
    )
    memory.compact_if_needed(
        "session-conversation-a", settings(), SessionRoutingState(active_entities=("192.0.2.2",)), request_id="r1"
    )
    components = memory.durable_components("session-conversation-a", turn_limit=4, episode_limit=4)
    state = ThreadMemoryState.from_routing_state(
        request_identity,
        SessionRoutingState(active_entities=("192.0.2.2",)),
        **components,
    )
    database = LocalSQLiteDatabase(tmp_path / "gate5.sqlite3")
    database.initialize()
    saved = SQLiteThreadStateStore(database).save(
        identity=request_identity, state=state, expected_revision=0
    )
    loaded = SQLiteThreadStateStore(database).load(identity=request_identity)
    assert loaded is not None
    restored = MemoryStore(20)
    restored.restore_durable_state(
        loaded,
        (
            message("r1", "user", "question " * 20, 1),
            message("r1", "assistant", "answer " * 20, 2),
        ),
    )
    snapshot = restored.prepare_for_model(
        loaded.session_id,
        settings(conversation_summary_trigger_tokens=10_000),
        loaded.to_routing_state(),
        context_key=key,
    )
    assert snapshot.summary_present
    assert snapshot.recent_message_count == 2
    assert restored.repository.list_episodes(loaded.session_id)[0].episode_id == "episode-closed"
    assert saved.revision == 1


def test_sqlite_v2_thread_state_migrates_idempotently(tmp_path: Path) -> None:
    request_identity = identity()
    database = LocalSQLiteDatabase(tmp_path / "migration.sqlite3")
    database.initialize()
    store = SQLiteThreadStateStore(database)
    store.save(
        identity=request_identity,
        state=ThreadMemoryState.from_routing_state(
            request_identity,
            SessionRoutingState(active_entities=("192.0.2.2",)),
        ),
        expected_revision=0,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE schema_metadata SET value = '2' WHERE key = 'local_schema_version'"
        )
        connection.execute(
            "UPDATE local_thread_states SET schema_version = 1, "
            "state_json = json_remove(state_json, '$.recent_turn_references', "
            "'$.recent_episodes', '$.summary_updated_at', "
            "'$.summary_source_request_id', '$.summary_size_tokens')"
        )
    database.initialize()
    database.initialize()
    loaded = store.load(identity=request_identity)
    assert loaded is not None
    assert loaded.schema_version == THREAD_STATE_SCHEMA_VERSION
    assert loaded.active_entities == ("192.0.2.2",)
    assert loaded.recent_turn_references == ()


def test_thread_memory_state_isolated_by_owner_and_has_no_raw_evidence() -> None:
    state = ThreadMemoryState.from_routing_state(
        identity(),
        SessionRoutingState(active_entities=("192.0.2.2",)),
        working_memory=WorkingMemory(
            session_id="session-conversation-a",
            context_key=context("192.0.2.2"),
            episode_id="episode-1",
            compact_summary="bounded summary",
        ),
    )
    payload = state.to_payload()
    rendered = str(payload)
    assert "ToolResult" not in rendered
    assert "EvidencePack" not in rendered
    assert "provider_payload" not in rendered
    assert state.user_id == "user-a"
    assert replace(state, user_id="user-b") != state


def test_legacy_backend_uses_shared_controller_and_stable_session() -> None:
    state = {"session_id": "stable-session", "messages": []}
    backend = LegacyDirectBackend(
        state,
        lambda _message: iter(({"type": "answer_delta", "text": "ok"}, {"type": "done"})),
    )
    controller = ConversationController(backend)
    conversation = backend.list_conversations()[0]
    assert conversation.session_id == "stable-session"
    assert list(
        controller.stream_turn(
            conversation, request_id="r1", message="hello", selected_ip=None
        )
    )[-1]["type"] == "done"
    controller.complete_turn(
        conversation,
        request_id="r1",
        user_content="hello",
        assistant_content="ok",
    )
    assert [item["role"] for item in controller.messages(conversation)] == ["user", "assistant"]


def test_local_backend_uses_shared_controller_and_user_switch_clears_state() -> None:
    conversation = LocalConversation("c1", "u2", "s1", "test", "", "")

    class FakeClient:
        def get_user(self, user_id):
            return {"user_id": user_id}

        def get_messages(self, user_id, conversation_id):
            assert (user_id, conversation_id) == ("u2", "c1")
            return [{"role": "assistant", "content": "restored"}]

        def stream_chat(self, **kwargs):
            assert kwargs["user_id"] == "u2"
            return iter(({"type": "done"},))

    state = {"local_simulation_user_id": "u1", "selected_copilot_ip": "192.0.2.1"}
    backend = LocalSimulationBackend(FakeClient(), state)  # type: ignore[arg-type]
    backend.login("u2")
    controller = ConversationController(backend)
    assert state == {"local_simulation_user_id": "u2"}
    assert controller.messages(conversation)[0]["content"] == "restored"
    assert list(
        controller.stream_turn(
            conversation, request_id="r1", message="hello", selected_ip=None
        )
    ) == [{"type": "done"}]
    backend.logout()
    assert state == {}
def investigation_baseline(
    *,
    owner: str = "user-a",
    request_id: str = "baseline-request",
    completeness: str = "complete",
    payload: dict | None = None,
) -> InvestigationBaseline:
    evidence = payload or {"role": "server", "risk": 4}
    projection = BaselineProjection(
        capability="asset.get_profile",
        entity_ids=("192.0.2.2",),
        view="overview",
        schema_version="product-view-v1",
        evidence_classes=("asset_identity", "asset_role"),
        payload=evidence,
        valid_at="2026-08-20T00:00:00+00:00",
        completeness=completeness,  # type: ignore[arg-type]
        fingerprint=fingerprint(evidence),
    )
    return InvestigationBaseline(
        entity_ids=("192.0.2.2",),
        captured_at=projection.valid_at,
        source_request_id=request_id,
        scope="node_summary",
        projections=(projection,),
        owner_id=owner,
    )


def test_active_and_archived_episode_baselines_roundtrip_in_thread_state() -> None:
    request_identity = identity()
    key = context("192.0.2.2")
    active = investigation_baseline(request_id="active-baseline")
    archived = investigation_baseline(request_id="archived-baseline")
    state = ThreadMemoryState.from_routing_state(
        request_identity,
        SessionRoutingState(active_entities=("192.0.2.2",)),
        working_memory=WorkingMemory(
            session_id=request_identity.session_id,
            context_key=key,
            episode_id="active-episode",
            baseline=active,
        ),
        recent_episodes=(
            EpisodeRecord(
                episode_id="archived-episode",
                session_id=request_identity.session_id,
                context_key=key,
                baseline=archived,
            ),
        ),
    )

    restored = ThreadMemoryState.from_payload(
        state.to_payload(),
        thread_key=request_identity.thread_key,
        user_id=request_identity.user_id,
        conversation_id=request_identity.conversation_id,
        session_id=request_identity.session_id,
        updated_at=state.updated_at,
        revision=1,
        schema_version=THREAD_STATE_SCHEMA_VERSION,
    )

    assert restored.working_memory is not None
    assert restored.working_memory.baseline == active
    assert restored.recent_episodes[0].baseline == archived


def test_schema_v3_thread_state_without_baseline_remains_loadable() -> None:
    request_identity = identity()
    state = ThreadMemoryState.from_routing_state(
        request_identity,
        SessionRoutingState(active_entities=("192.0.2.2",)),
    )

    restored = ThreadMemoryState.from_payload(
        state.to_payload(),
        thread_key=request_identity.thread_key,
        user_id=request_identity.user_id,
        conversation_id=request_identity.conversation_id,
        session_id=request_identity.session_id,
        updated_at=state.updated_at,
        revision=1,
        schema_version=3,
    )

    assert restored.active_entities == ("192.0.2.2",)
    assert restored.working_memory is None


def test_wrong_owner_baseline_is_dropped_without_losing_thread_continuity() -> None:
    request_identity = identity()
    key = context("192.0.2.2")
    state = ThreadMemoryState.from_routing_state(
        request_identity,
        SessionRoutingState(active_entities=("192.0.2.2",)),
        working_memory=WorkingMemory(
            session_id=request_identity.session_id,
            context_key=key,
            episode_id="owner-bound",
            baseline=investigation_baseline(owner="user-a"),
        ),
    )

    restored = ThreadMemoryState.from_payload(
        state.to_payload(),
        thread_key=request_identity.thread_key,
        user_id="user-b",
        conversation_id=request_identity.conversation_id,
        session_id=request_identity.session_id,
        updated_at=state.updated_at,
        revision=1,
        schema_version=THREAD_STATE_SCHEMA_VERSION,
    )

    assert restored.working_memory is not None
    assert restored.working_memory.episode_id == "owner-bound"
    assert restored.working_memory.baseline is None


def test_oversized_baseline_is_shed_before_thread_continuity() -> None:
    request_identity = identity()
    key = context("192.0.2.2")
    oversized = investigation_baseline(payload={"value": "x" * 20_000})
    state = ThreadMemoryState.from_routing_state(
        request_identity,
        SessionRoutingState(active_entities=("192.0.2.2",)),
        working_memory=WorkingMemory(
            session_id=request_identity.session_id,
            context_key=key,
            episode_id="bounded-episode",
            compact_summary="continuity survives",
            baseline=oversized,
        ),
    )

    payload = state.to_payload()

    assert payload["working_memory"]["compact_summary"] == "continuity survives"
    assert "baseline" not in payload["working_memory"]
    assert len(json.dumps(payload).encode("utf-8")) <= MAX_THREAD_STATE_BYTES


def test_strong_baseline_survives_partial_turn_and_episode_switch() -> None:
    memory = MemoryStore(20)
    key = context("192.0.2.2")
    baseline = investigation_baseline()
    memory.record_turn("baseline-session", "investigate", "answer", key, request_id="r1")
    assert memory.set_investigation_baseline(
        "baseline-session", key, baseline, request_id="r1"
    )
    detection_payload = {"classification": "server", "confidence": 0.9}
    detection_projection = BaselineProjection(
        capability="asset.get_detection",
        entity_ids=("192.0.2.2",),
        view="overview",
        schema_version="product-view-v1",
        evidence_classes=("detection_classification",),
        payload=detection_payload,
        valid_at="2026-08-21T00:00:00+00:00",
        completeness="complete",
        fingerprint=fingerprint(detection_payload),
    )
    narrower_complete = InvestigationBaseline(
        entity_ids=("192.0.2.2",),
        captured_at=detection_projection.valid_at,
        source_request_id="narrower-complete",
        scope="node_summary",
        projections=(detection_projection,),
        owner_id="user-a",
    )
    assert memory.set_investigation_baseline(
        "baseline-session", key, narrower_complete, request_id="r2"
    )
    strong = memory.repository.get_working("baseline-session").baseline  # type: ignore[union-attr]
    assert strong is not None
    assert {item.capability for item in strong.projections} == {
        "asset.get_profile", "asset.get_detection"
    }

    partial = investigation_baseline(
        request_id="partial",
        completeness="partial",
        payload={"role": "unknown"},
    )
    partial = replace(partial, captured_at="2026-08-22T00:00:00+00:00")
    assert not memory.set_investigation_baseline(
        "baseline-session", key, partial, request_id="r3"
    )
    assert memory.repository.get_working("baseline-session").baseline == strong  # type: ignore[union-attr]

    other = context("192.0.2.3")
    memory.prepare_for_model(
        "baseline-session",
        settings(conversation_summary_trigger_tokens=10_000),
        SessionRoutingState(active_entities=("192.0.2.3",)),
        context_key=other,
        activate_context=True,
        request_id="switch-away",
    )
    archived = memory.repository.list_episodes("baseline-session")
    assert archived[-1].baseline == strong

    memory.prepare_for_model(
        "baseline-session",
        settings(conversation_summary_trigger_tokens=10_000),
        SessionRoutingState(active_entities=("192.0.2.2",)),
        context_key=key,
        activate_context=True,
        request_id="switch-back",
    )
    restored = memory.investigation_baselines(
        "baseline-session", key, owner_id="user-a"
    )
    assert restored
    assert restored[0] == strong


def test_baseline_restores_after_simulated_thread_state_restart() -> None:
    request_identity = identity()
    key = context("192.0.2.2")
    memory = MemoryStore(20)
    memory.record_turn(
        request_identity.session_id,
        "investigate",
        "answer",
        key,
        request_id="restart-turn",
    )
    baseline = investigation_baseline()
    assert memory.set_investigation_baseline(
        request_identity.session_id, key, baseline
    )
    state = ThreadMemoryState.from_routing_state(
        request_identity,
        SessionRoutingState(active_entities=("192.0.2.2",)),
        **memory.durable_components(
            request_identity.session_id,
            turn_limit=4,
            episode_limit=4,
        ),
    )
    restored_state = ThreadMemoryState.from_payload(
        state.to_payload(),
        thread_key=request_identity.thread_key,
        user_id=request_identity.user_id,
        conversation_id=request_identity.conversation_id,
        session_id=request_identity.session_id,
        updated_at=state.updated_at,
        revision=1,
        schema_version=THREAD_STATE_SCHEMA_VERSION,
    )
    restarted = MemoryStore(20)
    restarted.restore_durable_state(restored_state)

    selected = restarted.investigation_baselines(
        request_identity.session_id,
        key,
        owner_id="user-a",
    )
    assert selected == (baseline,)
