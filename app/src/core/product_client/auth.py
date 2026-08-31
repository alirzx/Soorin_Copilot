"""Shared in-memory product authentication manager."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from src.config.settings import Settings
from src.core.product_client.errors import ProductApiConfigError, ProductApiError


logger = logging.getLogger(__name__)


def _metrics():
    from src.core.observability.metrics import get_metrics

    return get_metrics()


def _normalize_bearer_token(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("bearer "):
        return token.split(" ", 1)[1].strip()
    return token


class ProductAuthManager:
    """Thread-safe token manager shared by product-backed clients."""

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self._lock = threading.RLock()
        self._token = _normalize_bearer_token(settings.product_api_token)
        self._token_created_at = time.time() if self._token else 0.0
        self._bootstrap_invalidated = False
        self.last_auth_source = "bootstrap" if self._token else "none"
        self.last_token_refreshed = False
        self.last_retry_count = 0

    def get_token(self, *, request_id: str = "", reason: str = "request") -> str:
        with self._lock:
            self.last_token_refreshed = False
            self.last_retry_count = 0
            if self._token and not self._is_expired():
                logger.info(
                    "event=product_auth_token_reused request_id=%s reason=%s token_age_seconds=%s",
                    request_id,
                    reason,
                    self.token_age_seconds,
                )
                self.last_auth_source = "cache"
                return self._token
            if self._token and self._is_expired():
                if not self.settings.product_username or not self.settings.product_password:
                    logger.info(
                        "event=product_auth_token_reused request_id=%s reason=bootstrap_without_login_credentials token_age_seconds=%s",
                        request_id,
                        self.token_age_seconds,
                    )
                    self.last_auth_source = "bootstrap"
                    return self._token
                return self.refresh(request_id=request_id, reason="age_expired")
            if not self._bootstrap_invalidated and self.settings.product_api_token:
                self._token = _normalize_bearer_token(self.settings.product_api_token)
                self._token_created_at = time.time()
                self.last_auth_source = "bootstrap"
                logger.info(
                    "event=product_auth_token_reused request_id=%s reason=bootstrap token_age_seconds=%s",
                    request_id,
                    self.token_age_seconds,
                )
                return self._token
            return self.login(request_id=request_id, reason=reason)

    def login(self, *, request_id: str = "", reason: str = "login") -> str:
        if not self.settings.product_api_base_url:
            raise ProductApiConfigError("SOORIN_PRODUCT_API_BASE_URL is not configured.")
        if not self.settings.product_username or not self.settings.product_password:
            raise ProductApiConfigError("Product username/password are not configured.")
        if not self.settings.product_hwid:
            raise ProductApiConfigError("SOORIN_PRODUCT_HWID is not configured.")

        endpoint_path = self.settings.product_login_path
        if not endpoint_path.startswith("/"):
            endpoint_path = f"/{endpoint_path}"
        url = f"{self.settings.product_api_base_url}{endpoint_path}"
        headers = {
            "x-hwid": self.settings.product_hwid,
            "x-captcha-bypass": self.settings.product_captcha_bypass,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        started = time.perf_counter()
        logger.info("event=product_auth_login_started request_id=%s reason=%s", request_id, reason)
        try:
            response = self.session.post(
                url,
                headers=headers,
                json={
                    "username": self.settings.product_username,
                    "password": self.settings.product_password,
                },
                timeout=(self.settings.product_connect_timeout_seconds, self.settings.product_read_timeout_seconds),
            )
        except requests.RequestException as exc:
            elapsed = time.perf_counter() - started
            latency_ms = int(elapsed * 1000)
            _metrics().observe_product("login", duration_seconds=elapsed)
            logger.warning(
                "event=product_auth_failed request_id=%s reason=request_exception latency_ms=%s",
                request_id,
                latency_ms,
            )
            raise ProductApiError("Product authentication request failed.") from exc

        elapsed = time.perf_counter() - started
        latency_ms = int(elapsed * 1000)
        _metrics().observe_product(
            "login",
            duration_seconds=elapsed,
            status_code=response.status_code,
        )
        if response.status_code >= 400:
            logger.warning(
                "event=product_auth_failed request_id=%s reason=http_error status_code=%s latency_ms=%s",
                request_id,
                response.status_code,
                latency_ms,
            )
            raise ProductApiError(f"Product authentication failed with HTTP {response.status_code}.")

        try:
            payload: Any = response.json()
        except ValueError as exc:
            logger.warning(
                "event=product_auth_failed request_id=%s reason=invalid_json status_code=%s latency_ms=%s",
                request_id,
                response.status_code,
                latency_ms,
            )
            raise ProductApiError("Product authentication response was not valid JSON.") from exc
        token = _normalize_bearer_token(str(payload.get("accessToken") or ""))
        if not token:
            logger.warning(
                "event=product_auth_failed request_id=%s reason=missing_access_token status_code=%s latency_ms=%s",
                request_id,
                response.status_code,
                latency_ms,
            )
            raise ProductApiError("Product authentication response did not contain accessToken.")

        self._token = token
        self._token_created_at = time.time()
        self._bootstrap_invalidated = False
        self.last_auth_source = "login"
        self.last_token_refreshed = True
        logger.info(
            "event=product_auth_login_succeeded request_id=%s reason=%s status_code=%s latency_ms=%s token_age_seconds=%s",
            request_id,
            reason,
            response.status_code,
            latency_ms,
            self.token_age_seconds,
        )
        return token

    def refresh(self, *, request_id: str = "", reason: str = "refresh") -> str:
        logger.info(
            "event=product_auth_refresh_started request_id=%s reason=%s token_age_seconds=%s",
            request_id,
            reason,
            self.token_age_seconds,
        )
        token = self.login(request_id=request_id, reason=reason)
        logger.info(
            "event=product_auth_refresh_succeeded request_id=%s reason=%s token_age_seconds=%s",
            request_id,
            reason,
            self.token_age_seconds,
        )
        return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = ""
            self._token_created_at = 0.0
            self._bootstrap_invalidated = True

    @property
    def token_age_seconds(self) -> int:
        if not self._token or not self._token_created_at:
            return 0
        return max(0, int(time.time() - self._token_created_at))

    def _is_expired(self) -> bool:
        if not self._token:
            return True
        return self.token_age_seconds >= max(1, self.settings.product_token_refresh_seconds)
