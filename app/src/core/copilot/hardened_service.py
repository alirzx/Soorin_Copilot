"""Production hardening adapter for E2E-discovered Copilot issues."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any

import src.core.agent.nodes as agent_nodes_module
import src.core.copilot.service as base_service_module
from src.core.agent.hardened_phase4c import Phase4CWorkflowNodes as HardenedPhase4CWorkflowNodes
from src.core.agent.hardened_reviewer import EvidenceReviewer as HardenedEvidenceReviewer
from src.core.context.composer import ContextComposer as BaseContextComposer
from src.core.copilot.service import CopilotService as BaseCopilotService
from src.core.graph.organizational_neo4j import OrganizationalNeo4jGraphRepository
from src.core.graph.structured import MAX_AGGREGATE_GROUP_MEMBER_IPS
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent


class HardenedContextComposer(BaseContextComposer):
    """Preserve bounded aggregate member IP identities through model serialization."""

    @classmethod
    def _bounded_structured_value(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)) and value:
            member_ips: list[str] = []
            for item in value:
                try:
                    address = ipaddress.ip_address(str(item).strip())
                except ValueError:
                    break
                if address.version != 4:
                    break
                member_ips.append(str(address))
            else:
                return member_ips[:MAX_AGGREGATE_GROUP_MEMBER_IPS]
        return super()._bounded_structured_value(value)


def _bind_organizational_graph_repository(graph_provider: Any, settings: Any) -> None:
    """Keep chat Graph/Exact Search on the same organizational repository as Graph APIs."""
    graph_service = graph_provider.graph_service
    graph_service.repository = OrganizationalNeo4jGraphRepository(
        graph_service.driver,
        settings,
    )


class CopilotService(BaseCopilotService):
    """Use E2E hardening without changing the public Copilot API contract."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.evidence_reviewer = HardenedEvidenceReviewer()
        _bind_organizational_graph_repository(self.graph_provider, self.settings)
        self.context_composer = HardenedContextComposer(self.settings)
        # Workflow nodes instantiate their composer per request. Keep that seam
        # aligned with the hardened service-level composer without changing the
        # public context contract.
        agent_nodes_module.ContextComposer = HardenedContextComposer
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
