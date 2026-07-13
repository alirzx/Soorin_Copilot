"""Authenticated HTTP client for Soorin product API endpoints."""

from __future__ import annotations

import logging
import ipaddress
import time
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config.settings import Settings
from src.core.detection.models import RawAssetDetectionResponse
from src.core.product_client.auth import ProductAuthManager
from src.core.product_client.errors import ProductApiConfigError, ProductApiError, ProductApiHTTPError
from src.core.product_client.schemas import ProductTopologyResponse


logger = logging.getLogger(__name__)


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

    def get_json(self, endpoint_path: str, *, request_id: str = "") -> tuple[Any, int, float]:
        """Fetch JSON from a product endpoint without logging sensitive data."""
        if not endpoint_path.startswith("/"):
            endpoint_path = f"/{endpoint_path}"
        url = f"{self.base_url}{endpoint_path}"

        logger.info("event=product_request_started request_id=%s endpoint_path=%s", request_id, endpoint_path)
        started = time.perf_counter()
        response = None
        self.last_auth_retry_count = 0
        for attempt in range(2):
            try:
                response = self.session.get(
                    url,
                    headers=self._headers(
                        request_id=request_id,
                        reason="retry_after_401" if attempt else "request",
                    ),
                    timeout=(self.connect_timeout, self.read_timeout),
                )
            except requests.RequestException as exc:
                logger.exception("event=product_request_exception request_id=%s endpoint_path=%s", request_id, endpoint_path)
                raise ProductApiError("Product API request failed.") from exc

            if response.status_code != 401 or attempt == 1:
                break

            self.last_auth_retry_count = 1
            logger.info(
                "event=product_auth_retry_after_401 request_id=%s reason=unauthorized status_code=%s token_age_seconds=%s retry_count=%s",
                request_id,
                response.status_code,
                self.auth_manager.token_age_seconds,
                self.last_auth_retry_count,
            )
            self.auth_manager.invalidate()

        if response is None:
            raise ProductApiError("Product API request did not produce a response.")

        elapsed = time.perf_counter() - started
        logger.info(
            "event=product_response_received request_id=%s endpoint_path=%s status_code=%s elapsed_ms=%s auth_source=%s token_refreshed=%s auth_retry_count=%s",
            request_id,
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

    def fetch_topology_unique_ip_pairs(self) -> ProductTopologyResponse:
        """Fetch currently supported topology unique communication pairs."""
        payload, status_code, elapsed = self.get_json(self.settings.product_topology_path)
        return ProductTopologyResponse.from_payload(
            payload,
            endpoint_path=self.settings.product_topology_path,
            status_code=status_code,
            elapsed_seconds=elapsed,
        )

    def get_asset_detection(self, ip: str, *, request_id: str = "") -> RawAssetDetectionResponse:
        """Fetch and validate raw asset-detection evidence for one IP address."""
        try:
            normalized_ip = str(ipaddress.ip_address(str(ip).strip()))
        except ValueError as exc:
            logger.info("event=product_asset_detection_invalid_ip request_id=%s", request_id)
            raise ProductApiError("Invalid IP address for asset detection.") from exc

        safe_ip = quote(normalized_ip, safe="")
        endpoint_path = self.settings.product_asset_detection_path.format(ip=safe_ip)
        payload, status_code, elapsed = self.get_json(endpoint_path, request_id=request_id)
        try:
            response = RawAssetDetectionResponse.model_validate(payload)
        except ValueError as exc:
            logger.info(
                "event=product_asset_detection_validation_failed request_id=%s endpoint_path=%s status_code=%s elapsed_ms=%s",
                request_id,
                endpoint_path,
                status_code,
                int(elapsed * 1000),
            )
            raise ProductApiError("Product asset-detection response failed validation.") from exc

        signal_sections = []
        if response.signals:
            if response.signals.extended is not None:
                signal_sections.append("extended")
            if response.signals.normalized is not None:
                signal_sections.append("normalized")
        logger.info(
            "event=product_asset_detection_validated request_id=%s endpoint_path=%s status_code=%s elapsed_ms=%s asset_found=%s matched_rules=%s signal_sections=%s",
            request_id,
            endpoint_path,
            status_code,
            int(elapsed * 1000),
            response.asset_found,
            len(response.matched_rules),
            ",".join(signal_sections),
        )
        return response
