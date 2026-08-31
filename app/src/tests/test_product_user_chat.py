"""Offline contract tests for interactive Product login and chatroom transport."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
import requests

from src.web.product_user_chat import (
    ProductUserChatClient,
    ProductUserChatError,
    ProductUserChatHTTPError,
    product_user_id,
)


def settings():
    return SimpleNamespace(
        product_api_base_url="https://product.invalid",
        product_login_path="/auth/login",
        product_chat_rooms_path="/chat-rooms",
        product_hwid="configured-hwid",
        product_captcha_bypass="configured-captcha",
        product_connect_timeout_seconds=2,
        product_read_timeout_seconds=5,
        product_username="service-user-must-not-be-used",
        product_password="service-password-must-not-be-used",
    )


class Response:
    def __init__(self, payload=None, status_code=200):
        self.payload, self.status_code = payload, status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)

    def request(self, method, url, **kwargs):
        return self._next(method, url, kwargs)


def jwt(sub: str) -> str:
    header = base64.urlsafe_b64encode(b'{}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


@pytest.mark.parametrize("identity_key", ["userId", "user_id"])
def test_login_uses_runtime_credentials_and_explicit_identity(identity_key):
    transport = Session([Response({"accessToken": "interactive-token", identity_key: 42})])
    result = ProductUserChatClient(settings(), session=transport).login("runtime-user", "runtime-password")
    method, url, kwargs = transport.calls[0]
    assert (method, url) == ("POST", "https://product.invalid/auth/login")
    assert kwargs["json"] == {"username": "runtime-user", "password": "runtime-password"}
    assert kwargs["headers"]["x-hwid"] == "configured-hwid"
    assert kwargs["headers"]["x-captcha-bypass"] == "configured-captcha"
    assert result.access_token == "interactive-token" and result.user_id == "42"


def test_login_uses_jwt_sub_fallback_with_correct_base64url_padding():
    token = jwt("product-user-7")
    result = ProductUserChatClient(settings(), session=Session([Response({"accessToken": token})])).login("u", "p")
    assert result.user_id == "product-user-7"


def test_nested_documented_user_identity_is_supported_but_arbitrary_top_level_id_is_not():
    assert product_user_id({"user": {"id": "nested-user"}}, jwt("subject")) == "nested-user"
    assert product_user_id({"id": "room-shaped-id"}, jwt("subject")) == "subject"


@pytest.mark.parametrize("payload", [{"accessToken": "malformed"}, {"accessToken": jwt("")}])
def test_login_rejects_missing_or_malformed_identity(payload):
    with pytest.raises(ProductUserChatError, match="identity"):
        ProductUserChatClient(settings(), session=Session([Response(payload)])).login("u", "p")


def test_login_rejects_missing_token_and_invalid_json():
    with pytest.raises(ProductUserChatError, match="access token"):
        ProductUserChatClient(settings(), session=Session([Response({"userId": "u"})])).login("u", "p")
    with pytest.raises(ProductUserChatError, match="invalid JSON"):
        ProductUserChatClient(settings(), session=Session([Response(ValueError())])).login("u", "p")


def test_crud_requires_interactive_token():
    with pytest.raises(ProductUserChatError, match="authentication"):
        ProductUserChatClient(settings()).list_rooms()


def test_list_rooms_uses_bearer_hwid_and_pagination_and_accepts_array():
    transport = Session([Response([{"roomId": "room-1", "title": "One"}])])
    rooms = ProductUserChatClient(settings(), token="interactive", session=transport).list_rooms(limit=25, offset=5)
    method, url, kwargs = transport.calls[0]
    assert (method, url) == ("GET", "https://product.invalid/chat-rooms")
    assert kwargs["params"] == {"limit": 25, "offset": 5}
    assert kwargs["headers"]["Authorization"] == "Bearer interactive"
    assert kwargs["headers"]["x-hwid"] == "configured-hwid"
    assert "Content-Type" not in kwargs["headers"]
    assert rooms[0].room_id == "room-1"


def test_list_rooms_accepts_wrapped_items():
    response = {"data": {"items": [{"id": "room-2"}]}}
    rooms = ProductUserChatClient(settings(), token="t", session=Session([Response(response)])).list_rooms()
    assert [room.room_id for room in rooms] == ["room-2"]


def test_create_detail_append_and_delete_contracts():
    transport = Session([
        Response({"roomId": "r1", "title": "Asset", "assetIp": "192.0.2.1"}, 201),
        Response({"roomId": "r1", "messages": [{"role": "user", "content": "hello"}]}),
        Response({"messageId": "m1"}, 201),
        Response({"messageId": "m2"}, 201),
        Response(None, 204),
    ])
    client = ProductUserChatClient(settings(), token="t", session=transport)
    room = client.create_room("Asset", "192.0.2.1")
    detail = client.get_room("r1")
    client.append_message("r1", "user", "question")
    client.append_message("r1", "assistant", "answer")
    client.delete_room("r1")
    assert room.room_id == "r1" and detail.messages[0].content == "hello"
    assert transport.calls[0][2]["json"] == {"title": "Asset", "assetIp": "192.0.2.1"}
    assert transport.calls[0][2]["headers"]["Content-Type"] == "application/json"
    assert transport.calls[1][1].endswith("/chat-rooms/r1")
    assert transport.calls[2][2]["json"] == {"role": "user", "content": "question"}
    assert transport.calls[3][2]["json"] == {"role": "assistant", "content": "answer"}
    assert transport.calls[4][0] == "DELETE"


@pytest.mark.parametrize("role", ["system", "tool", ""])
def test_append_rejects_unsupported_roles(role):
    with pytest.raises(ProductUserChatError, match="invalid"):
        ProductUserChatClient(settings(), token="t").append_message("r", role, "content")


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429, 500, 503])
def test_http_failures_are_bounded_and_classified(status):
    client = ProductUserChatClient(settings(), token="t", session=Session([Response({}, status)]))
    with pytest.raises(ProductUserChatHTTPError) as captured:
        client.list_rooms()
    assert captured.value.status_code == status
    assert "Bearer" not in str(captured.value)


def test_network_failure_is_bounded(monkeypatch):
    transport = Session([])
    monkeypatch.setattr(transport, "request", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()))
    with pytest.raises(ProductUserChatError, match="unavailable"):
        ProductUserChatClient(settings(), token="t", session=transport).list_rooms()


def test_credentials_and_token_are_never_logged(caplog):
    transport = Session([Response({"accessToken": "secret-token", "userId": "u"})])
    ProductUserChatClient(settings(), session=transport).login("runtime", "secret-password")
    text = caplog.text
    assert "secret-token" not in text
    assert "secret-password" not in text
    assert "Authorization" not in text
