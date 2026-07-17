"""Arvan/OpenAI-compatible chat completions provider."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import requests
from urllib3.exceptions import ProtocolError

from src.config.llm_deployments import ArvanDeploymentConfig
from src.core.context.models import approx_tokens
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent


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


def _usage_dict(value: Any) -> dict[str, Any]:
    """Preserve reported usage fields without fabricating missing token counts."""
    return dict(value) if isinstance(value, dict) else {}


class ArvanProvider:
    provider_name = "arvan"

    def __init__(self, deployment: ArvanDeploymentConfig, *, enabled: bool = True) -> None:
        self.deployment = deployment
        self.enabled = enabled
        logger.info(
            "event=arvan_provider_initialized deployment=%s provider=%s model=%s host=%s enabled=%s ready=%s",
            deployment.name,
            self.provider_name,
            deployment.model,
            deployment.safe_host,
            enabled,
            self.health()["ready"],
        )

    @property
    def endpoint(self) -> str:
        return self.deployment.endpoint

    def health(self) -> dict[str, object]:
        missing = []
        if not self.enabled:
            missing.append("llm_disabled")
        if not self.deployment.endpoint:
            missing.append("missing_endpoint")
        if not self.deployment.api_key:
            missing.append("missing_api_key")

        return {
            "enabled": self.enabled,
            "ready": not missing,
            "deployment": self.deployment.name,
            "provider": self.provider_name,
            "model": self.deployment.model,
            "host": self.deployment.safe_host,
            "missing": missing,
        }

    def _log_complete(
        self,
        *,
        request_id: str,
        purpose: str,
        status_code: int | str,
        latency_ms: int,
        finish_reason: Any = "",
        usage: dict[str, Any] | None = None,
        output_chars: int = 0,
        reasoning_present: bool = False,
        outcome: str,
    ) -> None:
        usage = usage or {}
        logger.info(
            "event=llm_request_complete request_id=%s purpose=%s deployment=%s provider=%s model=%s host=%s outcome=%s status_code=%s latency_ms=%s finish_reason=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s output_chars=%s reasoning_present=%s",
            request_id,
            purpose,
            self.deployment.name,
            self.provider_name,
            self.deployment.model,
            self.deployment.safe_host,
            outcome,
            status_code,
            latency_ms,
            finish_reason or "",
            usage.get("prompt_tokens", ""),
            usage.get("completion_tokens", ""),
            usage.get("total_tokens", ""),
            output_chars,
            reasoning_present,
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        request_id: str = "",
        max_tokens: int,
        temperature: float | None,
        top_p: float | None,
        timeout_seconds: int,
        purpose: str = "chat",
    ) -> LLMProviderResult:
        readiness = self.health()
        if not readiness["ready"]:
            raise LLMError(
                "Selected Arvan deployment is not ready.",
                reason="provider_not_ready",
                details={"deployment": self.deployment.name},
            )

        payload: dict[str, Any] = {
            "model": self.deployment.model,
            "messages": messages,
            "max_tokens": max_tokens,
            **self.deployment.request_options_dict(),
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        headers = {
            "Authorization": f"{self.deployment.auth_scheme} {self.deployment.api_key}",
            "Content-Type": "application/json",
        }
        timeout = (self.deployment.connect_timeout_seconds, timeout_seconds)

        logger.info(
            "event=provider_request_start request_id=%s purpose=%s deployment=%s provider=%s model=%s host=%s message_count=%s max_tokens=%s connect_timeout_seconds=%s read_timeout_seconds=%s temperature_included=%s top_p_included=%s",
            request_id,
            purpose,
            self.deployment.name,
            self.provider_name,
            self.deployment.model,
            self.deployment.safe_host,
            len(messages),
            max_tokens,
            timeout[0],
            timeout[1],
            "temperature" in payload,
            "top_p" in payload,
        )
        started = time.perf_counter()
        try:
            response = requests.post(self.endpoint, json=payload, headers=headers, timeout=timeout)
        except PROVIDER_REQUEST_EXCEPTIONS as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            retryable = isinstance(exc, RETRYABLE_REQUEST_EXCEPTIONS)
            logger.warning(
                "event=provider_request_exception request_id=%s purpose=%s deployment=%s provider=%s model=%s host=%s error_type=%s retryable=%s latency_ms=%s",
                request_id,
                purpose,
                self.deployment.name,
                self.provider_name,
                self.deployment.model,
                self.deployment.safe_host,
                type(exc).__name__,
                retryable,
                latency_ms,
            )
            self._log_complete(
                request_id=request_id,
                purpose=purpose,
                status_code="",
                latency_ms=latency_ms,
                outcome="transport_error",
            )
            raise LLMError(
                "Arvan request failed.",
                reason="provider_transport_error",
                details={
                    "deployment": self.deployment.name,
                    "error_type": type(exc).__name__,
                    "retryable": retryable,
                },
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=provider_response request_id=%s purpose=%s deployment=%s provider=%s model=%s host=%s status_code=%s latency_ms=%s",
            request_id,
            purpose,
            self.deployment.name,
            self.provider_name,
            self.deployment.model,
            self.deployment.safe_host,
            response.status_code,
            latency_ms,
        )
        if response.status_code >= 400:
            retryable = response.status_code in RETRYABLE_HTTP_STATUS_CODES
            logger.warning(
                "event=provider_http_error request_id=%s purpose=%s deployment=%s provider=%s model=%s host=%s status_code=%s latency_ms=%s retryable=%s",
                request_id,
                purpose,
                self.deployment.name,
                self.provider_name,
                self.deployment.model,
                self.deployment.safe_host,
                response.status_code,
                latency_ms,
                retryable,
            )
            self._log_complete(
                request_id=request_id,
                purpose=purpose,
                status_code=response.status_code,
                latency_ms=latency_ms,
                outcome="http_error",
            )
            raise LLMError(
                "Arvan provider returned an error.",
                reason="provider_http_error",
                details={
                    "deployment": self.deployment.name,
                    "status_code": response.status_code,
                    "error_type": f"HTTP_{response.status_code}",
                    "retryable": retryable,
                },
            )

        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            logger.warning(
                "event=provider_invalid_json request_id=%s purpose=%s deployment=%s provider=%s model=%s host=%s status_code=%s latency_ms=%s",
                request_id,
                purpose,
                self.deployment.name,
                self.provider_name,
                self.deployment.model,
                self.deployment.safe_host,
                response.status_code,
                latency_ms,
            )
            self._log_complete(
                request_id=request_id,
                purpose=purpose,
                status_code=response.status_code,
                latency_ms=latency_ms,
                outcome="invalid_json",
            )
            raise LLMError("Arvan provider returned invalid JSON.", reason="provider_invalid_json") from exc

        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        text = str(message.get("content") or "")
        reasoning_present = bool(message.get("reasoning_content"))
        finish_reason = choice.get("finish_reason")
        usage = _usage_dict(data.get("usage"))

        if not text:
            self._log_complete(
                request_id=request_id,
                purpose=purpose,
                status_code=response.status_code,
                latency_ms=latency_ms,
                finish_reason=finish_reason,
                usage=usage,
                reasoning_present=reasoning_present,
                outcome="empty_content",
            )
            if purpose in {"intent_router", "intent_router_repair"}:
                return LLMProviderResult(
                    text="",
                    provider=self.provider_name,
                    model=self.deployment.model,
                    deployment=self.deployment.name,
                    finish_reason=finish_reason,
                    usage=usage,
                    latency_ms=latency_ms,
                    status_code=response.status_code,
                    endpoint=self.deployment.safe_host,
                    reasoning_present=reasoning_present,
                    reasoning_exposed=False,
                    payload_format="chat_completions",
                )
            raise LLMError(
                "Arvan provider returned an empty answer.",
                reason="provider_empty_answer",
                details={"deployment": self.deployment.name},
            )

        usage_keys = sorted(str(key) for key in usage.keys())
        numeric_usage_fields = sorted(
            str(key)
            for key, value in usage.items()
            if isinstance(value, (int, float)) and ("token" in str(key).lower() or "cache" in str(key).lower())
        )
        logger.info(
            "event=provider_usage request_id=%s purpose=%s deployment=%s provider=%s model=%s status_code=%s latency_ms=%s assistant_chars=%s output_approx_tokens=%s output_tokens=%s usage_keys=%s numeric_usage_fields=%s reasoning_present=%s",
            request_id,
            purpose,
            self.deployment.name,
            self.provider_name,
            self.deployment.model,
            response.status_code,
            latency_ms,
            len(text),
            approx_tokens(text),
            usage.get("output_tokens", ""),
            ",".join(usage_keys),
            ",".join(numeric_usage_fields),
            reasoning_present,
        )
        self._log_complete(
            request_id=request_id,
            purpose=purpose,
            status_code=response.status_code,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            usage=usage,
            output_chars=len(text),
            reasoning_present=reasoning_present,
            outcome="success",
        )

        return LLMProviderResult(
            text=text,
            provider=self.provider_name,
            model=self.deployment.model,
            deployment=self.deployment.name,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency_ms,
            status_code=response.status_code,
            endpoint=self.deployment.safe_host,
            reasoning_present=reasoning_present,
            reasoning_exposed=False,
            payload_format="chat_completions",
        )

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        request_id: str = "",
        max_tokens: int,
        temperature: float | None,
        top_p: float | None,
        timeout_seconds: int,
        purpose: str = "chat",
    ) -> Iterator[LLMStreamEvent]:
        """Normalize OpenAI-compatible SSE chunks without retaining raw payloads."""
        if purpose != "chat":
            raise LLMError(
                "Streaming is supported only for final chat synthesis.",
                reason="provider_stream_purpose_not_supported",
            )
        readiness = self.health()
        if not readiness["ready"]:
            raise LLMError(
                "Selected Arvan deployment is not ready.",
                reason="provider_not_ready",
                details={"deployment": self.deployment.name},
            )

        payload: dict[str, Any] = {
            "model": self.deployment.model,
            "messages": messages,
            "max_tokens": max_tokens,
            **self.deployment.request_options_dict(),
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        headers = {
            "Authorization": f"{self.deployment.auth_scheme} {self.deployment.api_key}",
            "Content-Type": "application/json",
        }
        timeout = (self.deployment.connect_timeout_seconds, timeout_seconds)
        logger.info(
            "event=provider_stream_request_start request_id=%s purpose=%s deployment=%s provider=%s model=%s host=%s message_count=%s max_tokens=%s",
            request_id,
            purpose,
            self.deployment.name,
            self.provider_name,
            self.deployment.model,
            self.deployment.safe_host,
            len(messages),
            max_tokens,
        )

        started = time.perf_counter()
        response: requests.Response | None = None
        chunk_count = 0
        reasoning_chunk_count = 0
        answer_chunk_count = 0
        finish_reason: Any = None
        usage: dict[str, Any] = {}
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=timeout,
                stream=True,
            )
            header_latency_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "event=provider_stream_response request_id=%s purpose=%s deployment=%s provider=%s model=%s host=%s status_code=%s header_latency_ms=%s",
                request_id,
                purpose,
                self.deployment.name,
                self.provider_name,
                self.deployment.model,
                self.deployment.safe_host,
                response.status_code,
                header_latency_ms,
            )
            if response.status_code >= 400:
                retryable = response.status_code in RETRYABLE_HTTP_STATUS_CODES
                raise LLMError(
                    "Arvan provider returned a streaming error.",
                    reason="provider_stream_http_error",
                    details={
                        "deployment": self.deployment.name,
                        "status_code": response.status_code,
                        "error_type": f"HTTP_{response.status_code}",
                        "retryable": retryable,
                    },
                )

            stream_done = False
            for raw_line in response.iter_lines(chunk_size=1, decode_unicode=True):
                line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line or "")
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if line == "[DONE]":
                    stream_done = True
                    break
                try:
                    data = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise LLMError(
                        "Arvan provider returned an invalid streaming event.",
                        reason="provider_stream_invalid_json",
                        details={"error_type": type(exc).__name__},
                    ) from exc
                if not isinstance(data, dict):
                    continue
                if isinstance(data.get("error"), dict):
                    raise LLMError(
                        "Arvan provider returned a streaming error.",
                        reason="provider_stream_error",
                    )

                reported_usage = _usage_dict(data.get("usage"))
                if reported_usage:
                    usage = reported_usage
                    chunk_count += 1
                    yield LLMStreamEvent("usage", data=usage)

                choices = data.get("choices")
                choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                reasoning_delta = delta.get("reasoning_content")
                if isinstance(reasoning_delta, str) and reasoning_delta:
                    chunk_count += 1
                    reasoning_chunk_count += 1
                    yield LLMStreamEvent("reasoning_delta", text=reasoning_delta)
                answer_delta = delta.get("content")
                if isinstance(answer_delta, str) and answer_delta:
                    chunk_count += 1
                    answer_chunk_count += 1
                    yield LLMStreamEvent("answer_delta", text=answer_delta)
                if choice.get("finish_reason") is not None:
                    finish_reason = choice.get("finish_reason")

            latency_ms = int((time.perf_counter() - started) * 1000)
            yield LLMStreamEvent(
                "done",
                data={
                    "provider": self.provider_name,
                    "model": self.deployment.model,
                    "deployment": self.deployment.name,
                    "finish_reason": finish_reason,
                    "usage": usage,
                    "latency_ms": latency_ms,
                    "status_code": response.status_code,
                    "stream_terminated": stream_done,
                },
            )
            logger.info(
                "event=provider_stream_complete request_id=%s purpose=%s deployment=%s provider=%s model=%s status_code=%s latency_ms=%s chunk_count=%s reasoning_chunk_count=%s answer_chunk_count=%s finish_reason=%s",
                request_id,
                purpose,
                self.deployment.name,
                self.provider_name,
                self.deployment.model,
                response.status_code,
                latency_ms,
                chunk_count,
                reasoning_chunk_count,
                answer_chunk_count,
                finish_reason or "",
            )
        except PROVIDER_REQUEST_EXCEPTIONS as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "event=provider_stream_exception request_id=%s purpose=%s deployment=%s provider=%s model=%s host=%s error_type=%s latency_ms=%s",
                request_id,
                purpose,
                self.deployment.name,
                self.provider_name,
                self.deployment.model,
                self.deployment.safe_host,
                type(exc).__name__,
                latency_ms,
            )
            raise LLMError(
                "Arvan streaming request failed.",
                reason="provider_stream_transport_error",
                details={"error_type": type(exc).__name__},
            ) from exc
        finally:
            if response is not None:
                response.close()
