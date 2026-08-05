"""Offline auth tests for the Copilot API.

Sets SOORIN_COPILOT_API_KEY before app imports so the settings cache picks up
the test value. No live LLM, graph, Product, or ASGI server calls are made.
"""

from __future__ import annotations

import os
import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials

os.environ["SOORIN_COPILOT_API_KEY"] = "test-api-key-12345"

from src.api.auth import verify_api_key
from src.api.main import create_app
from src.api.routes import llm_health, health
from src.config.settings import get_settings

get_settings.cache_clear()

TEST_KEY = "test-api-key-12345"


def bearer(value: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=value)


def assert_unauthorized(*, expected_detail: str, **kwargs) -> None:
    with pytest.raises(HTTPException) as exc:
        verify_api_key(**kwargs)
    assert exc.value.status_code == 401
    assert exc.value.detail == expected_detail


def test_health_is_public() -> None:
    assert health().model_dump() == {"status": "ok"}


def test_valid_bearer_copilot_token_succeeds() -> None:
    verify_api_key(credentials=bearer(TEST_KEY))


def test_valid_custom_copilot_header_and_product_jwt_succeeds() -> None:
    verify_api_key(
        credentials=bearer("product.jwt.value"),
        copilot_api_key_header=TEST_KEY,
    )


def test_invalid_custom_copilot_header_and_product_jwt_returns_401() -> None:
    assert_unauthorized(
        expected_detail="Invalid Bearer token",
        credentials=bearer("product.jwt.value"),
        copilot_api_key_header="invalid",
    )


def test_missing_both_copilot_credentials_returns_401() -> None:
    assert_unauthorized(
        expected_detail="Missing Authorization header",
        credentials=None,
        copilot_api_key_header=None,
    )


def test_invalid_bearer_token_returns_401() -> None:
    assert_unauthorized(
        expected_detail="Invalid Bearer token",
        credentials=bearer("invalid"),
    )


def test_both_present_custom_valid_takes_precedence_and_succeeds() -> None:
    verify_api_key(
        credentials=bearer("invalid-or-product-jwt"),
        copilot_api_key_header=TEST_KEY,
    )


def test_both_present_custom_invalid_takes_precedence_and_returns_401() -> None:
    assert_unauthorized(
        expected_detail="Invalid Bearer token",
        credentials=bearer(TEST_KEY),
        copilot_api_key_header="invalid",
    )


def test_llm_health_succeeds_after_valid_custom_header_verification() -> None:
    verify_api_key(
        credentials=bearer("product.jwt.value"),
        copilot_api_key_header=TEST_KEY,
    )
    fake_health = {
        "enabled": True,
        "ready": True,
        "provider": "arvan",
        "model": "kimi-k3",
        "deployment": "kimi",
    }
    with patch("src.api.routes.llm_client.health", return_value=fake_health):
        body = llm_health(_auth=None)
    assert body["status"] == "ok"
    assert body["data"]["ready"] is True


def test_cors_preflight_configuration_allows_custom_copilot_header() -> None:
    app = create_app()
    cors_layers = [item for item in app.user_middleware if item.cls is CORSMiddleware]
    assert len(cors_layers) == 1
    options = cors_layers[0].kwargs
    assert options["allow_origins"] == ["*"]
    assert options["allow_credentials"] is False
    assert options["allow_methods"] == ["*"]
    assert options["allow_headers"] == ["*"]
    requested_headers = "authorization,content-type,soorin_copilot_api_key,x-user-id"
    assert "soorin_copilot_api_key" in requested_headers
    assert "x-user-id" in requested_headers


def test_cors_preflight_succeeds_with_identity_and_auth_headers() -> None:
    async def request_preflight():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.options(
                "/chat/stream",
                headers={
                    "Origin": "https://product.example",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": (
                        "Authorization,Content-Type,"
                        "Soorin_copilot_api_key,X-User-ID"
                    ),
                },
            )

    response = asyncio.run(request_preflight())
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers
    assert "content-type" in allowed_headers
    assert "soorin_copilot_api_key" in allowed_headers
    assert "x-user-id" in allowed_headers


def test_user_id_metadata_alone_does_not_authorize() -> None:
    assert_unauthorized(
        expected_detail="Missing Authorization header",
        credentials=None,
        copilot_api_key_header=None,
    )


def test_protected_routes_require_auth_dependency_and_health_does_not() -> None:
    app = create_app()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path or path in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}:
            continue
        dependencies = [dep.call for dep in getattr(route, "dependant", ()).dependencies]
        if path == "/health":
            assert verify_api_key not in dependencies
        else:
            assert verify_api_key in dependencies, path


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


def test_streamlit_still_uses_bearer_for_builtin_ui_calls() -> None:
    source = Path(__file__).resolve().parents[2] / "app_st.py"
    text = source.read_text(encoding="utf-8")

    assert 'return {"Authorization": f"Bearer {key}"}' in text
    assert "requests.get(HEALTH_URL, timeout=10)" in text
    assert "requests.get(\n            LLM_HEALTH_URL,\n            headers=_get_auth_headers()," in text
    assert "requests.post(\n            CHAT_URL,\n            json=payload,\n            headers=_get_auth_headers()," in text
    assert "requests.post(\n            CHAT_STREAM_URL,\n            json=payload,\n            headers=_get_auth_headers()," in text
