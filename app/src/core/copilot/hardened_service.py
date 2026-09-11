"""Production hardening adapter for E2E-discovered Copilot issues."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import src.core.copilot.service as base_service_module
from src.core.agent.hardened_phase4c import Phase4CWorkflowNodes as HardenedPhase4CWorkflowNodes
from src.core.copilot.service import CopilotService as BaseCopilotService
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent


class CopilotService(BaseCopilotService):
    """Use hardened Phase 4C continuity and never expose truncated final prose."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # BaseCopilotService intentionally owns workflow construction. Bind the
        # compatible Phase4C subclass at the single factory seam it already uses.
        base_service_module.Phase4CWorkflowNodes = HardenedPhase4CWorkflowNodes

    def _stream_final_model(
        self,
        messages: list[dict[str, str]],
        *,
        request_id: str,
        max_tokens: int,
        temperature: float | None,
        top_p: float | None,
        timeout_seconds: int,
        sink: Callable[[LLMStreamEvent], None],
        metrics: dict[str, Any],
        trace_id: str = "",
    ) -> LLMProviderResult:
        buffered: list[LLMStreamEvent] = []
        result = super()._stream_final_model(
            messages,
            request_id=request_id,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            timeout_seconds=timeout_seconds,
            sink=buffered.append,
            metrics=metrics,
            trace_id=trace_id,
        )

        if result.finish_reason == "length":
            metrics["stream_error_type"] = "provider_stream_answer_truncated"
            recovery = self._synthesis_non_stream_fallback(
                messages,
                request_id=request_id,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                timeout_seconds=timeout_seconds,
                sink=sink,
                metrics=metrics,
                reason="provider_stream_answer_truncated",
                trace_id=trace_id,
            )
            if recovery.finish_reason == "length":
                raise LLMError(
                    "The Copilot recovery response reached its output limit.",
                    reason="provider_recovery_truncated",
                    details={"error_type": "provider_recovery_truncated"},
                )
            return recovery

        for event in buffered:
            sink(event)
        return result
