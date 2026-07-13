"""Focused tests for asset-detection product client and normalization."""

from __future__ import annotations

import unittest
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import requests

from src.config.settings import get_settings
from src.core.detection import adapt_asset_detection, compact_full, summary
from src.core.detection.models import RawAssetDetectionResponse
from src.core.product_client import ProductApiClient, ProductAuthManager
from src.core.product_client.errors import ProductApiError, ProductApiHTTPError


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(
        self,
        responses: list[FakeResponse | Exception],
        *,
        post_responses: list[FakeResponse | Exception] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.post_responses = list(post_responses or [])
        self.calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    def mount(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str], timeout: tuple[int, int]) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: tuple[int, int]) -> FakeResponse:
        self.post_calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        response = self.post_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_settings(**overrides: Any):
    values = {
        "product_api_base_url": "http://product.local",
        "product_asset_detection_path": "/asset-detection/test/{ip}",
        "product_login_path": "/auth/login",
        "product_api_token": "Bearer secret-token",
        "product_username": "admin",
        "product_password": "secret-password",
        "product_captcha_bypass": "captcha-secret",
        "product_token_refresh_seconds": 600,
        "product_hwid": "secret-hwid",
        "product_connect_timeout_seconds": 7,
        "product_read_timeout_seconds": 11,
        "product_max_retries": 1,
        "product_retry_backoff_seconds": 0.0,
    }
    values.update(overrides)
    return replace(
        get_settings(),
        **values,
    )


def sample_payload() -> dict[str, Any]:
    return {
        "ip": "192.168.21.1",
        "assetFound": True,
        "storedTag": "Endpoint",
        "storedSubTag": "Workstation",
        "detection": {
            "primaryRole": "Domain Joined Workstation",
            "confidence": 0.91,
            "topRoles": [{"role": "Domain Joined Workstation"}, {"role": "Windows Host"}],
            "vendor": "Microsoft",
            "product": "Windows 10",
        },
        "tagging": {
            "tag": "Endpoint",
            "subTag": "Windows",
            "confidence": 0.88,
            "inferredDeviceType": "Network Device",
        },
        "matchedRules": [
            {
                "id": "r1",
                "code": "windows_identity",
                "name": "Windows identity",
                "confidence": 0.9,
                "evidence": ["os_is_windows=true", "os_is_windows=true", "kerberos_server=false"],
            }
        ],
        "signals": {
            "extended": {
                "os_is_linux": False,
                "os_is_windows": True,
                "is_domain_controller": False,
                "kerberos_server": False,
                "serves_smb_sessions": False,
                "tls_server_sessions": 0,
                "dns_query_count": 0,
                "outbound_ratio_pct": 99.5,
                "dhcp_is_printer": False,
                "printer_product": "Printer",
                "external_peer_count": 0,
            },
            "normalized": {
                "outbound_ratio_pct": 100,
                "primary_role": "Domain Joined Workstation",
                "vendor": "Microsoft",
                "product": "",
                "nullable_hint": None,
            },
        },
        "futureBackendField": {"kept": True},
    }


