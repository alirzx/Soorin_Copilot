"""Offline Gate 4 tests for local simulation API, migration, and UI helpers."""

from __future__ import annotations

import inspect
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from src.api.auth import verify_api_key
from src.api.main import create_app
from src.api import local_simulation_routes as local_routes
from src.api.schemas.local_simulation import LocalConversationCreateRequest, LocalUserCreateRequest
from src.config.settings import get_settings
from src.core.memory.factory import LocalPersistenceAdapters
from src.core.memory.persistence import LOCAL_SCHEMA_VERSION
from src.core.memory.sqlite import LocalSQLiteDatabase, SQLiteChatRepository
from src.web.local_simulation import (
    LOCAL_CONVERSATION_KEY,
    LOCAL_MESSAGES_KEY,
    LOCAL_USER_KEY,
    LocalSimulationApiClient,
    clear_local_ui_state,
    copilot_auth_headers,
)


def enabled_settings(path: Path, **overrides):
    values = {
        "local_product_simulation_enabled": True,
        "streamlit_auth_backend": "local_simulation",
        "local_test_user_creation_enabled": True,
        "thread_state_backend": "sqlite",
        "local_sqlite_path": str(path),
    }
    values.update(overrides)
    return replace(get_settings(), **values)


def local_repository(tmp_path: Path) -> SQLiteChatRepository:
    database = LocalSQLiteDatabase(tmp_path / "runtime" / "local.sqlite3")
    database.initialize()
    return SQLiteChatRepository(database)


def local_routes_ready(monkeypatch, repository: SQLiteChatRepository, settings) -> None:
    monkeypatch.setattr(local_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        local_routes,
        "get_local_persistence",
        lambda: LocalPersistenceAdapters(chat_repository=repository),
    )
def test_disabled_local_routes_return_typed_not_enabled(monkeypatch, tmp_path: Path) -> None:
    settings = enabled_settings(tmp_path / "disabled.sqlite3", local_product_simulation_enabled=False, streamlit_auth_backend="none")
    monkeypatch.setattr(local_routes, "get_settings", lambda: settings)
    response = local_routes.list_users(_auth=None)

    assert response.status == "not_enabled"


def test_local_auth_defaults_and_test_user_creation_guard(monkeypatch, tmp_path: Path) -> None:
    assert get_settings().streamlit_auth_backend == "none"
    assert get_settings().local_test_user_creation_enabled is False
    repository = local_repository(tmp_path)
    settings = enabled_settings(repository.database.path, local_test_user_creation_enabled=False)
    local_routes_ready(monkeypatch, repository, settings)

    response = local_routes.create_user(LocalUserCreateRequest(), _auth=None)

    assert response.status == "creation_disabled"


def test_local_routes_remain_protected_by_existing_auth(monkeypatch, tmp_path: Path) -> None:
    settings = enabled_settings(tmp_path / "auth.sqlite3")
    repository = local_repository(tmp_path)
    local_routes_ready(monkeypatch, repository, settings)
    for endpoint in (
        local_routes.list_users,
        local_routes.create_user,
        local_routes.list_conversations,
        local_routes.create_conversation,
        local_routes.get_conversation,
        local_routes.get_messages,
        local_routes.delete_conversation,
    ):
        dependency = inspect.signature(endpoint).parameters["_auth"].default
        assert dependency.dependency is verify_api_key


def test_local_users_conversations_messages_and_ownership(monkeypatch, tmp_path: Path) -> None:
    repository = local_repository(tmp_path)
    settings = enabled_settings(repository.database.path)
    local_routes_ready(monkeypatch, repository, settings)

    created_user = local_routes.create_user(LocalUserCreateRequest(), _auth=None)
    user_id = created_user.user_id
    assert user_id in {item.user_id for item in local_routes.list_users(_auth=None).users}

    with pytest.raises(Exception) as missing_user:
        local_routes.create_conversation(LocalConversationCreateRequest(title="Case"), x_user_id=None, _auth=None)
    assert getattr(missing_user.value, "status_code", None) == 422
    conversation = local_routes.create_conversation(
        LocalConversationCreateRequest(title="Case"),
        x_user_id=user_id,
        _auth=None,
    )
    assert conversation.session_id

    reopened = local_routes.get_conversation(
        conversation.conversation_id,
        x_user_id=user_id,
        _auth=None,
    )
    assert reopened.session_id == conversation.session_id
    repository.commit_turn(
        user_id=user_id,
        conversation_id=conversation.conversation_id,
        request_id="turn-1",
        user_content="question",
        assistant_content="answer",
    )
    messages = local_routes.get_messages(
        conversation.conversation_id,
        limit=100,
        x_user_id=user_id,
        _auth=None,
    )
    assert [item.content for item in messages.messages] == ["question", "answer"]
    with pytest.raises(Exception) as forbidden:
        local_routes.get_conversation(conversation.conversation_id, x_user_id="other-user", _auth=None)
    assert getattr(forbidden.value, "status_code", None) == 404
    assert local_routes.delete_conversation(conversation.conversation_id, x_user_id=user_id, _auth=None).deleted is True


