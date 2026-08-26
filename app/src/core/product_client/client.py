"""Authenticated HTTP client for Soorin product API endpoints."""

from __future__ import annotations

import logging
import ipaddress
import time
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config.settings import Settings
from src.core.product_client.auth import ProductAuthManager
from src.core.product_client.errors import ProductApiConfigError, ProductApiError, ProductApiHTTPError
from src.core.product_client.schemas import ProductAssetResponse, ProductTopologyResponse


logger = logging.getLogger(__name__)


def _metrics():
    from src.core.observability.metrics import get_metrics

    return get_metrics()


class ProductApiClient:
    """Authenticated product API boundary.

    The bearer token and HWID prove access to the product API. They must never be
    logged. Future product endpoint methods should call `get_json()`.
    """

    def __init__(self, settings: Settings, auth_manager: ProductAuthManager | None = None) -> None:
        self.settings = settings
        self.base_url = settings.product_api_base_url
        self.connect_timeout = settings.product_connect_timeout_seconds
        self.read_timeout = settings.product_read_timeout_seconds
        self.session = requests.Session()
        self.auth_manager = auth_manager or ProductAuthManager(settings, self.session)
        self.last_auth_source = "none"
        self.last_token_refreshed = False
        self.last_auth_retry_count = 0

        retry_strategy = Retry(
            total=settings.product_max_retries,
            backoff_factor=settings.product_retry_backoff_seconds,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        logger.info(
            "event=product_client_initialized base_url_configured=%s hwid_present=%s username_present=%s password_present=%s captcha_bypass_present=%s connect_timeout=%s read_timeout=%s max_retries=%s",
            bool(settings.product_api_base_url),
            bool(settings.product_hwid),
            bool(settings.product_username),
            bool(settings.product_password),
            bool(settings.product_captcha_bypass),
            self.connect_timeout,
            self.read_timeout,
            settings.product_max_retries,
        )

    def _headers(self, *, request_id: str = "", reason: str = "request") -> dict[str, str]:
        if not self.base_url:
            raise ProductApiConfigError("SOORIN_PRODUCT_API_BASE_URL is not configured.")
        if not self.settings.product_hwid:
            raise ProductApiConfigError("SOORIN_PRODUCT_HWID is not configured.")
        token = self.auth_manager.get_token(request_id=request_id, reason=reason)
        self.last_auth_source = self.auth_manager.last_auth_source
        self.last_token_refreshed = self.auth_manager.last_token_refreshed
        return {
            "Authorization": f"Bearer {token}",
            "x-hwid": self.settings.product_hwid,
            "Accept": "application/json",
        }

    def _resolve_url(self, endpoint_or_url: str) -> tuple[str, str]:
        if endpoint_or_url.startswith(("http://", "https://")):
            parsed = urlsplit(endpoint_or_url)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            return endpoint_or_url, path
        endpoint_path = endpoint_or_url
        if not endpoint_path.startswith("/"):
            endpoint_path = f"/{endpoint_path}"
        return f"{self.base_url}{endpoint_path}", endpoint_path

    def _request_json(
        self,
        method: str,
        endpoint_or_url: str,
        *,
        request_id: str = "",
        json_body: Any | None = None,
        extra_headers: dict[str, str] | None = None,
        operation: str = "other",
    ) -> tuple[Any, int, float]:
        method = method.upper()
        url, endpoint_path = self._resolve_url(endpoint_or_url)
        logger.info(
            "event=product_request_started request_id=%s method=%s endpoint_path=%s",
            request_id,
            method,
            endpoint_path,
        )
        started = time.perf_counter()
        response = None
        self.last_auth_retry_count = 0
        for attempt in range(2):
            try:
                headers = self._headers(
                    request_id=request_id,
                    reason="retry_after_401" if attempt else "request",
                )
                if extra_headers:
                    headers.update(extra_headers)
                request_kwargs: dict[str, Any] = {
                    "headers": headers,
                    "timeout": (self.connect_timeout, self.read_timeout),
                }
                if json_body is not None:
                    request_kwargs["json"] = json_body
                response = self.session.request(method, url, **request_kwargs)
            except requests.RequestException as exc:
                _metrics().observe_product(
                    operation,
                    duration_seconds=time.perf_counter() - started,
                )
                logger.warning(
                    "event=product_request_exception request_id=%s method=%s endpoint_path=%s error_type=%s",
                    request_id,
                    method,
                    endpoint_path,
                    type(exc).__name__,
                )
                raise ProductApiError("Product API request failed.") from exc

            if response.status_code != 401 or attempt == 1:
                break

            self.last_auth_retry_count = 1
            logger.info(
                "event=product_auth_retry_after_401 request_id=%s reason=unauthorized status_code=%s token_age_seconds=%s retry_count=%s method=%s endpoint_path=%s",
                request_id,
                response.status_code,
                self.auth_manager.token_age_seconds,
                self.last_auth_retry_count,
                method,
                endpoint_path,
            )
            self.auth_manager.invalidate()

        if response is None:
            raise ProductApiError("Product API request did not produce a response.")

        elapsed = time.perf_counter() - started
        _metrics().observe_product(
            operation,
            duration_seconds=elapsed,
            status_code=response.status_code,
        )
        logger.info(
            "event=product_response_received request_id=%s method=%s endpoint_path=%s status_code=%s elapsed_ms=%s auth_source=%s token_refreshed=%s auth_retry_count=%s",
            request_id,
            method,
            endpoint_path,
            response.status_code,
            int(elapsed * 1000),
            self.last_auth_source,
            self.last_token_refreshed,
            self.last_auth_retry_count,
        )

        if response.status_code == 401:
            raise ProductApiHTTPError(
                "Product API request was unauthorized. Check token and HWID.",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise ProductApiHTTPError(
                f"Product API request failed with HTTP {response.status_code}.",
                status_code=response.status_code,
            )

        try:
            return response.json(), response.status_code, elapsed
        except ValueError as exc:
            raise ProductApiError("Product API response was not valid JSON.") from exc

    def get_json(
        self,
        endpoint_path: str,
        *,
        request_id: str = "",
        operation: str = "other",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[Any, int, float]:
        """Fetch JSON from a product endpoint without logging sensitive data."""
        return self._request_json(
            "GET",
            endpoint_path,
            request_id=request_id,
            operation=operation,
            extra_headers=extra_headers,
        )

    def post_json(
        self,
        endpoint_or_url: str,
        payload: dict[str, Any],
        *,
        request_id: str = "",
        idempotency_key: str = "",
        operation: str = "other",
    ) -> tuple[Any, int, float]:
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self._request_json(
            "POST",
            endpoint_or_url,
            request_id=request_id,
            json_body=payload,
            extra_headers=headers,
            operation=operation,
        )

    def fetch_topology_unique_ip_pairs(self) -> ProductTopologyResponse:
        """Fetch currently supported topology unique communication pairs."""
        payload, status_code, elapsed = self.get_json(
            self.settings.product_topology_path,
            operation="topology",
        )
        return ProductTopologyResponse.from_payload(
            payload,
            endpoint_path=self.settings.product_topology_path,
            status_code=status_code,
            elapsed_seconds=elapsed,
        )

    def _asset_endpoint_path(self, template: str, ip: str, *, endpoint_name: str, request_id: str) -> tuple[str, str]:
        """Validate one IPv4 target before substituting it into a configured path."""
        try:
            normalized_ip = str(ipaddress.ip_address(str(ip).strip()))
        except ValueError as exc:
            logger.info("event=product_asset_endpoint_invalid_ip request_id=%s endpoint_name=%s", request_id, endpoint_name)
            raise ProductApiError(f"Invalid IP address for {endpoint_name}.") from exc
        if template.count("{ip}") != 1 or ".." in template:
            raise ProductApiConfigError(f"Configured {endpoint_name} path must contain exactly one safe {{ip}} placeholder.")
        safe_ip = quote(normalized_ip, safe="")
        return normalized_ip, template.format(ip=safe_ip)

    def _get_asset_json(
        self,
        ip: str,
        template: str,
        *,
        endpoint_name: str,
        request_id: str = "",
    ) -> ProductAssetResponse:
        normalized_ip, endpoint_path = self._asset_endpoint_path(
            template,
            ip,
            endpoint_name=endpoint_name,
            request_id=request_id,
        )

        try:
            payload, status_code, elapsed = self.get_json(
                endpoint_path,
                request_id=request_id,
                operation="detection" if endpoint_name.startswith("asset_detection") else "profile",
            )
        except ProductApiHTTPError as exc:
            if exc.status_code != 404:
                raise
            logger.info(
                "event=product_asset_endpoint_not_found request_id=%s endpoint_name=%s endpoint_path=%s ip=%s status_code=404",
                request_id,
                endpoint_name,
                endpoint_path,
                normalized_ip,
            )
            return ProductAssetResponse(
                target_ip=normalized_ip,
                raw_payload=None,
                endpoint_path=endpoint_path,
                status_code=404,
                elapsed_seconds=0.0,
                found=False,
            )
        if not isinstance(payload, (dict, list)):
            raise ProductApiError(f"Product {endpoint_name} response must be a JSON object or array.")
        found = payload.get("assetFound") if isinstance(payload, dict) else True
        if not isinstance(found, bool):
            found = True
        logger.info(
            "event=product_asset_endpoint_validated request_id=%s endpoint_name=%s endpoint_path=%s status_code=%s elapsed_ms=%s asset_found=%s top_level_type=%s top_level_key_count=%s",
            request_id,
            endpoint_name,
            endpoint_path,
            status_code,
            int(elapsed * 1000),
            found,
            type(payload).__name__,
            len(payload),
        )
        return ProductAssetResponse(
            target_ip=normalized_ip,
            raw_payload=payload,
            endpoint_path=endpoint_path,
            status_code=status_code,
            elapsed_seconds=elapsed,
            found=found,
        )

    def get_asset_detection(
        self,
        ip: str,
        *,
        request_id: str = "",
        view: str = "full",
    ) -> ProductAssetResponse:
        """Fetch one approved Detection view through the shared authenticated client."""
        paths = {
            "full": self.settings.product_asset_detection_path,
            "overview": self.settings.product_asset_detection_overview_path,
            "evidence": self.settings.product_asset_detection_evidence_path,
            "similarity": self.settings.product_asset_detection_similarity_path,
            "cluster": self.settings.product_asset_detection_cluster_path,
        }
        if view not in paths:
            raise ProductApiConfigError("Unsupported Product asset-detection view.")
        return self._get_asset_json(
            ip,
            paths[view],
            endpoint_name=f"asset_detection_{view}",
            request_id=request_id,
        )

    def get_asset_profile(self, ip: str, *, request_id: str = "") -> ProductAssetResponse:
        """Fetch one complete Asset Profile JSON payload through shared authentication."""
        return self._get_asset_json(
            ip,
            self.settings.product_asset_profile_path,
            endpoint_name="asset_profile",
            request_id=request_id,
        )
