"""Graph context provider backed by deterministic topology digests."""

from __future__ import annotations

import logging
import time

from src.config.settings import Settings
from src.core.context.models import GraphProviderResult, ProviderProvenance, ResolvedEntity, RouteDecision, approx_tokens
from src.core.graph.retrieval import GraphRetrievalSpec, retrieve_graph_context


logger = logging.getLogger(__name__)


class GraphContextProvider:
    """Call graph context code directly; no self-HTTP and no raw graph exposure."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def provide(
        self,
        entity: ResolvedEntity | None,
        *,
        route: RouteDecision | None = None,
        request_id: str = "",
    ) -> GraphProviderResult:
        started = time.perf_counter()
        entities = route.target_entities if route and route.target_entities else ([entity] if entity else [])
        scope = route.scope if route else "node_summary"
        direction = route.direction if route else "both"
        depth = route.depth if route else 0
        target_label = entity.value if entity else ",".join(item.value for item in entities[:2])
        logger.info(
            "event=graph_context_provider_start request_id=%s target_ip=%s source=%s scope=%s direction=%s depth=%s entity_count=%s",
            request_id,
            target_label,
            entity.source if entity else "multi",
            scope,
            direction,
            depth,
            len(entities),
        )
        provenance = ProviderProvenance(source="observed_communication_graph", status="unavailable")

        try:
            context = retrieve_graph_context(
                GraphRetrievalSpec(
                    scope=scope,
                    direction=direction,
                    depth=depth,
                    entities=entities,
                    intent=route.intent if route else "graph_neighbors",
                    relationship_mode=route.relationship_mode if route else "none",
                    exhaustive_connections_requested=route.exhaustive_connections_requested if route else False,
                ),
                self.settings,
            )
        except FileNotFoundError:
            latency_ms = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "event=graph_context_provider_complete request_id=%s target_ip=%s status=unavailable reason=artifact_missing latency_ms=%s",
                request_id,
                target_label,
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
                target_label,
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

        if route and "security_or_anomaly" in route.matched_signals:
            context["formal_anomaly_evidence_available"] = False
            context["graph_structural_analysis_available"] = True
            limitations = list(context.get("limitations") or [])
            anomaly_limitation = (
                "No dedicated anomaly provider evidence is available; only bounded graph structural analysis is supplied."
            )
            if anomaly_limitation not in limitations:
                limitations.append(anomaly_limitation)
            context["limitations"] = limitations

        status = "available" if context.get("node_found") else "not_found"
        provenance = ProviderProvenance(source="observed_communication_graph", status=status)
        context_chars = len(str(context))
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=graph_context_provider_complete request_id=%s target_ip=%s status=%s requested_scope=%s direction=%s depth=%s inbound_total=%s inbound_retrieved=%s outbound_total=%s outbound_retrieved=%s bidirectional_total=%s bidirectional_retrieved=%s candidate_nodes=%s returned_nodes=%s candidate_edges=%s returned_edges=%s retrieval_complete=%s retrieval_truncated=%s retrieval_truncation_reason=%s requested_scope_complete=%s complete_for_user_request=%s context_chars=%s context_approx_tokens=%s latency_ms=%s",
            request_id,
            target_label,
            status,
            context.get("requested_scope", context.get("scope", "")),
            context.get("direction", ""),
            context.get("depth", ""),
            context.get("inbound_total", 0),
            context.get("inbound_retrieved", context.get("inbound_returned", 0)),
            context.get("outbound_total", 0),
            context.get("outbound_retrieved", context.get("outbound_returned", 0)),
            context.get("bidirectional_total", 0),
            context.get("bidirectional_retrieved", context.get("bidirectional_returned", 0)),
            context.get("candidate_node_count", 0),
            context.get("retrieved_node_count", context.get("returned_node_count", 0)),
            context.get("candidate_edge_count", 0),
            context.get("retrieved_edge_count", context.get("returned_edge_count", 0)),
            context.get("retrieval_complete", False),
            context.get("retrieval_truncated", context.get("truncated", False)),
            context.get("retrieval_truncation_reason") or context.get("truncation_reason") or "",
            context.get("requested_scope_complete", False),
            context.get("complete_for_user_request", False),
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
