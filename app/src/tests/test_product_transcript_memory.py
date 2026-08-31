"""Offline Product transcript and bounded-compaction parity regressions."""

from types import SimpleNamespace

import pytest

from src.core.identity import RequestIdentity
from src.core.memory.episodes import MemoryContextKey
from src.core.memory.persistence import LocalPersistenceOwnershipError, ThreadMemoryState
from src.core.memory.product_chat import ProductTranscriptRepository
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.store import MemoryStore
from src.core.context.models import approx_tokens


class FakeProductChatClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.settings = SimpleNamespace(product_chat_rooms_path="/chat-rooms")

    def get_json(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self.payload, 200, 0.01


def summary_settings(**overrides):
    values = {
        "conversation_summary_enabled": True,
        "conversation_summary_trigger_tokens": 500,
        "conversation_summary_max_tokens": 300,
        "conversation_recent_raw_messages": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_product_transcript_is_owner_scoped_bounded_and_request_correlated():
    fake = FakeProductChatClient({
        "data": {"roomId": "room-1", "userId": "user-a", "messages": [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "current"},
        ]}
    })
    messages = ProductTranscriptRepository(fake).recent(
        user_id="user-a", conversation_id="room-1", limit=2, request_id="req-current"
    )
    assert [(item.role, item.content) for item in messages] == [
        ("assistant", "answer"), ("user", "current")
    ]
    assert fake.calls == [(
        "/chat-rooms/room-1",
        {"request_id": "req-current", "operation": "chat_transcript", "extra_headers": {"X-User-ID": "user-a"}},
    )]


def test_product_transcript_rejects_owner_mismatch():
    fake = FakeProductChatClient({
        "roomId": "room-1", "userId": "user-b", "messages": []
    })
    with pytest.raises(LocalPersistenceOwnershipError):
        ProductTranscriptRepository(fake).recent(user_id="user-a", conversation_id="room-1")


def test_uncorrelated_product_pairs_restore_references_and_ignore_current_dangling_user():
    session = "session-a"
    key = MemoryContextKey(("192.0.2.10",), "asset_investigation", "none", "asset")
    first = MemoryStore(4, relevant_turn_limit=4)
    first.record_turn(session, "T10 question", "T10 conclusion", key, request_id="r10")
    first.record_turn(session, "T11 question", "T11 conclusion", key, request_id="r11")
    identity = RequestIdentity.resolve(
        user_id="user-a", conversation_id="room-1", session_id=session, request_id="r12"
    )
    state = ThreadMemoryState.from_routing_state(
        identity, SessionRoutingState(active_entities=key.entities),
        **first.durable_components(session, turn_limit=4, episode_limit=4),
    )
    fake = FakeProductChatClient({"roomId": "room-1", "userId": "user-a", "messages": [
        {"role": "user", "content": "T10 question"},
        {"role": "assistant", "content": "T10 conclusion"},
        {"role": "user", "content": "T11 question"},
        {"role": "assistant", "content": "T11 conclusion"},
        {"role": "user", "content": "T12 current request"},
    ]})
    transcript = ProductTranscriptRepository(fake).recent(
        user_id="user-a", conversation_id="room-1", limit=9, request_id="r12"
    )
    restored = MemoryStore(4, relevant_turn_limit=4)
    restored.restore_durable_state(state, transcript)
    assert [item["content"] for item in restored.get(session)] == [
        "T10 question", "T10 conclusion", "T11 question", "T11 conclusion"
    ]
    package = restored.compose_memory_context(
        session, SimpleNamespace(memory_relevant_turn_limit=4, memory_relevant_turn_token_budget=900,
        memory_episode_context_limit=2, memory_episode_context_token_budget=300,
        memory_context_token_budget=1400, memory_context_long_term_token_budget=500),
        context_key=key, active_entities=key.entities,
    )
    assert [item.request_id for item in package.relevant_turns] == ["r10", "r11"]
    assert restored.repository.list_episodes(session) == ()
    working = restored.repository.get_working(session)
    assert working is not None
    assert working.latest_user_turn == "T11 question"
    assert working.latest_assistant_turn == "T11 conclusion"


def test_retention_pressure_summarizes_before_discard_and_preserves_complete_pair():
    session = "session-retention"
    key = MemoryContextKey(("192.0.2.20",), "asset_investigation", "none", "asset")
    memory = MemoryStore(4, relevant_turn_limit=4)
    for number in range(3):
        memory.record_turn(session, f"short question {number}", f"short conclusion {number}", key, request_id=f"r{number}")
    assert memory.compact_if_needed(
        session, summary_settings(), SessionRoutingState(active_entities=key.entities), request_id="r2"
    )
    assert memory.get(session) == [
        {"role": "user", "content": "short question 2"},
        {"role": "assistant", "content": "short conclusion 2"},
    ]
    working = memory.repository.get_working(session)
    assert working is not None and "short question 0" in working.compact_summary
    assert approx_tokens(working.compact_summary) <= 300
    assert not memory.compact_if_needed(
        session, summary_settings(), SessionRoutingState(active_entities=key.entities), request_id="r2"
    )
