"""Focused tests for lossless product asset endpoints and shared authentication."""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import requests

from src.config.settings import get_settings
from src.core.product_client import ProductApiClient
from src.core.product_client.errors import ProductApiConfigError, ProductApiError, ProductApiHTTPError


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, *, json_error: Exception | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(
        self,
        get_responses: list[FakeResponse | Exception],
        *,
        post_responses: list[FakeResponse | Exception] | None = None,
    ) -> None:
        self.get_responses = list(get_responses)
        self.post_responses = list(post_responses or [])
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    def mount(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str], timeout: tuple[int, int]) -> FakeResponse:
        # This deliberately has no json/data/body parameter: the endpoint is GET-only.
        self.get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        response = self.get_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: tuple[int, int],
    ) -> FakeResponse:
        self.post_calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        response = self.post_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_settings(**overrides: Any):
    values = {
        "product_api_base_url": "http://product.invalid",
        "product_asset_detection_path": "/asset-detection/test/{ip}",
        "product_asset_profile_path": "/profile/{ip}",
        "product_login_path": "/auth/login",
        "product_api_token": "Bearer bootstrap-token",
        "product_username": "test-user",
        "product_password": "test-password",
        "product_captcha_bypass": "test-captcha",
        "product_token_refresh_seconds": 600,
        "product_hwid": "test-hwid",
        "product_connect_timeout_seconds": 7,
        "product_read_timeout_seconds": 11,
        "product_max_retries": 1,
        "product_retry_backoff_seconds": 0.0,
    }
    values.update(overrides)
    return replace(get_settings(), **values)


def detection_payload() -> dict[str, Any]:
    return {
        "ip": "192.0.2.10",
        "assetFound": True,
        "nullable": None,
        "falseValue": False,
        "emptyList": [],
        "emptyObject": {},
        "matchedRules": [
            {"id": index, "evidence": [f"signal-{index}", None, False]}
            for index in range(25)
        ],
        "conflicts": [{"code": f"conflict-{index}"} for index in range(12)],
        "unknownFutureField": {"nested": [{"original_key": "original-value"}]},
    }


def profile_payload() -> dict[str, Any]:
    return {
        "id": "asset-test-1",
        "ip_address": "192.0.2.10",
        "risk_score": 42,
        "identity": {
            "snmp": None,
            "kerberos": {"domain_joined": True, "servers": ["192.0.2.20"]},
            "ldap": {"success": False, "users": []},
            "ntlm": {"observed": True, "details": {}},
            "smb": {"ports": [445], "sessions": []},
            "network": {"mac": "00:00:5e:00:53:01", "open_ports": [80, 443]},
            "detection": {"role": "fake-server", "confidence": None},
        },
        "future_profile_field": {"preserve": [None, False, {}, []]},
    }


