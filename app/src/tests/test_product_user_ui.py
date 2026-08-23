"""Offline orchestration tests for the Product-backed Streamlit controller."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from src.config.settings import get_settings
from src.web.local_simulation import LocalConversation
from src.web.product_user_chat import (
    ProductChatMessage,
    ProductChatRoom,
    ProductUserChatError,
    ProductUserChatHTTPError,
)
from src.web.product_user_ui import (
    PRODUCT_TOKEN_KEY,
    PRODUCT_TURN_KEY,
    PRODUCT_USER_ID_KEY,
    ProductConversationBackend,
    ProductCopilotStreamError,
    clear_product_ui_state,
)


def settings():
    return SimpleNamespace(
        api_base_url="http://copilot.invalid",
        api_timeout_seconds=30,
        copilot_api_key="copilot-key",
    )


def conversation(room_id="room-1"):
    return LocalConversation(room_id, "product-user", room_id, "Room", "", "")


class Client:
    token = "product-jwt"

    def __init__(self):
        self.calls = []
        self.fail_role = ""

    def list_rooms(self):
        self.calls.append(("list",))
        return [ProductChatRoom("room-1", "Room")]

    def create_room(self, title, asset_ip=""):
        self.calls.append(("create", title, asset_ip))
        return ProductChatRoom("room-new", title, asset_ip)

    def get_room(self, room_id):
        self.calls.append(("get", room_id))
        return ProductChatRoom(room_id, "Room", messages=(ProductChatMessage("user", "old"),))

    def append_message(self, room_id, role, content):
        self.calls.append(("append", room_id, role, content))
        if role == self.fail_role:
            raise ProductUserChatHTTPError("chat", 500)
        return {}

    def delete_room(self, room_id):
        self.calls.append(("delete", room_id))


class StreamResponse:
    def __init__(self, records, status_code=200):
        self.records, self.status_code, self.encoding = records, status_code, ""

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def raise_for_status(self):
        if self.status_code >= 400: raise requests.HTTPError()
    def iter_lines(self, **kwargs): return iter(self.records)


class HTTP:
    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def state():
    return {PRODUCT_TOKEN_KEY: "product-jwt", PRODUCT_USER_ID_KEY: "product-user"}


def done_response():
    return StreamResponse([
        'data: {"type":"answer_delta","text":"answer"}',
        "",
        'data: {"type":"done"}',
        "",
    ])


def test_product_room_backend_uses_only_product_client_and_room_ids():
    client, current = Client(), state()
    current["selected_copilot_ip"] = "192.0.2.8"
    backend = ProductConversationBackend(settings(), client, current)
    assert backend.list_conversations()[0].conversation_id == "room-1"
    created = backend.create_conversation("Asset investigation")
    assert created.session_id == "room-new"
    assert ("create", "Asset investigation", "192.0.2.8") in client.calls
    assert backend.get_messages("room-1") == [{"role": "user", "content": "old"}]
    backend.delete_conversation("room-1")
    assert ("delete", "room-1") in client.calls


def test_stream_uses_canonical_sse_headers_ids_and_ui_context_once():
    client, current, http = Client(), state(), HTTP(done_response())
    backend = ProductConversationBackend(settings(), client, current, http=http)
    events = list(backend.stream_chat(conversation(), request_id="request-1", message="question", selected_ip="192.0.2.5"))
    assert [event["type"] for event in events] == ["answer_delta", "done"]
    assert client.calls.count(("append", "room-1", "user", "question")) == 1
    assert len(http.calls) == 1
    url, kwargs = http.calls[0]
    assert url == "http://copilot.invalid/chat/stream"
    assert kwargs["headers"]["Authorization"] == "Bearer product-jwt"
    assert kwargs["headers"]["Soorin_copilot_api_key"] == "copilot-key"
    assert kwargs["headers"]["X-User-ID"] == "product-user"
    assert kwargs["headers"]["Accept"] == "text/event-stream"
    assert kwargs["json"] == {
        "conversation_id": "room-1",
        "session_id": "room-1",
        "request_id": "request-1",
        "message": "question",
        "ui_context": {"selected_ip": "192.0.2.5"},
    }
    messages = backend.complete_display_turn(conversation(), request_id="request-1", user_content="question", assistant_content="answer")
    assert ("append", "room-1", "assistant", "answer") in client.calls
    assert messages == [{"role": "user", "content": "old"}]
    assert current[PRODUCT_TURN_KEY]["completed"] is True


def test_rerun_guard_prevents_duplicate_user_and_copilot_calls():
    client, current, http = Client(), state(), HTTP(done_response())
    backend = ProductConversationBackend(settings(), client, current, http=http)
    list(backend.stream_chat(conversation(), request_id="same", message="question", selected_ip=None))
    with pytest.raises(ProductCopilotStreamError, match="already submitted"):
        list(backend.stream_chat(conversation(), request_id="same", message="question", selected_ip=None))
    assert client.calls.count(("append", "room-1", "user", "question")) == 1
    assert len(http.calls) == 1


def test_incomplete_turn_cannot_be_replaced_by_a_new_request():
    client = Client()
    current = state()
    backend = ProductConversationBackend(
        settings(),
        client,
        current,
        http=HTTP(StreamResponse(['data: {"type":"answer_delta","text":"partial"}', ""])),
    )
    list(backend.stream_chat(conversation(), request_id="first", message="question", selected_ip=None))
    with pytest.raises(ProductCopilotStreamError, match="previous Product turn"):
        list(backend.stream_chat(conversation(), request_id="second", message="next", selected_ip=None))
    assert client.calls.count(("append", "room-1", "user", "question")) == 1
    assert not any(call[-1:] == ("next",) for call in client.calls)


def test_user_append_failure_prevents_copilot():
    client, http = Client(), HTTP(done_response())
    client.fail_role = "user"
    backend = ProductConversationBackend(settings(), client, state(), http=http)
    with pytest.raises(ProductUserChatHTTPError):
        list(backend.stream_chat(conversation(), request_id="r", message="q", selected_ip=None))
    assert http.calls == []


def test_stream_without_done_prevents_assistant_persistence():
    client = Client()
    http = HTTP(StreamResponse(['data: {"type":"answer_delta","text":"partial"}', ""]))
    backend = ProductConversationBackend(settings(), client, state(), http=http)
    list(backend.stream_chat(conversation(), request_id="r", message="q", selected_ip=None))
    with pytest.raises(ProductUserChatError, match="not completed"):
        backend.complete_display_turn(conversation(), request_id="r", user_content="q", assistant_content="partial")
    assert not any(call[:3] == ("append", "room-1", "assistant") for call in client.calls)


def test_completed_turn_cannot_persist_assistant_into_another_room():
    client, current = Client(), state()
    backend = ProductConversationBackend(settings(), client, current, http=HTTP(done_response()))
    list(backend.stream_chat(conversation(), request_id="r", message="q", selected_ip=None))
    with pytest.raises(ProductUserChatError, match="not completed"):
        backend.complete_display_turn(
            conversation("room-2"),
            request_id="r",
            user_content="q",
            assistant_content="answer",
        )
    assert not any(call[:3] == ("append", "room-2", "assistant") for call in client.calls)


def test_failed_assistant_save_is_retryable_without_reappending_user():
    client, current = Client(), state()
    client.fail_role = "assistant"
    backend = ProductConversationBackend(settings(), client, current, http=HTTP(done_response()))
    list(backend.stream_chat(conversation(), request_id="r", message="q", selected_ip=None))
    with pytest.raises(ProductUserChatHTTPError):
        backend.complete_display_turn(conversation(), request_id="r", user_content="q", assistant_content="answer")
    assert current[PRODUCT_TURN_KEY]["user_persisted"] is True
    assert current[PRODUCT_TURN_KEY]["assistant_persisted"] is False
    client.fail_role = ""
    assert backend.retry_pending_assistant("room-1") is True
    assert client.calls.count(("append", "room-1", "user", "q")) == 1
    assert client.calls.count(("append", "room-1", "assistant", "answer")) == 2


def test_product_401_clears_interactive_session():
    current = state()
    backend = ProductConversationBackend(settings(), Client(), current, http=HTTP(StreamResponse([], 401)))
    with pytest.raises(ProductCopilotStreamError, match="expired"):
        list(backend.stream_chat(conversation(), request_id="r", message="q", selected_ip=None))
    assert PRODUCT_TOKEN_KEY not in current and PRODUCT_USER_ID_KEY not in current


def test_logout_clears_all_product_state():
    current = state() | {PRODUCT_TURN_KEY: {"request_id": "r"}, "unrelated": True}
    clear_product_ui_state(current)
    assert current == {"unrelated": True}


def test_product_login_is_login_only_and_app_has_one_early_product_dispatch():
    root = Path(__file__).resolve().parents[2]
    product_ui = (root / "src" / "web" / "product_user_ui.py").read_text(encoding="utf-8")
    app_source = (root / "app_st.py").read_text(encoding="utf-8")
    assert 'st.form("product_login_form"' in product_ui
    assert 'clear_on_submit=True' in product_ui
    assert "Sign Up" not in product_ui and "Create account" not in product_ui
    assert app_source.count('settings.streamlit_auth_backend == "product"') == 1
    assert app_source.index('settings.streamlit_auth_backend == "product"') < app_source.index("def init_graph")
    assert "run_local_simulation_workspace(settings)" not in product_ui


def test_product_mode_configuration_rejects_local_or_non_product_persistence():
    base = get_settings()
    valid = replace(
        base,
        streamlit_auth_backend="product",
        local_product_simulation_enabled=False,
        thread_state_backend="product",
        long_term_memory_enabled=True,
        long_term_memory_backend="product",
        product_api_base_url="https://product.invalid",
        product_login_path="/auth/login",
        product_chat_rooms_path="/chat-rooms",
        product_hwid="hwid",
        copilot_api_key="copilot-key",
    )
    valid.validate_local_persistence_configuration()

    invalid_variants = (
        replace(valid, local_product_simulation_enabled=True),
        replace(valid, thread_state_backend="memory"),
        replace(valid, long_term_memory_enabled=False),
        replace(valid, long_term_memory_backend="sqlite"),
        replace(valid, product_chat_rooms_path=""),
    )
    for invalid in invalid_variants:
        with pytest.raises(ValueError):
            invalid.validate_local_persistence_configuration()
