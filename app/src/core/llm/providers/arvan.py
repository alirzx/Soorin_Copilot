"""Arvan/OpenAI-compatible chat completions provider."""

from __future__ import annotations

import time
from typing import Any

import requests

from src.config.settings import Settings
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult


class ArvanProvider:
    provider_name = "arvan"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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

    def chat(self, messages: list[dict[str, str]]) -> LLMProviderResult:
        readiness = self.health()
        if not readiness["ready"]:
            raise LLMError("Arvan provider is not configured.", reason="provider_not_ready")

        payload = {
            "model": self.settings.arvan_model,
            "messages": messages,
            "max_tokens": self.settings.arvan_max_tokens,
            "temperature": self.settings.arvan_temperature,
            "top_p": self.settings.arvan_top_p,
        }
        headers = {
            "Authorization": f"{self.settings.arvan_auth_scheme} {self.settings.arvan_api_key}",
            "Content-Type": "application/json",
        }
        timeout = (
            self.settings.arvan_connect_timeout_seconds,
            self.settings.arvan_timeout_seconds,
        )

        started = time.perf_counter()
        try:
            response = requests.post(self.endpoint, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise LLMError("Arvan request failed.", reason="provider_request_failed") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            raise LLMError(
                "Arvan provider returned an error.",
                reason="provider_http_error",
                details={"status_code": response.status_code},
            )

        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise LLMError("Arvan provider returned invalid JSON.", reason="provider_invalid_json") from exc

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = str(message.get("content") or "")
        reasoning_present = bool(message.get("reasoning_content"))

        if not text:
            raise LLMError("Arvan provider returned an empty answer.", reason="provider_empty_answer")

        return LLMProviderResult(
            text=text,
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
