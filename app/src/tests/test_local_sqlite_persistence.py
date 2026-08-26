"""Focused offline tests for disabled-by-default local SQLite persistence."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from threading import RLock

import pytest

from src.config.settings import get_settings
from src.core.agent.workflow import BoundedCopilotWorkflow
from src.core.copilot.service import CopilotService
from src.core.identity import RequestIdentity
from src.core.memory.factory import build_local_persistence
from src.core.product_client.memory_client import ProductMemoryClient
from src.core.memory.persistence import (
    LOCAL_SCHEMA_VERSION,
    MAX_THREAD_STATE_BYTES,
    CompactThreadState,
    LocalPersistenceConflictError,
    LocalPersistenceError,
    LocalPersistenceOwnershipError,
    LocalPersistenceQuotaError,
    LocalPersistenceSchemaError,
    MemoryStoragePolicy,
)
from src.core.memory.routing_state import (
    SessionRoutingState,
    SessionRoutingStateStore,
)
from src.core.memory.sqlite import (
    LocalSQLiteDatabase,
    SQLiteChatRepository,
    SQLiteThreadStateStore,
)
from src.core.memory.store import MemoryStore
from src.tests.test_context_routing import FakeLLMClient, fake_result, make_settings


GENERAL_ROUTE = json.dumps(
    {
        "intent": "general_knowledge",
        "scope": "none",
        "direction": "none",
        "depth": 0,
        "requires_graph": False,
        "requires_detection": False,
        "requires_asset_profile": False,
        "requires_knowledge": False,
        "entity_binding": "none",
        "requires_multiple_entities": False,
        "is_followup": False,
        "classification_confidence": 0.95,
        "reason": "general",
    }
)


def enabled_settings(path: Path, **overrides):
    values = {
        "local_product_simulation_enabled": True,
        "thread_state_backend": "sqlite",
        "local_sqlite_path": str(path),
        "langgraph_checkpoint_backend": "none",
    }
    values.update(overrides)
    return replace(get_settings(), **values)


def database(tmp_path: Path) -> LocalSQLiteDatabase:
    result = LocalSQLiteDatabase(tmp_path / "runtime" / "copilot-local.sqlite3")
    result.initialize()
    return result


def identity(
    *,
    user: str | None = "user-a",
    conversation: str | None = "conversation-a",
    session: str = "session-a",
    request: str = "request-a",
) -> RequestIdentity:
    return RequestIdentity.resolve(
        user_id=user,
        conversation_id=conversation,
        session_id=session,
        request_id=request,
    )


def routing_state(*entities: str) -> SessionRoutingState:
    return SessionRoutingState(
        active_entities=entities,
        last_resolved_entities=entities,
        previous_entity_count=len(entities),
        previous_entity_mode="pair" if len(entities) == 2 else "single",
        last_provider="graph",
        last_providers=("graph",),
        previous_intent="asset_investigation",
        previous_scope="node_summary",
        previous_direction="both",
        previous_depth=0,
        previous_requires_detection=False,
        previous_requires_asset_profile=True,
    )


def continuity_service(store, chat_repository=None) -> CopilotService:
    service = object.__new__(CopilotService)
    service.memory_store = MemoryStore(10)
    service.routing_state_store = SessionRoutingStateStore()
    service.thread_state_store = store
    service.chat_repository = chat_repository
    service._persistence_lock = RLock()
    service._session_identity_bindings = {}
    service._thread_revisions = {}
    return service


def test_default_configuration_remains_memory_and_none(monkeypatch, tmp_path: Path) -> None:
    import src.config.settings as settings_module
    with pytest.MonkeyPatch.context() as isolated:
        isolated.setattr(settings_module, "ENV_PATH", tmp_path / "missing.env")
        isolated.setattr(settings_module, "LEGACY_ENV_PATH", tmp_path / "missing-legacy.env")
        isolated.setattr(os, "environ", {})
        settings_module.get_settings.cache_clear()
        settings = settings_module.get_settings()
        assert settings.local_product_simulation_enabled is False
        assert settings.thread_state_backend == "memory"
        assert settings.langgraph_checkpoint_backend == "none"
        settings_module.get_settings.cache_clear()


def test_disabled_configuration_creates_no_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "disabled" / "copilot.sqlite3"
    settings = replace(
        get_settings(),
        local_product_simulation_enabled=False,
        thread_state_backend="memory",
        long_term_memory_enabled=False,
        local_sqlite_path=str(path),
        langgraph_checkpoint_backend="none",
    )

    adapters = build_local_persistence(settings)

    assert adapters.chat_repository is None
    assert adapters.thread_state_store is None
    assert not path.exists()
    assert not path.parent.exists()


def test_product_thread_without_sqlite_subsystem_creates_no_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "product-only" / "copilot.sqlite3"
    settings = replace(
        get_settings(), local_product_simulation_enabled=False, thread_state_backend="product",
        long_term_memory_enabled=False, local_sqlite_path=str(path), langgraph_checkpoint_backend="none",
    )
    adapters = build_local_persistence(
        settings,
        product_memory_client=ProductMemoryClient(object()),
        product_client=object(),
    )
    assert adapters.thread_state_store is not None
    assert adapters.transcript_repository is not None
    assert adapters.chat_repository is None
    assert not path.exists() and not path.parent.exists()


def test_product_thread_with_sqlite_ltm_initializes_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "staged" / "copilot.sqlite3"
    settings = replace(
        get_settings(), local_product_simulation_enabled=False, thread_state_backend="product",
        long_term_memory_enabled=True, long_term_memory_backend="sqlite", local_sqlite_path=str(path),
    )
    adapters = build_local_persistence(settings, product_memory_client=ProductMemoryClient(object()))
    assert adapters.thread_state_store is not None and adapters.long_term_memory_store is not None
    assert path.exists()


def test_product_thread_with_product_ltm_creates_no_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "product-ltm-only" / "copilot.sqlite3"
    settings = replace(
        get_settings(), local_product_simulation_enabled=False, thread_state_backend="product",
        long_term_memory_enabled=True, long_term_memory_backend="product", local_sqlite_path=str(path),
    )

    adapters = build_local_persistence(settings, product_memory_client=ProductMemoryClient(object()))

    assert adapters.thread_state_store is not None and adapters.long_term_memory_store is not None
    assert not path.exists() and not path.parent.exists()


def test_checkpoint_sqlite_setting_remains_deferred_and_creates_no_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "checkpoint-only.sqlite3"
    settings = replace(
        get_settings(),
        local_product_simulation_enabled=False,
        thread_state_backend="memory",
        local_sqlite_path=str(path),
        langgraph_checkpoint_backend="sqlite",
    )
    workflow = BoundedCopilotWorkflow(settings)

    assert getattr(workflow.graph, "checkpointer", None) is None
    assert not path.exists()
    workflow.close()


def test_enabled_sqlite_requires_a_path() -> None:
    settings = replace(
        get_settings(),
        local_product_simulation_enabled=True,
        thread_state_backend="sqlite",
        local_sqlite_path="",
    )
    with pytest.raises(ValueError, match="SOORIN_LOCAL_SQLITE_PATH"):
        settings.validate_local_persistence_configuration()


def test_fresh_and_repeated_schema_initialization_is_safe(tmp_path: Path) -> None:
    db = database(tmp_path)
    db.initialize()

    with db.connect() as connection:
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = ?",
            ("local_schema_version",),
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == str(LOCAL_SCHEMA_VERSION)
    assert {
        "schema_metadata",
        "local_users",
        "local_conversations",
        "local_messages",
        "local_request_commits",
        "local_thread_states",
    } <= tables
    assert db.foreign_keys_enabled() is True


def test_invalid_path_fails_safely(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocked", encoding="utf-8")
    db = LocalSQLiteDatabase(blocker / "copilot.sqlite3")

    with pytest.raises(LocalPersistenceError):
        db.initialize()


def test_schema_stores_only_versioned_password_hashes(tmp_path: Path) -> None:
    db = database(tmp_path)
    repository = SQLiteChatRepository(db)
    password = "correct horse battery staple"
    from src.core.memory.local_auth import hash_password, verify_password

    user = repository.create_user(
        username="analyst",
        password_hash=hash_password(password),
    )
    repository.create_conversation(
        user_id=user.user_id,
        conversation_id="conversation-a",
        title="Local investigation",
    )

    content = db.path.read_bytes()
    assert b"api_key" not in content.lower()
    assert b"authorization" not in content.lower()
    assert password.encode("utf-8") not in content
    encoded = repository.get_password_hash(user_id=user.user_id)
    assert encoded and encoded.startswith("scrypt$v1$")
    assert verify_password(password, encoded)


def test_chat_repository_create_list_get_append_and_bounded_order(
    tmp_path: Path,
) -> None:
    repository = SQLiteChatRepository(database(tmp_path))
    created = repository.create_conversation(
        user_id="user-a",
        conversation_id="conversation-a",
        title="Investigation",
    )
    assert created.user_id == "user-a"
    assert repository.get_conversation(
        user_id="user-a",
        conversation_id="conversation-a",
    ) == created
    assert repository.list_conversations(user_id="user-a") == (created,)

    repository.append(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-1",
        role="user",
        content="first",
    )
    repository.append(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-1",
        role="assistant",
        content="second",
    )
    repository.append(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-2",
        role="user",
        content="third",
    )

    messages = repository.recent(
        user_id="user-a",
        conversation_id="conversation-a",
        limit=2,
    )
    assert [item.content for item in messages] == ["second", "third"]
    assert [item.position for item in messages] == [2, 3]


def test_chat_storage_policy_bounds_messages_and_evicts_oldest_unprotected_conversation(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    repository = SQLiteChatRepository(
        db,
        MemoryStoragePolicy(
            max_conversations_per_user=2,
            max_messages_per_conversation=2,
        ),
    )
    for conversation in ("conversation-a", "conversation-b"):
        repository.create_conversation(user_id="user-a", conversation_id=conversation)
    for index in range(3):
        repository.append(
            user_id="user-a",
            conversation_id="conversation-b",
            request_id=f"request-{index}",
            role="user",
            content=f"message-{index}",
        )
    assert [item.content for item in repository.recent(
        user_id="user-a", conversation_id="conversation-b", limit=10
    )] == ["message-1", "message-2"]

    repository.create_conversation(user_id="user-a", conversation_id="conversation-c")
    assert repository.get_conversation(
        user_id="user-a", conversation_id="conversation-a"
    ) is None
    assert {item.conversation_id for item in repository.list_conversations(user_id="user-a")} == {
        "conversation-b", "conversation-c"
    }


def test_conversation_quota_preserves_records_with_durable_thread_state(tmp_path: Path) -> None:
    db = database(tmp_path)
    repository = SQLiteChatRepository(
        db,
        MemoryStoragePolicy(max_conversations_per_user=1),
    )
    repository.create_conversation(user_id="user-a", conversation_id="conversation-a")
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO local_thread_states(thread_key,user_id,conversation_id,session_id,revision,schema_version,state_json,updated_at) "
            "VALUES ('thread-a','user-a','conversation-a','session-a',1,3,'{}','2026-08-16T00:00:00+00:00')"
        )
    with pytest.raises(LocalPersistenceQuotaError):
        repository.create_conversation(user_id="user-a", conversation_id="conversation-b")


def test_chat_repository_owner_and_conversation_isolation(tmp_path: Path) -> None:
    repository = SQLiteChatRepository(database(tmp_path))
    repository.create_conversation(
        user_id="user-a",
        conversation_id="conversation-a",
    )
    repository.create_conversation(
        user_id="user-a",
        conversation_id="conversation-b",
    )
    repository.append(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-a",
        role="user",
        content="only a",
    )

    with pytest.raises(LocalPersistenceOwnershipError):
        repository.get_conversation(
            user_id="user-b",
            conversation_id="conversation-a",
        )
    assert repository.recent(
        user_id="user-a",
        conversation_id="conversation-b",
    ) == ()


def test_duplicate_request_commits_one_ordered_turn(tmp_path: Path) -> None:
    repository = SQLiteChatRepository(database(tmp_path))
    repository.begin_request(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-a",
    )
    first = repository.commit_turn(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-a",
        user_content="question",
        assistant_content="answer",
    )
    second = repository.commit_turn(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-a",
        user_content="question",
        assistant_content="answer",
    )

    assert first == second
    assert len(
        repository.recent(
            user_id="user-a",
            conversation_id="conversation-a",
        )
    ) == 2
    assert repository.request_status(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-a",
    ).status == "completed"

    with pytest.raises(LocalPersistenceConflictError):
        repository.commit_turn(
            user_id="user-a",
            conversation_id="conversation-a",
            request_id="request-a",
            user_content="different question",
            assistant_content="answer",
        )


def test_same_request_id_is_independent_across_conversations(tmp_path: Path) -> None:
    repository = SQLiteChatRepository(database(tmp_path))

    for conversation in ("conversation-a", "conversation-b"):
        repository.begin_request(
            user_id="user-a",
            conversation_id=conversation,
            request_id="shared-request",
        )
        repository.commit_turn(
            user_id="user-a",
            conversation_id=conversation,
            request_id="shared-request",
            user_content=f"question for {conversation}",
            assistant_content=f"answer for {conversation}",
        )

    assert [
        item.content
        for item in repository.recent(
            user_id="user-a",
            conversation_id="conversation-a",
        )
    ] == ["question for conversation-a", "answer for conversation-a"]
    assert [
        item.content
        for item in repository.recent(
            user_id="user-a",
            conversation_id="conversation-b",
        )
    ] == ["question for conversation-b", "answer for conversation-b"]


def test_request_status_distinguishes_interrupted_failed_and_completed(
    tmp_path: Path,
) -> None:
    repository = SQLiteChatRepository(database(tmp_path))
    repository.begin_request(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-interrupted",
    )
    interrupted = repository.mark_request_status(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-interrupted",
        status="interrupted",
    )
    repository.begin_request(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-failed",
    )
    failed = repository.mark_request_status(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-failed",
        status="failed",
    )
    repository.begin_request(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-complete",
    )
    repository.commit_turn(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-complete",
        user_content="question",
        assistant_content="answer",
    )

    assert interrupted.status == "interrupted"
    assert failed.status == "failed"
    assert repository.request_status(
        user_id="user-a",
        conversation_id="conversation-a",
        request_id="request-complete",
    ).status == "completed"


def test_delete_conversation_cascades_only_its_messages_and_commits(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    repository = SQLiteChatRepository(db)
    for conversation in ("conversation-a", "conversation-b"):
        repository.begin_request(
            user_id="user-a",
            conversation_id=conversation,
            request_id=f"request-{conversation[-1]}",
        )
        repository.commit_turn(
            user_id="user-a",
            conversation_id=conversation,
            request_id=f"request-{conversation[-1]}",
            user_content="question",
            assistant_content="answer",
        )

    assert repository.delete_conversation(
        user_id="user-a",
        conversation_id="conversation-a",
    )
    assert repository.get_conversation(
        user_id="user-a",
        conversation_id="conversation-a",
    ) is None
    assert len(
        repository.recent(
            user_id="user-a",
            conversation_id="conversation-b",
        )
    ) == 2
    with db.connect() as connection:
        deleted_messages = connection.execute(
            "SELECT COUNT(*) FROM local_messages WHERE conversation_id = ?",
            ("conversation-a",),
        ).fetchone()[0]
        deleted_commits = connection.execute(
            "SELECT COUNT(*) FROM local_request_commits WHERE conversation_id = ?",
            ("conversation-a",),
        ).fetchone()[0]
    assert deleted_messages == 0
    assert deleted_commits == 0


def test_thread_state_save_load_delete_and_restore_after_recreation(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    request_identity = identity()
    first_store = SQLiteThreadStateStore(db)
    state = CompactThreadState.from_routing_state(
        request_identity,
        routing_state("192.0.2.10", "192.0.2.11"),
    )
    saved = first_store.save(
        identity=request_identity,
        state=state,
        expected_revision=0,
    )

    restored = SQLiteThreadStateStore(
        LocalSQLiteDatabase(db.path)
    ).load(identity=request_identity)
    assert restored is not None
    assert restored.revision == 1
    assert restored.active_entities == ("192.0.2.10", "192.0.2.11")
    assert restored.to_routing_state().active_entity_count == 2
    assert first_store.delete(identity=request_identity)
    assert first_store.load(identity=request_identity) is None
    assert saved.thread_key == "conversation-a"


def test_thread_key_uses_conversation_then_legacy_session(tmp_path: Path) -> None:
    store = SQLiteThreadStateStore(database(tmp_path))
    conversation_identity = identity()
    legacy_identity = identity(
        user=None,
        conversation=None,
        session="legacy-session",
        request="legacy-request",
    )
    for request_identity in (conversation_identity, legacy_identity):
        store.save(
            identity=request_identity,
            state=CompactThreadState.from_routing_state(
                request_identity,
                routing_state("192.0.2.10"),
            ),
            expected_revision=0,
        )

    assert conversation_identity.thread_key == "conversation-a"
    assert legacy_identity.thread_key == "legacy-session"
    assert store.load(identity=legacy_identity).active_entities == ("192.0.2.10",)


def test_thread_state_owner_conversation_and_revision_isolation(
    tmp_path: Path,
) -> None:
    store = SQLiteThreadStateStore(database(tmp_path))
    owner = identity()
    state = CompactThreadState.from_routing_state(
        owner,
        routing_state("192.0.2.10"),
    )
    store.save(identity=owner, state=state, expected_revision=0)

    with pytest.raises(LocalPersistenceOwnershipError):
        store.load(
            identity=identity(
                user="user-b",
                conversation="conversation-a",
                session="session-b",
                request="request-b",
            )
        )
    assert store.load(
        identity=identity(
            user="user-a",
            conversation="conversation-b",
            session="session-b",
            request="request-b",
        )
    ) is None
    with pytest.raises(LocalPersistenceConflictError):
        store.save(identity=owner, state=state, expected_revision=0)


@pytest.mark.parametrize("corruption", ["malformed", "oversized", "future"])
def test_thread_state_rejects_corruption(tmp_path: Path, corruption: str) -> None:
    db = database(tmp_path)
    store = SQLiteThreadStateStore(db)
    request_identity = identity()
    store.save(
        identity=request_identity,
        state=CompactThreadState.from_routing_state(
            request_identity,
            routing_state("192.0.2.10"),
        ),
        expected_revision=0,
    )
    with db.connect() as connection:
        if corruption == "malformed":
            connection.execute(
                "UPDATE local_thread_states SET state_json = ? WHERE thread_key = ?",
                ("{invalid", request_identity.thread_key),
            )
        elif corruption == "oversized":
            connection.execute(
                "UPDATE local_thread_states SET state_json = ? WHERE thread_key = ?",
                ("x" * (MAX_THREAD_STATE_BYTES + 1), request_identity.thread_key),
            )
        else:
            connection.execute(
                "UPDATE local_thread_states SET schema_version = ? WHERE thread_key = ?",
                (999, request_identity.thread_key),
            )
    with pytest.raises(LocalPersistenceSchemaError):
        store.load(identity=request_identity)


def test_service_recreation_restores_active_entity_and_pair(tmp_path: Path) -> None:
    store = SQLiteThreadStateStore(database(tmp_path))
    request_identity = identity()
    first = continuity_service(store)
    first.restore_thread_continuity(request_identity)
    first.persist_thread_continuity(
        request_identity,
        routing_state("192.0.2.10", "192.0.2.11"),
    )

    second = continuity_service(SQLiteThreadStateStore(store.database))
    second.restore_thread_continuity(request_identity)
    restored = second.routing_state_store.get(request_identity.session_id)

    assert restored.active_entities == ("192.0.2.10", "192.0.2.11")
    assert restored.active_entity_count == 2


def test_service_different_conversations_and_users_do_not_share_state(
    tmp_path: Path,
) -> None:
    store = SQLiteThreadStateStore(database(tmp_path))
    owner = identity()
    first = continuity_service(store)
    first.restore_thread_continuity(owner)
    first.persist_thread_continuity(owner, routing_state("192.0.2.10"))

    other_conversation = identity(
        conversation="conversation-b",
        session="shared-session",
        request="request-b",
    )
    second = continuity_service(store)
    second.restore_thread_continuity(other_conversation)
    assert second.routing_state_store.get("shared-session").active_entities == ()

    other_user_same_thread = identity(
        user="user-b",
        conversation="conversation-a",
        session="shared-session",
        request="request-c",
    )
    second.routing_state_store.set(
        "shared-session",
        routing_state("198.51.100.20"),
    )
    second.restore_thread_continuity(other_user_same_thread)
    assert second.routing_state_store.get("shared-session").active_entities == ()


def test_storage_failure_is_nonfatal_and_public_health_remains_independent() -> None:
    class BrokenStore:
        def load(self, **_kwargs):
            raise RuntimeError("secret database detail")

        def save(self, **_kwargs):
            raise RuntimeError("secret database detail")

    service = continuity_service(BrokenStore())
    request_identity = identity()

    service.restore_thread_continuity(request_identity)
    service.persist_thread_continuity(
        request_identity,
        routing_state("192.0.2.10"),
    )

    assert service.routing_state_store.get(request_identity.session_id).active_entities == ()
    from src.api.routes import health

    assert health().model_dump() == {"status": "ok"}


def test_identity_metadata_never_enters_model_messages(tmp_path: Path) -> None:
    db = database(tmp_path)
    request_identity = identity(
        user="private-user-id",
        conversation="private-conversation-id",
        session="private-session-id",
        request="private-request-id",
    )
    llm = FakeLLMClient(
        [
            fake_result(GENERAL_ROUTE),
            fake_result("Kerberos is a network authentication protocol."),
        ]
    )
    service = CopilotService(
        make_settings(conversation_summary_enabled=False),
        llm,
        MemoryStore(10),
        SessionRoutingStateStore(),
        chat_repository=SQLiteChatRepository(db),
        thread_state_store=SQLiteThreadStateStore(db),
    )

    result = service.chat(
        "What is Kerberos?",
        request_identity.session_id,
        request_id=request_identity.request_id,
        request_identity=request_identity,
    )
    serialized_messages = json.dumps(
        [call["messages"] for call in llm.calls],
        ensure_ascii=False,
    )

    assert result["answer"] == "Kerberos is a network authentication protocol."
    for value in (
        request_identity.user_id,
        request_identity.conversation_id,
        request_identity.session_id,
        request_identity.request_id,
        request_identity.thread_key,
    ):
        assert value not in serialized_messages
    service.close()
