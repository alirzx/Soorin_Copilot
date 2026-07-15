"""Arvan/OpenAI-compatible chat completions provider."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from urllib3.exceptions import ProtocolError

from src.config.settings import Settings
from src.core.context.models import approx_tokens
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult


logger = logging.getLogger(__name__)

RETRYABLE_HTTP_STATUS_CODES = {429, 502, 503, 504}
RETRYABLE_REQUEST_EXCEPTIONS = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    ProtocolError,
)
PROVIDER_REQUEST_EXCEPTIONS = (requests.exceptions.RequestException, ProtocolError)


class ArvanProvider:
    provider_name = "arvan"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        logger.info(
            "event=arvan_provider_initialized provider=%s model=%s chat_path=%s",
            self.provider_name,
            settings.arvan_model,
            settings.arvan_chat_path,
        )

    @property
    def endpoint(self) -> str:
        path = self.settings.arvan_chat_path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.settings.arvan_base_url}{path}"

    def health(self) -> dict[str, object]:
        missing = []
        if not self.settings.llm_enabled:
            missing.append("llm_disabled")
        if not self.settings.arvan_base_url:
            missing.append("missing_base_url")
        if not self.settings.arvan_api_key:
            missing.append("missing_api_key")

        return {
            "enabled": self.settings.llm_enabled,
            "ready": not missing,
            "provider": self.provider_name,
            "model": self.settings.arvan_model,
            "chat_path": self.settings.arvan_chat_path,
            "missing": missing,
        }

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        request_id: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        timeout_seconds: int | None = None,
        purpose: str = "chat",
    ) -> LLMProviderResult:
        readiness = self.health()
        if not readiness["ready"]:
            raise LLMError("Arvan provider is not configured.", reason="provider_not_ready")

        payload = {
            "model": self.settings.arvan_model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else self.settings.arvan_max_tokens,
            "temperature": temperature if temperature is not None else self.settings.arvan_temperature,
            "top_p": top_p if top_p is not None else self.settings.arvan_top_p,
        }
        headers = {
            "Authorization": f"{self.settings.arvan_auth_scheme} {self.settings.arvan_api_key}",
            "Content-Type": "application/json",
        }
        timeout = (
            self.settings.llm_connect_timeout_seconds,
            timeout_seconds if timeout_seconds is not None else self.settings.chat_timeout_seconds,
        )

        logger.info(
            "event=provider_request_start request_id=%s provider=%s model=%s message_count=%s chat_path=%s purpose=%s connect_timeout_seconds=%s read_timeout_seconds=%s",
            request_id,
            self.provider_name,
            self.settings.arvan_model,
            len(messages),
            self.settings.arvan_chat_path,
            purpose,
            timeout[0],
            timeout[1],
        )
        started = time.perf_counter()
        try:
            response = requests.post(self.endpoint, json=payload, headers=headers, timeout=timeout)
        except PROVIDER_REQUEST_EXCEPTIONS as exc:
            retryable = isinstance(exc, RETRYABLE_REQUEST_EXCEPTIONS)
            logger.warning(
                "event=provider_request_exception request_id=%s provider=%s model=%s purpose=%s error_type=%s retryable=%s",
                request_id,
                self.provider_name,
                self.settings.arvan_model,
                purpose,
                type(exc).__name__,
                retryable,
            )
            raise LLMError(
                "Arvan request failed.",
                reason="provider_transport_error",
                details={"error_type": type(exc).__name__, "retryable": retryable},
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=provider_response request_id=%s provider=%s model=%s status_code=%s latency_ms=%s purpose=%s",
            request_id,
            self.provider_name,
            self.settings.arvan_model,
            response.status_code,
            latency_ms,
            purpose,
        )
        if response.status_code >= 400:
            logger.warning(
                "event=provider_http_error request_id=%s provider=%s model=%s status_code=%s latency_ms=%s retryable=%s",
                request_id,
                self.provider_name,
                self.settings.arvan_model,
                response.status_code,
                latency_ms,
                response.status_code in RETRYABLE_HTTP_STATUS_CODES,
            )
            retryable = response.status_code in RETRYABLE_HTTP_STATUS_CODES
            raise LLMError(
                "Arvan provider returned an error.",
                reason="provider_http_error",
                details={
                    "status_code": response.status_code,
                    "error_type": f"HTTP_{response.status_code}",
                    "retryable": retryable,
                },
            )

        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            logger.exception(
                "event=provider_invalid_json request_id=%s provider=%s model=%s status_code=%s latency_ms=%s",
                request_id,
                self.provider_name,
                self.settings.arvan_model,
                response.status_code,
                latency_ms,
            )
            raise LLMError("Arvan provider returned invalid JSON.", reason="provider_invalid_json") from exc

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = str(message.get("content") or "")
        reasoning_present = bool(message.get("reasoning_content"))

        if not text:
            if purpose == "intent_router":
                return LLMProviderResult(
                    text="",
                    provider=self.provider_name,
                    model=str(data.get("model") or self.settings.arvan_model),
                    finish_reason=choice.get("finish_reason"),
                    usage=data.get("usage") or {},
                    latency_ms=latency_ms,
                    status_code=response.status_code,
                    endpoint=self.settings.arvan_chat_path,
                    reasoning_present=reasoning_present,
                    reasoning_exposed=False,
                    payload_format="chat_completions",
                )
            logger.warning(
                "event=provider_empty_answer request_id=%s provider=%s model=%s latency_ms=%s",
                request_id,
                self.provider_name,
                self.settings.arvan_model,
                latency_ms,
            )
            raise LLMError("Arvan provider returned an empty answer.", reason="provider_empty_answer")

        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        usage_keys = sorted(str(key) for key in usage.keys())
        numeric_usage_fields = sorted(
            str(key)
            for key, value in usage.items()
            if isinstance(value, (int, float)) and ("token" in str(key).lower() or "cache" in str(key).lower())
        )
        logger.info(
            "event=provider_latency request_id=%s provider=%s model=%s latency_ms=%s purpose=%s assistant_chars=%s output_approx_tokens=%s provider_prompt_tokens=%s provider_completion_tokens=%s provider_total_tokens=%s usage_keys=%s numeric_usage_fields=%s assistant_preview=%r reasoning_present=%s",
            request_id,
            self.provider_name,
            str(data.get("model") or self.settings.arvan_model),
            latency_ms,
            purpose,
            len(text),
            approx_tokens(text),
            prompt_tokens if prompt_tokens is not None else "",
            completion_tokens if completion_tokens is not None else "",
            total_tokens if total_tokens is not None else "",
            ",".join(usage_keys),
            ",".join(numeric_usage_fields),
            text.strip().replace("\n", " ")[:120],
            reasoning_present,
        )

        return LLMProviderResult(
            text=text,
            provider=self.provider_name,
            model=str(data.get("model") or self.settings.arvan_model),
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            latency_ms=latency_ms,
            status_code=response.status_code,
            endpoint=self.settings.arvan_chat_path,
            reasoning_present=reasoning_present,
            reasoning_exposed=False,
            payload_format="chat_completions",
        )