class ProductAssetEndpointTests(unittest.TestCase):
    def client(self, session: FakeSession, **settings_overrides: Any) -> ProductApiClient:
        with patch("src.core.product_client.client.requests.Session", return_value=session):
            return ProductApiClient(make_settings(**settings_overrides))

    def test_profile_uses_safe_get_path_shared_bearer_and_hwid_without_body(self) -> None:
        session = FakeSession([FakeResponse(200, profile_payload())])
        result = self.client(session).get_asset_profile(" 192.0.2.10 ", request_id="req-profile")

        self.assertEqual(result.target_ip, "192.0.2.10")
        self.assertEqual(result.endpoint_path, "/profile/192.0.2.10")
        self.assertEqual(session.get_calls[0]["url"], "http://product.invalid/profile/192.0.2.10")
        self.assertEqual(session.get_calls[0]["headers"]["Authorization"], "Bearer bootstrap-token")
        self.assertEqual(session.get_calls[0]["headers"]["x-hwid"], "test-hwid")
        self.assertEqual(session.get_calls[0]["timeout"], (7, 11))

    def test_detection_and_profile_preserve_complete_raw_payloads(self) -> None:
        detection = detection_payload()
        profile = profile_payload()
        session = FakeSession([FakeResponse(200, detection), FakeResponse(200, profile)])
        client = self.client(session)

        detection_result = client.get_asset_detection("192.0.2.10")
        profile_result = client.get_asset_profile("192.0.2.10")

        self.assertEqual(detection_result.raw_payload, detection)
        self.assertEqual(profile_result.raw_payload, profile)
        self.assertIsNone(detection_result.raw_payload["nullable"])
        self.assertFalse(detection_result.raw_payload["falseValue"])
        self.assertEqual(detection_result.raw_payload["emptyList"], [])
        self.assertEqual(detection_result.raw_payload["emptyObject"], {})
        self.assertEqual(len(detection_result.raw_payload["matchedRules"]), 25)
        self.assertEqual(len(detection_result.raw_payload["conflicts"]), 12)
        self.assertIsNone(profile_result.raw_payload["identity"]["snmp"])
        self.assertIn("future_profile_field", profile_result.raw_payload)
        self.assertEqual(session.get_calls[0]["headers"]["Authorization"], "Bearer bootstrap-token")
        self.assertEqual(session.get_calls[1]["headers"]["Authorization"], "Bearer bootstrap-token")
        self.assertEqual(session.post_calls, [])

    def test_invalid_ip_and_unsafe_template_are_rejected_before_request(self) -> None:
        session = FakeSession([])
        client = self.client(session)
        with self.assertRaises(ProductApiError):
            client.get_asset_profile("../../admin")
        self.assertEqual(session.get_calls, [])

        unsafe = self.client(session, product_asset_profile_path="/profile/{ip}/../secrets")
        with self.assertRaises(ProductApiConfigError):
            unsafe.get_asset_profile("192.0.2.10")
        self.assertEqual(session.get_calls, [])

    def test_profile_404_is_typed_not_found_without_fake_payload(self) -> None:
        result = self.client(FakeSession([FakeResponse(404, {"message": "not found"})])).get_asset_profile(
            "192.0.2.10"
        )
        self.assertFalse(result.found)
        self.assertIsNone(result.raw_payload)
        self.assertEqual(result.status_code, 404)

    def test_401_reuses_shared_login_and_retries_once(self) -> None:
        session = FakeSession(
            [FakeResponse(401, {}), FakeResponse(200, profile_payload())],
            post_responses=[FakeResponse(200, {"accessToken": "refreshed-token"})],
        )
        result = self.client(session).get_asset_profile("192.0.2.10", request_id="req-refresh")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(session.get_calls), 2)
        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(session.post_calls[0]["url"], "http://product.invalid/auth/login")
        self.assertEqual(session.post_calls[0]["headers"]["x-hwid"], "test-hwid")
        self.assertEqual(session.post_calls[0]["headers"]["x-captcha-bypass"], "test-captcha")
        self.assertEqual(
            session.post_calls[0]["json"],
            {"username": "test-user", "password": "test-password"},
        )
        self.assertEqual(session.get_calls[1]["headers"]["Authorization"], "Bearer refreshed-token")
        self.assertEqual(session.get_calls[1]["headers"]["x-hwid"], "test-hwid")

    def test_second_401_and_other_http_failures_remain_typed(self) -> None:
        session = FakeSession(
            [FakeResponse(401, {}), FakeResponse(401, {})],
            post_responses=[FakeResponse(200, {"accessToken": "refreshed-token"})],
        )
        with self.assertRaises(ProductApiHTTPError) as unauthorized:
            self.client(session).get_asset_profile("192.0.2.10")
        self.assertEqual(unauthorized.exception.status_code, 401)
        self.assertEqual(len(session.get_calls), 2)
        self.assertEqual(len(session.post_calls), 1)

        for status_code in (403, 429, 500):
            with self.subTest(status_code=status_code):
                with self.assertRaises(ProductApiHTTPError) as error:
                    self.client(FakeSession([FakeResponse(status_code, {})])).get_asset_profile("192.0.2.10")
                self.assertEqual(error.exception.status_code, status_code)

    def test_timeout_malformed_json_and_unexpected_json_type_fail_safely(self) -> None:
        with self.assertRaisesRegex(ProductApiError, "request failed"):
            self.client(FakeSession([requests.Timeout("private timeout details")])).get_asset_profile("192.0.2.10")

        with self.assertRaisesRegex(ProductApiError, "not valid JSON"):
            self.client(FakeSession([FakeResponse(200, json_error=ValueError("bad JSON"))])).get_asset_profile(
                "192.0.2.10"
            )

        with self.assertRaisesRegex(ProductApiError, "JSON object or array"):
            self.client(FakeSession([FakeResponse(200, "not-an-object")])).get_asset_profile("192.0.2.10")

        with self.assertRaisesRegex(ProductApiError, "JSON object or array"):
            self.client(FakeSession([FakeResponse(200, None)])).get_asset_profile("192.0.2.10")

    def test_operational_logs_do_not_contain_payload_or_credentials(self) -> None:
        payload = profile_payload()
        payload["private_marker"] = "DO-NOT-LOG-PROFILE-CONTENT"
        session = FakeSession([FakeResponse(200, payload)])
        with self.assertLogs("src.core.product_client", level="INFO") as captured:
            self.client(session).get_asset_profile("192.0.2.10", request_id="req-safe-log")

        logs = "\n".join(captured.output)
        self.assertIn("event=product_asset_endpoint_validated", logs)
        for secret in ("DO-NOT-LOG-PROFILE-CONTENT", "bootstrap-token", "test-password", "test-hwid"):
            self.assertNotIn(secret, logs)


if __name__ == "__main__":
    unittest.main()
