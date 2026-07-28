"""Offline auth tests for the Copilot API.

Sets SOORIN_COPILOT_API_KEY before any app imports so the settings cache
picks up the test value.  No live LLM/graph calls are made."""
from __future__ import annotations

import os

os.environ["SOORIN_COPILOT_API_KEY"] = "test-api-key-12345"

from unittest.mock import patch

from starlette.testclient import TestClient

# Clear any previously cached settings (e.g. from another test file)
# BEFORE importing create_app, so the env var takes effect.
from src.config.settings import get_settings

get_settings.cache_clear()

from src.api.main import create_app

TEST_KEY = "test-api-key-12345"


def test_health_is_public() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_passes_with_invalid_auth() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/health", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 200


def test_llm_health_401_without_auth() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/llm/health")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing Authorization header"


def test_llm_health_401_with_invalid_token() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/llm/health", headers={"Authorization": "Bearer invalid"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid Bearer token"


def test_llm_health_200_with_valid_token() -> None:
    app = create_app()
    client = TestClient(app)
    fake_health = {
        "enabled": True,
        "ready": True,
        "provider": "arvan",
        "model": "kimi-k3",
        "deployment": "kimi",
    }
    with patch("src.api.routes.llm_client.health", return_value=fake_health):
        resp = client.get(
            "/llm/health",
            headers={"Authorization": f"Bearer {TEST_KEY}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["data"]["ready"] is True


def test_chat_401_without_auth() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.post("/chat", json={"message": "test"})
    assert resp.status_code == 401


def test_chat_stream_401_without_auth() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.post("/chat/stream", json={"message": "test"})
    assert resp.status_code == 401


def test_graph_status_401_without_auth() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/graph/status")
    assert resp.status_code == 401


def test_graph_neighbors_401_without_auth() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/graph/nodes/192.168.1.1/neighbors")
    assert resp.status_code == 401


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
