"""Provider-neutral LLM client."""

from __future__ import annotations

import logging
import random
import time

from src.config.settings import Settings
from src.core.llm.errors import LLMDisabledError, LLMError
from src.core.llm.providers.arvan import ArvanProvider
from src.core.llm.providers.base import LLMProviderResult


logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if settings.llm_provider == "arvan":
            self.provider = ArvanProvider(settings)
        else:
            self.provider = None
        logger.info(
            "event=provider_initialization provider=%s model=%s enabled=%s ready=%s",
            settings.llm_provider,
            settings.arvan_model,
            settings.llm_enabled,
            self.provider is not None,
        )

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
        if self.provider is None:
            raise LLMError("Unsupported LLM provider.", reason="provider_not_supported")
        configured_retries = max(0, int(self.settings.llm_max_transient_retries))
        requested_retries = configured_retries if transient_retries is None else max(0, int(transient_retries))
        max_retries = 0 if purpose == "intent_router" else min(1, requested_retries)

        for attempt in range(max_retries + 1):
            attempt_started = time.perf_counter()
            if attempt:
                logger.info(
                    "event=provider_retry_attempt request_id=%s provider=%s model=%s purpose=%s attempt=%s max_retries=%s",
                    request_id,
                    self.settings.llm_provider,
                    self.settings.arvan_model,
                    purpose,
                    attempt + 1,
                    max_retries,
                )
            try:
                result = self.provider.chat(
                    messages,
                    request_id=request_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    timeout_seconds=timeout_seconds,
                    purpose=purpose,
                )
            except LLMError as exc:
                attempt_latency_ms = int((time.perf_counter() - attempt_started) * 1000)
                retryable = bool(exc.details.get("retryable", False))
                error_type = str(exc.details.get("error_type") or exc.reason)
                if not retryable:
                    logger.warning(
                        "event=provider_retry_skipped request_id=%s provider=%s model=%s purpose=%s retryable=false retry_count=%s max_retries=%s reason=%s error_type=%s attempt_latency_ms=%s",
                        request_id,
                        self.settings.llm_provider,
                        self.settings.arvan_model,
                        purpose,
                        attempt,
                        max_retries,
                        exc.reason,
                        error_type,
                        attempt_latency_ms,
                    )
                    raise
                if attempt >= max_retries:
                    logger.warning(
                        "event=provider_retry_exhausted request_id=%s provider=%s model=%s purpose=%s retry_count=%s max_retries=%s reason=%s error_type=%s attempt_latency_ms=%s",
                        request_id,
                        self.settings.llm_provider,
                        self.settings.arvan_model,
                        purpose,
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
                    "event=provider_retry_scheduled request_id=%s provider=%s model=%s purpose=%s retry_count=%s next_attempt=%s reason=%s error_type=%s delay_seconds=%.3f attempt_latency_ms=%s",
                    request_id,
                    self.settings.llm_provider,
                    self.settings.arvan_model,
                    purpose,
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
                    "event=provider_retry_succeeded request_id=%s provider=%s model=%s purpose=%s retry_count=%s attempt_latency_ms=%s",
                    request_id,
                    result.provider,
                    result.model,
                    purpose,
                    attempt,
                    int((time.perf_counter() - attempt_started) * 1000),
                )
            return result

        raise LLMError("LLM provider retry loop ended unexpectedly.", reason="provider_retry_state_error")

    def health(self) -> dict[str, object]:
        if self.provider is None:
            return {
                "enabled": self.settings.llm_enabled,
                "ready": False,
                "provider": self.settings.llm_provider,
                "model": self.settings.arvan_model,
                "reason": "provider_not_supported",
            }
        return self.provider.health()
