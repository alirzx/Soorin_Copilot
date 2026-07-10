"""Authenticated HTTP client for Soorin product API endpoints."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config.settings import Settings
from src.core.product_client.errors import ProductApiConfigError, ProductApiError, ProductApiHTTPError
from src.core.product_client.schemas import ProductTopologyResponse


logger = logging.getLogger(__name__)

def _normalize_bearer_token(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("bearer "):
        return token.split(" ", 1)[1].strip()
    return token


class ProductApiClient:
    """Authenticated product API boundary.

    The bearer token and HWID prove access to the product API. They must never be
    logged. Future product endpoint methods should call `get_json()`.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.product_api_base_url
        self.connect_timeout = settings.product_connect_timeout_seconds
        self.read_timeout = settings.product_read_timeout_seconds
        self.session = requests.Session()

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
            "event=product_client_initialized base_url_configured=%s hwid_present=%s connect_timeout=%s read_timeout=%s max_retries=%s",
            bool(settings.product_api_base_url),
            bool(settings.product_hwid),
            self.connect_timeout,
            self.read_timeout,
            settings.product_max_retries,
        )

    def _headers(self) -> dict[str, str]:
        token = _normalize_bearer_token(self.settings.product_api_token)
        if not self.base_url:
            raise ProductApiConfigError("SOORIN_PRODUCT_API_BASE_URL is not configured.")
        if not token:
            raise ProductApiConfigError("SOORIN_PRODUCT_API_TOKEN is not configured.")
        if not self.settings.product_hwid:
            raise ProductApiConfigError("SOORIN_PRODUCT_HWID is not configured.")
        return {
            "Authorization": f"Bearer {token}",
            "x-hwid": self.settings.product_hwid,
            "Accept": "application/json",
        }

    def get_json(self, endpoint_path: str) -> tuple[Any, int, float]:
        """Fetch JSON from a product endpoint without logging sensitive data."""
        if not endpoint_path.startswith("/"):
            endpoint_path = f"/{endpoint_path}"
        url = f"{self.base_url}{endpoint_path}"

        logger.info("event=product_request_started endpoint_path=%s", endpoint_path)
        started = time.perf_counter()
        try:
            response = self.session.get(
                url,
                headers=self._headers(),
                timeout=(self.connect_timeout, self.read_timeout),
            )
        except requests.RequestException as exc:
            logger.exception("event=product_request_exception endpoint_path=%s", endpoint_path)
            raise ProductApiError("Product API request failed.") from exc

        elapsed = time.perf_counter() - started
        logger.info(
            "event=product_response_received endpoint_path=%s status_code=%s elapsed_ms=%s",
            endpoint_path,
            response.status_code,
            int(elapsed * 1000),
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