class ProductAssetDetectionClientTests(unittest.TestCase):
    def client(self, session: FakeSession) -> ProductApiClient:
        client = ProductApiClient(make_settings())
        client.session = session  # type: ignore[assignment]
        return client

    def test_successful_request_construction_and_headers_are_not_logged(self) -> None:
        session = FakeSession([FakeResponse(200, sample_payload())])
        client = self.client(session)
        with self.assertLogs("src.core.product_client.client", level="INFO") as logs:
            response = client.get_asset_detection("192.168.21.1", request_id="req-1")

        self.assertEqual(response.ip, "192.168.21.1")
        self.assertEqual(session.calls[0]["url"], "http://product.local/asset-detection/test/192.168.21.1")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(session.calls[0]["headers"]["x-hwid"], "secret-hwid")
        self.assertEqual(session.calls[0]["timeout"], (7, 11))
        log_text = "\n".join(logs.output)
        self.assertIn("endpoint_path=/asset-detection/test/192.168.21.1", log_text)
        self.assertNotIn("secret-token", log_text)
        self.assertNotIn("secret-hwid", log_text)
        self.assertNotIn("Authorization", log_text)

    def test_login_request_uses_expected_headers_and_body_without_logging_secrets(self) -> None:
        session = FakeSession([], post_responses=[FakeResponse(200, {"accessToken": "login-token"})])
        manager = ProductAuthManager(make_settings(product_api_token=""), session)  # type: ignore[arg-type]
        with self.assertLogs("src.core.product_client.auth", level="INFO") as logs:
            token = manager.get_token(request_id="auth-1")
        self.assertEqual(token, "login-token")
        self.assertEqual(session.post_calls[0]["url"], "http://product.local/auth/login")
        self.assertEqual(session.post_calls[0]["headers"]["x-hwid"], "secret-hwid")
        self.assertEqual(session.post_calls[0]["headers"]["x-captcha-bypass"], "captcha-secret")
        self.assertEqual(session.post_calls[0]["json"], {"username": "admin", "password": "secret-password"})
        log_text = "\n".join(logs.output)
        self.assertIn("product_auth_login_started", log_text)
        self.assertIn("product_auth_login_succeeded", log_text)
        self.assertNotIn("login-token", log_text)
        self.assertNotIn("secret-password", log_text)
        self.assertNotIn("secret-hwid", log_text)
        self.assertNotIn("captcha-secret", log_text)

    def test_token_reuse_before_expiry_and_refresh_after_configured_age(self) -> None:
        session = FakeSession(
            [],
            post_responses=[
                FakeResponse(200, {"accessToken": "token-1"}),
                FakeResponse(200, {"accessToken": "token-2"}),
            ],
        )
        manager = ProductAuthManager(make_settings(product_api_token="", product_token_refresh_seconds=1), session)  # type: ignore[arg-type]
        self.assertEqual(manager.get_token(request_id="auth-2"), "token-1")
        self.assertEqual(manager.get_token(request_id="auth-3"), "token-1")
        manager._token_created_at = time.time() - 5
        self.assertEqual(manager.get_token(request_id="auth-4"), "token-2")
        self.assertEqual(len(session.post_calls), 2)

    def test_401_invalidates_token_logins_once_and_retries_original_request(self) -> None:
        session = FakeSession(
            [FakeResponse(401, {"error": "expired"}), FakeResponse(200, sample_payload())],
            post_responses=[
                FakeResponse(200, {"accessToken": "token-1"}),
                FakeResponse(200, {"accessToken": "token-2"}),
            ],
        )
        client = ProductApiClient(make_settings(product_api_token=""))
        client.session = session  # type: ignore[assignment]
        client.auth_manager = ProductAuthManager(make_settings(product_api_token=""), session)  # type: ignore[assignment]
        response = client.get_asset_detection("192.168.21.1", request_id="auth-5")
        self.assertEqual(response.ip, "192.168.21.1")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(session.post_calls), 2)
        self.assertEqual(client.last_auth_retry_count, 1)

    def test_second_401_raises_typed_unauthorized_error(self) -> None:
        session = FakeSession(
            [FakeResponse(401, {"error": "expired"}), FakeResponse(401, {"error": "expired"})],
            post_responses=[
                FakeResponse(200, {"accessToken": "token-1"}),
                FakeResponse(200, {"accessToken": "token-2"}),
            ],
        )
        client = ProductApiClient(make_settings(product_api_token=""))
        client.session = session  # type: ignore[assignment]
        client.auth_manager = ProductAuthManager(make_settings(product_api_token=""), session)  # type: ignore[assignment]
        with self.assertRaises(ProductApiHTTPError) as caught:
            client.get_asset_detection("192.168.21.1", request_id="auth-6")
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(session.post_calls), 2)

    def test_graph_and_detection_clients_can_share_one_token_manager(self) -> None:
        auth_session = FakeSession([], post_responses=[FakeResponse(200, {"accessToken": "shared-token"})])
        manager = ProductAuthManager(make_settings(product_api_token=""), auth_session)  # type: ignore[arg-type]
        graph_session = FakeSession([FakeResponse(200, [{"src_ip": "192.168.1.1", "dst_ip": "192.168.1.2"}])])
        detection_session = FakeSession([FakeResponse(200, sample_payload())])
        graph_client = ProductApiClient(make_settings(product_api_token=""), manager)
        detection_client = ProductApiClient(make_settings(product_api_token=""), manager)
        graph_client.session = graph_session  # type: ignore[assignment]
        detection_client.session = detection_session  # type: ignore[assignment]
        graph_client.fetch_topology_unique_ip_pairs()
        detection_client.get_asset_detection("192.168.21.1")
        self.assertEqual(len(auth_session.post_calls), 1)
        self.assertEqual(graph_session.calls[0]["headers"]["Authorization"], "Bearer shared-token")
        self.assertEqual(detection_session.calls[0]["headers"]["Authorization"], "Bearer shared-token")

    def test_invalid_ip_is_rejected_before_http_call(self) -> None:
        session = FakeSession([FakeResponse(200, sample_payload())])
        client = self.client(session)
        with self.assertRaises(ProductApiError):
            client.get_asset_detection("999.999.999.999")
        self.assertEqual(session.calls, [])

    def test_non_retryable_4xx_raises_typed_http_error(self) -> None:
        session = FakeSession([FakeResponse(404, {"error": "missing"})])
        client = self.client(session)
        with self.assertRaises(ProductApiHTTPError) as caught:
            client.get_asset_detection("192.168.21.1")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(len(session.calls), 1)

    def test_retryable_server_and_network_errors_raise_typed_errors(self) -> None:
        server = self.client(FakeSession([FakeResponse(500, {"error": "server"})]))
        with self.assertRaises(ProductApiHTTPError) as server_error:
            server.get_asset_detection("192.168.21.1")
        self.assertEqual(server_error.exception.status_code, 500)

        network = self.client(FakeSession([requests.ConnectionError("network down")]))
        with self.assertRaises(ProductApiError):
            network.get_asset_detection("192.168.21.1")


