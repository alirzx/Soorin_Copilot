"""Provider-neutral LLM client with purpose-specific deployment selection."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterator

from src.config.llm_deployments import ArvanDeploymentConfig, LLMRequestConfig
from src.config.settings import Settings
from src.core.llm.errors import LLMDisabledError, LLMError
from src.core.llm.providers.arvan import ArvanProvider
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent


logger = logging.getLogger(__name__)
STRUCTURED_PURPOSES = {"intent_router", "intent_router_repair", "planner", "planner_repair"}


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.providers: dict[str, ArvanProvider] = {}
        if settings.llm_provider == "arvan":
            selected_aliases = dict.fromkeys(
                (
                    settings.intent_router_deployment,
                    settings.chat_deployment,
                    *([settings.planner_deployment] if settings.planner_enabled else []),
                )
            )
            for alias in selected_aliases:
                deployment = settings.deployment(alias)
                self.providers[alias] = ArvanProvider(
                    deployment,
                    enabled=settings.llm_enabled,
                )
        self.provider = self.providers.get(settings.chat_deployment)
        router = settings.deployment(settings.intent_router_deployment)
        chat = settings.deployment(settings.chat_deployment)
        planner = settings.deployment(settings.planner_deployment)
        logger.info(
            "event=provider_initialization provider=%s enabled=%s router_deployment=%s router_model=%s router_host=%s planner_enabled=%s planner_deployment=%s planner_model=%s planner_host=%s chat_deployment=%s chat_model=%s chat_host=%s ready=%s",
            settings.llm_provider,
            settings.llm_enabled,
            router.name,
            router.model,
            router.safe_host,
            settings.planner_enabled,
            planner.name,
            planner.model,
            planner.safe_host,
            chat.name,
            chat.model,
            chat.safe_host,
            bool(self.providers),
        )

    def validate_configuration(self) -> None:
        self.settings.validate_selected_llm_deployments()

    def deployment_for_purpose(self, purpose: str) -> ArvanDeploymentConfig:
        return self.settings.deployment_for_purpose(purpose)

    def request_config(self, purpose: str) -> LLMRequestConfig:
        return self.deployment_for_purpose(purpose).request_config(purpose)

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
        transient_retries: int | None = None,
    ) -> LLMProviderResult:
        if not self.settings.llm_enabled:
            raise LLMDisabledError("LLM is disabled.", reason="llm_disabled")

        deployment = self.deployment_for_purpose(purpose)
        provider = self.providers.get(deployment.name)
        if provider is None:
            raise LLMError("Unsupported LLM provider.", reason="provider_not_supported")

        defaults = deployment.request_config(purpose)
        resolved_max_tokens = defaults.max_tokens
        if max_tokens is not None:
            resolved_max_tokens = min(
                max(1, int(max_tokens)),
                max(1, deployment.maximum_completion_tokens),
            )
        resolved_temperature = defaults.temperature if temperature is None else temperature
        if not deployment.supports_temperature:
            resolved_temperature = None
        resolved_top_p = defaults.top_p if top_p is None else top_p
        if not deployment.supports_top_p:
            resolved_top_p = None
        resolved_timeout = defaults.read_timeout_seconds if timeout_seconds is None else max(1, int(timeout_seconds))

        configured_retries = max(0, int(self.settings.llm_max_transient_retries))
        requested_retries = configured_retries if transient_retries is None else max(0, int(transient_retries))
        max_retries = 0 if purpose in STRUCTURED_PURPOSES else min(1, requested_retries)

        for attempt in range(max_retries + 1):
            attempt_started = time.perf_counter()
            if attempt:
                logger.info(
                    "event=provider_retry_attempt request_id=%s purpose=%s deployment=%s provider=%s model=%s attempt=%s max_retries=%s",
                    request_id,
                    purpose,
                    deployment.name,
                    deployment.provider_type,
                    deployment.model,
                    attempt + 1,
                    max_retries,
                )
            try:
                result = provider.chat(
                    messages,
                    request_id=request_id,
                    max_tokens=resolved_max_tokens,
                    temperature=resolved_temperature,
                    top_p=resolved_top_p,
                    timeout_seconds=resolved_timeout,
                    purpose=purpose,
                )
            except LLMError as exc:
                attempt_latency_ms = int((time.perf_counter() - attempt_started) * 1000)
                retryable = bool(exc.details.get("retryable", False))
                error_type = str(exc.details.get("error_type") or exc.reason)
                if not retryable:
                    logger.warning(
                        "event=provider_retry_skipped request_id=%s purpose=%s deployment=%s provider=%s model=%s retryable=false retry_count=%s max_retries=%s reason=%s error_type=%s attempt_latency_ms=%s",
                        request_id,
                        purpose,
                        deployment.name,
                        deployment.provider_type,
                        deployment.model,
                        attempt,
                        max_retries,
                        exc.reason,
                        error_type,
                        attempt_latency_ms,
                    )
                    raise
                if attempt >= max_retries:
                    logger.warning(
                        "event=provider_retry_exhausted request_id=%s purpose=%s deployment=%s provider=%s model=%s retry_count=%s max_retries=%s reason=%s error_type=%s attempt_latency_ms=%s",
                        request_id,
                        purpose,
                        deployment.name,
                        deployment.provider_type,
                        deployment.model,
                        attempt,
                        max_retries,
                        exc.reason,
                        error_type,
                        attempt_latency_ms,
                    )
                    raise

                base_delay = max(0.0, float(self.settings.llm_retry_base_delay_seconds))
                max_delay = max(base_delay, float(self.settings.llm_retry_max_delay_seconds))
                backoff = min(max_delay, base_delay * (2**attempt))
                jitter_limit = min(backoff * 0.25, max(0.0, max_delay - backoff))
                delay = min(max_delay, backoff + random.uniform(0.0, jitter_limit))
                logger.warning(
                    "event=provider_retry_scheduled request_id=%s purpose=%s deployment=%s provider=%s model=%s retry_count=%s next_attempt=%s reason=%s error_type=%s delay_seconds=%.3f attempt_latency_ms=%s",
                    request_id,
                    purpose,
                    deployment.name,
                    deployment.provider_type,
                    deployment.model,
                    attempt + 1,
                    attempt + 2,
                    exc.reason,
                    error_type,
                    delay,
                    attempt_latency_ms,
                )
                if delay:
                    time.sleep(delay)
                continue

            if attempt:
                logger.info(
                    "event=provider_retry_succeeded request_id=%s purpose=%s deployment=%s provider=%s model=%s retry_count=%s attempt_latency_ms=%s",
                    request_id,
                    purpose,
                    deployment.name,
                    result.provider,
                    result.model,
                    attempt,
                    int((time.perf_counter() - attempt_started) * 1000),
                )
            return result

        raise LLMError("LLM provider retry loop ended unexpectedly.", reason="provider_retry_state_error")

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        request_id: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        timeout_seconds: int | None = None,
        purpose: str = "chat",
    ) -> Iterator[LLMStreamEvent]:
        """Stream only the final chat deployment; routing remains non-streaming."""
        if purpose != "chat":
            raise LLMError(
                "Streaming is supported only for final chat synthesis.",
                reason="provider_stream_purpose_not_supported",
            )
        if not self.settings.llm_enabled:
            raise LLMDisabledError("LLM is disabled.", reason="llm_disabled")

        deployment = self.deployment_for_purpose(purpose)
        provider = self.providers.get(deployment.name)
        if provider is None or not callable(getattr(provider, "stream_chat", None)):
            raise LLMError(
                "Selected LLM provider does not support streaming.",
                reason="provider_stream_not_supported",
            )

        defaults = deployment.request_config(purpose)
        resolved_max_tokens = defaults.max_tokens
        if max_tokens is not None:
            resolved_max_tokens = min(
                max(1, int(max_tokens)),
                max(1, deployment.maximum_completion_tokens),
            )
        resolved_temperature = defaults.temperature if temperature is None else temperature
        if not deployment.supports_temperature:
            resolved_temperature = None
        resolved_top_p = defaults.top_p if top_p is None else top_p
        if not deployment.supports_top_p:
            resolved_top_p = None
        resolved_timeout = defaults.read_timeout_seconds if timeout_seconds is None else max(1, int(timeout_seconds))

        yield from provider.stream_chat(
            messages,
            request_id=request_id,
            max_tokens=resolved_max_tokens,
            temperature=resolved_temperature,
            top_p=resolved_top_p,
            timeout_seconds=resolved_timeout,
            purpose=purpose,
        )

    def health(self) -> dict[str, object]:
        if not self.providers:
            chat = self.settings.deployment(self.settings.chat_deployment)
            return {
                "enabled": self.settings.llm_enabled,
                "ready": False,
                "provider": self.settings.llm_provider,
                "model": chat.model,
                "deployment": chat.name,
                "reason": "provider_not_supported",
            }

        router = self.providers[self.settings.intent_router_deployment].health()
        chat = self.providers[self.settings.chat_deployment].health()
        planner = (
            self.providers[self.settings.planner_deployment].health()
            if self.settings.planner_enabled
            else {"enabled": False, "ready": True, "deployment": self.settings.planner_deployment}
        )
        return {
            "enabled": self.settings.llm_enabled,
            "ready": bool(router["ready"] and chat["ready"]),
            "provider": chat["provider"],
            "model": chat["model"],
            "deployment": chat["deployment"],
            "router": router,
            "planner": planner,
            "chat": chat,
        }
