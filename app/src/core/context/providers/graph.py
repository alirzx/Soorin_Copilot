"""Graph context provider backed by deterministic topology digests."""

from __future__ import annotations

import json
import logging
import time

from src.core.context.models import GraphProviderResult, ProviderProvenance, ResolvedEntity, approx_tokens
from src.core.graph.context import build_graph_context


logger = logging.getLogger(__name__)


class GraphContextProvider:
    """Call graph context code directly; no self-HTTP and no raw graph exposure."""

    def provide(self, entity: ResolvedEntity, *, request_id: str = "") -> GraphProviderResult:
        started = time.perf_counter()
        logger.info(
            "event=graph_context_provider_start request_id=%s target_ip=%s source=%s",
            request_id,
            entity.value,
            entity.source,
        )
        provenance = ProviderProvenance(source="observed_communication_graph", status="unavailable")

        try:
            context = build_graph_context(entity.value)
        except FileNotFoundError:
            latency_ms = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "event=graph_context_provider_complete request_id=%s target_ip=%s status=unavailable reason=artifact_missing latency_ms=%s",
                request_id,
                entity.value,
                latency_ms,
            )
            return GraphProviderResult(
                provider="graph",
                status="unavailable",
                target_entity=entity,
                provenance=provenance,
                limitations=[],
                error_reason="artifact_missing",
                latency_ms=latency_ms,
            )
        except Exception:
            latency_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "event=graph_context_provider_exception request_id=%s target_ip=%s latency_ms=%s",
                request_id,
                entity.value,
                latency_ms,
            )
            return GraphProviderResult(
                provider="graph",
                status="unavailable",
                target_entity=entity,
                provenance=provenance,
                limitations=[],
                error_reason="provider_exception",
                latency_ms=latency_ms,
            )

        status = "available" if context.get("node_found") else "not_found"
        provenance = ProviderProvenance(source="observed_communication_graph", status=status)
        degree = context.get("degree") or {}
        inbound_count = len(context.get("top_inbound_peers") or [])
        outbound_count = len(context.get("top_outbound_peers") or [])
        context_chars = len(json.dumps(context, sort_keys=True))
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=graph_context_provider_complete request_id=%s target_ip=%s status=%s degree_in=%s degree_out=%s degree_total=%s inbound_peers=%s outbound_peers=%s context_chars=%s context_approx_tokens=%s latency_ms=%s",
            request_id,
            entity.value,
            status,
            degree.get("in", 0),
            degree.get("out", 0),
            degree.get("total", 0),
            inbound_count,
            outbound_count,
            context_chars,
            approx_tokens("x" * context_chars),
            latency_ms,
        )
        return GraphProviderResult(
            provider="graph",
            status=status,
            target_entity=entity,
            context=context,
            provenance=provenance,
            limitations=list(context.get("limitations") or []),
            latency_ms=latency_ms,
        )