class AssetDetectionNormalizationTests(unittest.TestCase):
    def evidence(self):
        raw = RawAssetDetectionResponse.model_validate(sample_payload())
        return adapt_asset_detection(raw, fetched_at=datetime(2026, 7, 12, tzinfo=timezone.utc))

    def test_raw_schema_accepts_real_shape_and_future_additions(self) -> None:
        raw = RawAssetDetectionResponse.model_validate(sample_payload())
        self.assertTrue(raw.asset_found)
        self.assertEqual(raw.stored_tag, "Endpoint")
        self.assertEqual(raw.stored_sub_tag, "Workstation")
        self.assertEqual(len(raw.matched_rules), 1)
        self.assertIsNotNone(raw.signals)
        self.assertEqual(raw.model_extra["futureBackendField"], {"kept": True})

    def test_preserves_false_zero_null_empty_and_missing_distinctions(self) -> None:
        evidence = self.evidence()
        self.assertIs(evidence.signals.extended["os_is_linux"], False)
        self.assertIs(evidence.signals.extended["is_domain_controller"], False)
        self.assertEqual(evidence.signals.extended["tls_server_sessions"], 0)
        self.assertEqual(evidence.signals.extended["dns_query_count"], 0)
        self.assertIsNone(evidence.signals.normalized["nullable_hint"])
        self.assertEqual(evidence.signals.normalized["product"], "")
        self.assertNotIn("missing_protocol_section", evidence.signals.normalized)

    def test_duplicate_evidence_removed_and_precise_metric_preferred(self) -> None:
        evidence = self.evidence()
        self.assertEqual(evidence.matched_rules[0].evidence.count("os_is_windows=true"), 1)
        self.assertEqual(evidence.signals.metrics["outbound_ratio_pct"], 99.5)
        self.assertEqual(evidence.signals.normalized["outbound_ratio_pct"], 100)

    def test_conflicts_are_structured(self) -> None:
        evidence = self.evidence()
        codes = {conflict.code for conflict in evidence.conflicts}
        self.assertIn("role_device_type_conflict", codes)
        self.assertIn("printer_hint_conflict", codes)
        self.assertIn("rounded_metric_mismatch", codes)
        for conflict in evidence.conflicts:
            self.assertTrue(conflict.severity)
            self.assertTrue(conflict.explanation)

    def test_summary_and_compact_full_are_bounded_and_include_negatives(self) -> None:
        evidence = self.evidence()
        short = summary(evidence)
        full = compact_full(evidence)
        self.assertIn("Asset found: True", short)
        self.assertIn("Primary role: Domain Joined Workstation", short)
        self.assertIn("kerberos_server=false", full)
        self.assertIn("tls_server_sessions=0", full)
        self.assertLess(len(short), 2200)
        self.assertLess(len(full), 5200)

    def test_null_and_missing_sections_are_separate(self) -> None:
        raw = RawAssetDetectionResponse.model_validate(
            {
                "ip": "192.168.21.1",
                "assetFound": False,
                "detection": None,
                "signals": {"extended": None},
            }
        )
        evidence = adapt_asset_detection(raw, fetched_at=datetime(2026, 7, 12, tzinfo=timezone.utc))
        self.assertIn("detection", evidence.signals.null_sections)
        self.assertIn("signals.extended", evidence.signals.null_sections)
        self.assertIn("tagging", evidence.signals.missing_sections)
        self.assertIn("signals.normalized", evidence.signals.missing_sections)


if __name__ == "__main__":
    unittest.main()
