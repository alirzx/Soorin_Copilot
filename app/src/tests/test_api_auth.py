"""Offline auth tests for the Copilot API.

Sets SOORIN_COPILOT_API_KEY before any app imports so the settings cache
picks up the test value.  No live LLM/graph calls are made."""
from __future__ import annotations

import os
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

os.environ["SOORIN_COPILOT_API_KEY"] = "test-api-key-12345"

from unittest.mock import patch

# Clear any previously cached settings (e.g. from another test file)
# BEFORE importing create_app, so the env var takes effect.
from src.config.settings import get_settings

get_settings.cache_clear()

from src.api.main import create_app
from src.api.dependencies import get_graph_service

TEST_KEY = "test-api-key-12345"


def request(
    app,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    async def run() -> tuple[int, dict[str, str], bytes]:
        raw_body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        parsed_path, _, query = path.partition("?")
        response_status = 0
        response_headers: dict[str, str] = {}
        response_body = bytearray()
        request_headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        ]
        if json_body is not None:
            request_headers.append((b"content-type", b"application/json"))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": parsed_path,
            "raw_path": parsed_path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": request_headers,
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }

        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal response_status, response_headers
            if message["type"] == "http.response.start":
                response_status = message["status"]
                response_headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))

        await app(scope, receive, send)
        return response_status, response_headers, bytes(response_body)

    return asyncio.run(run())


def json_response(body: bytes) -> dict[str, object]:
    return json.loads(body.decode("utf-8"))


def test_health_is_public() -> None:
    app = create_app()
    status, _headers, body = request(app, "GET", "/health")
    assert status == 200
    assert json_response(body) == {"status": "ok"}


def test_health_passes_with_invalid_auth() -> None:
    app = create_app()
    status, _headers, _body = request(
        app,
        "GET",
        "/health",
        headers={"Authorization": "Bearer garbage"},
    )
    assert status == 200


def test_llm_health_401_without_auth() -> None:
    app = create_app()
    status, _headers, body = request(app, "GET", "/llm/health")
    assert status == 401
    assert json_response(body)["detail"] == "Missing Authorization header"


def test_llm_health_401_with_invalid_token() -> None:
    app = create_app()
    status, _headers, body = request(
        app,
        "GET",
        "/llm/health",
        headers={"Authorization": "Bearer invalid"},
    )
    assert status == 401
    assert json_response(body)["detail"] == "Invalid Bearer token"


def test_llm_health_200_with_valid_token() -> None:
    app = create_app()
    fake_health = {
        "enabled": True,
        "ready": True,
        "provider": "arvan",
        "model": "kimi-k3",
        "deployment": "kimi",
    }
    with patch("src.api.routes.llm_client.health", return_value=fake_health):
        status, _headers, body = request(
            app,
            "GET",
            "/llm/health",
            headers={"Authorization": f"Bearer {TEST_KEY}"},
        )
    assert status == 200
    payload = json_response(body)
    assert payload["status"] == "ok"
    assert payload["data"]["ready"] is True


def test_cors_preflight_allows_browser_authorization_header() -> None:
    app = create_app()
    status, headers, _body = request(
        app,
        "OPTIONS",
        "/chat",
        headers={
            "Origin": "http://frontend.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert status == 200
    assert headers["access-control-allow-origin"] == "*"
    assert "POST" in headers["access-control-allow-methods"]
    assert "authorization" in headers["access-control-allow-headers"].lower()


def test_chat_401_without_auth() -> None:
    app = create_app()
    status, _headers, _body = request(app, "POST", "/chat", json_body={"message": "test"})
    assert status == 401


def test_chat_stream_401_without_auth() -> None:
    app = create_app()
    status, _headers, _body = request(
        app,
        "POST",
        "/chat/stream",
        json_body={"message": "test"},
    )
    assert status == 401


def test_graph_status_401_without_auth() -> None:
    app = create_app()
    status, _headers, _body = request(app, "GET", "/graph/status")
    assert status == 401


def test_graph_neighbors_401_without_auth() -> None:
    app = create_app()
    status, _headers, _body = request(app, "GET", "/graph/nodes/192.168.1.1/neighbors")
    assert status == 401


def test_graph_status_200_with_valid_token() -> None:
    app = create_app()

    class FakeGraphService:
        def status(self):
            return SimpleNamespace(
                loaded=False,
                nodes=0,
                edges=0,
                directed=True,
                artifact_available=False,
                active_graph_loaded_at=None,
                active_graph_source=None,
                active_graph_version=None,
                refresh_enabled=False,
                refresh_running=False,
                refresh_interval_seconds=0,
                refresh_last_attempt_at=None,
                refresh_last_success_at=None,
                refresh_last_failure_at=None,
                refresh_last_error_type=None,
                refresh_last_error_message=None,
                refresh_consecutive_failures=0,
                raw_snapshot_path=None,
                processed_snapshot_path=None,
                last_known_good=False,
            )

    app.dependency_overrides[get_graph_service] = lambda: FakeGraphService()
    status, _headers, body = request(
        app,
        "GET",
        "/graph/status",
        headers={"Authorization": f"Bearer {TEST_KEY}"},
    )
    assert status == 200
    assert json_response(body)["loaded"] is False
    app.dependency_overrides.clear()


def test_openapi_includes_bearer_security() -> None:
    app = create_app()
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert "HTTPBearer" in schemes
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    assert schemes["HTTPBearer"]["type"] == "http"
    protected = schema["paths"]["/llm/health"]["get"]
    assert "security" in protected
    public = schema["paths"]["/health"]["get"]
    assert "security" not in public


def test_streamlit_uses_bearer_only_for_protected_api_calls() -> None:
    source = Path(__file__).resolve().parents[2] / "app_st.py"
    text = source.read_text(encoding="utf-8")

    assert 'return {"Authorization": f"Bearer {key}"}' in text
    assert "requests.get(HEALTH_URL, timeout=10)" in text
    assert "requests.get(\n            LLM_HEALTH_URL,\n            headers=_get_auth_headers()," in text
    assert "requests.post(\n            CHAT_URL,\n            json=payload,\n            headers=_get_auth_headers()," in text
    assert "requests.post(\n            CHAT_STREAM_URL,\n            json=payload,\n            headers=_get_auth_headers()," in text
