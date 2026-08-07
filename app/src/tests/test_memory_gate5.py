"""Focused offline tests for Gate 5 durable memory and shared UI contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from src.core.identity import RequestIdentity
from src.core.memory.episodes import EpisodeRecord, MemoryContextKey, WorkingMemory
from src.core.memory.persistence import LocalChatMessage, ThreadMemoryState
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
    assert loaded.schema_version == 2
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