def test_local_openapi_has_no_message_write_route_and_marks_local_only(monkeypatch, tmp_path: Path) -> None:
    local_routes_ready(monkeypatch, local_repository(tmp_path), enabled_settings(tmp_path / "api.sqlite3"))
    app = create_app()
    schema = app.openapi()

    message_path = "/local-simulation/conversations/{conversation_id}/messages"
    assert set(schema["paths"][message_path]) == {"get"}
    assert "Local development only" in schema["paths"]["/local-simulation/users"]["get"]["description"]


def test_gate3_database_migrates_to_stable_conversation_session(tmp_path: Path) -> None:
    path = tmp_path / "gate3.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_metadata VALUES ('local_schema_version', '1');
        CREATE TABLE local_users (user_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE local_conversations (
            conversation_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE local_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL,
            position INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE local_thread_states (
            thread_key TEXT PRIMARY KEY,
            user_id TEXT,
            conversation_id TEXT,
            session_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO local_users VALUES ('user-a', '2026-01-01T00:00:00+00:00');
        INSERT INTO local_conversations VALUES ('conversation-a', 'user-a', 'Case', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        INSERT INTO local_messages VALUES ('message-a', 'conversation-a', 'request-a', 'user', 'existing', 'completed', 1, '2026-01-01T00:00:00+00:00');
        INSERT INTO local_thread_states VALUES ('conversation-a', 'user-a', 'conversation-a', 'legacy-session', 1, 1, '{}', '2026-01-01T00:00:00+00:00');
        """
    )
    connection.close()

    database = LocalSQLiteDatabase(path)
    database.initialize()
    database.initialize()
    repository = SQLiteChatRepository(database)
    conversation = repository.get_conversation(user_id="user-a", conversation_id="conversation-a")

    assert conversation is not None and conversation.session_id
    with database.connect() as upgraded:
        assert upgraded.execute("SELECT value FROM schema_metadata WHERE key = 'local_schema_version'").fetchone()[0] == str(LOCAL_SCHEMA_VERSION)
        assert upgraded.execute("SELECT session_id FROM local_conversations WHERE conversation_id = 'conversation-a'").fetchone()[0] == conversation.session_id
        assert upgraded.execute("SELECT content FROM local_messages WHERE message_id = 'message-a'").fetchone()[0] == "existing"
        assert upgraded.execute("SELECT session_id FROM local_thread_states WHERE thread_key = 'conversation-a'").fetchone()[0] == "legacy-session"


def test_local_ui_helpers_clear_state_and_send_both_auth_headers() -> None:
    state = {
        LOCAL_USER_KEY: "user-a",
        LOCAL_CONVERSATION_KEY: "conversation-a",
        LOCAL_MESSAGES_KEY: [{"content": "secret"}],
        "selected_copilot_ip": "192.0.2.10",
    }
    clear_local_ui_state(state)

    assert state == {}
    assert copilot_auth_headers("copilot-key") == {
        "Authorization": "Bearer copilot-key",
        "Soorin_copilot_api_key": "copilot-key",
    }


def test_local_stream_request_uses_stable_identity_and_selected_ip(monkeypatch) -> None:
    captured = {}

    class Response:
        status_code = 200
        encoding = "utf-8"

        def raise_for_status(self):
            return None

        def iter_lines(self, **_kwargs):
            return [
                'data: {"type":"answer_delta","text":"ok"}',
                "",
                'data: {"type":"done","data":{}}',
                "",
            ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("src.web.local_simulation.requests.post", fake_post)
    client = LocalSimulationApiClient(api_base_url="http://copilot", api_key="key", timeout_seconds=10)
    from src.web.local_simulation import LocalConversation

    events = list(client.stream_chat(
        user_id="user-a",
        conversation=LocalConversation("conversation-a", "user-a", "session-a", "", "", ""),
        request_id="request-a",
        message="Question",
        selected_ip="192.0.2.10",
    ))

    assert [event["type"] for event in events] == ["answer_delta", "done"]
    assert captured["json"] == {
        "session_id": "session-a",
        "conversation_id": "conversation-a",
        "request_id": "request-a",
        "message": "Question",
        "ui_context": {"selected_ip": "192.0.2.10"},
    }
    assert captured["headers"]["Authorization"] == "Bearer key"
    assert captured["headers"]["Soorin_copilot_api_key"] == "key"
    assert captured["headers"]["X-User-ID"] == "user-a"
