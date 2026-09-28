"""Graph context provider backed by deterministic topology digests."""

from __future__ import annotations

import logging
import time

from src.config.settings import Settings
from src.core.context.models import GraphProviderResult, ProviderProvenance, ResolvedEntity, RouteDecision, approx_tokens
from src.core.graph.retrieval import GraphRetrievalSpec
from src.core.graph.service import GraphService
from src.core.graph.structured import (
    AssetAggregateRequest,
    AssetSearchRequest,
)


logger = logging.getLogger(__name__)


class GraphContextProvider:
    """Call graph context code directly; no self-HTTP and no raw graph exposure."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.graph_service = GraphService(settings)

    def _structured_limits(self, requested: int | None) -> tuple[int | None, int | None]:
        """Read execution bounds from the runtime policy for safe observability."""
        policy = getattr(getattr(self.graph_service, "repository", None), "policy", None)
        if policy is not None:
            return policy.structured_limit(requested), policy.asset_search_max_limit
        # Lightweight test adapters may expose only settings; production always
        # uses the GraphQueryPolicy branch above.
        maximum = getattr(self.settings, "graph_asset_search_max_limit", None)
        default = getattr(self.settings, "graph_asset_search_default_limit", maximum)
        return (requested if requested is not None else default), maximum

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
            context = self.graph_service.context(
                GraphRetrievalSpec(
                    scope=scope,
                    direction=direction,
                    depth=depth,
                    entities=entities,
                    intent=(route.intent if route and route.intent else "graph_neighbors"),
                    relationship_mode=route.relationship_mode if route else "none",
                    exhaustive_connections_requested=route.exhaustive_connections_requested if route else False,
                ),
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

    def search_assets(
        self,
        request: AssetSearchRequest,
        *,
        request_id: str = "",
    ) -> GraphProviderResult:
        """Execute one typed Asset-set search without topology/entity coercion."""
        started = time.perf_counter()
        try:
            result = self.graph_service.search_assets(request)
        except Exception:
            logger.exception(
                "event=graph_structured_search_failed request_id=%s",
                request_id,
            )
            return GraphProviderResult(
                provider="graph",
                status="unavailable",
                provenance=ProviderProvenance(
                    source="neo4j_structured_asset_projection",
                    status="unavailable",
                ),
                error_reason="provider_exception",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        effective_limit, maximum_limit = self._structured_limits(request.limit)
        context = result.model_dump(mode="json", exclude={"next_cursor"})
        context.update(
            {
                "scope": "asset_search",
                "requested_scope": "asset_search",
                "graph_direction": "none",
                "depth": 0,
                "candidate_node_count": result.matched_total,
                "retrieved_node_count": result.returned_count,
                "returned_node_count": result.returned_count,
                "retrieval_complete": not result.truncated,
                "retrieval_truncated": result.truncated,
                "requested_scope_complete": not result.truncated,
                "complete_for_user_request": not result.truncated,
                "serialized_context_complete_for_retrieved_subset": True,
                "runtime_effective_limit": effective_limit,
                "runtime_max_limit": maximum_limit,
            }
        )
        return GraphProviderResult(
            provider="graph",
            status="available" if result.returned_count else "not_found",
            context=context,
            provenance=ProviderProvenance(
                source="neo4j_structured_asset_projection",
                status="available" if result.returned_count else "not_found",
            ),
            limitations=list(result.limitations),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def aggregate_assets(
        self,
        request: AssetAggregateRequest,
        *,
        request_id: str = "",
    ) -> GraphProviderResult:
        """Execute one typed Asset-set aggregate entirely in Neo4j."""
        started = time.perf_counter()
        try:
            result = self.graph_service.aggregate_assets(request)
        except Exception:
            logger.exception(
                "event=graph_structured_aggregate_failed request_id=%s",
                request_id,
            )
            return GraphProviderResult(
                provider="graph",
                status="unavailable",
                provenance=ProviderProvenance(
                    source="neo4j_structured_asset_projection",
                    status="unavailable",
                ),
                error_reason="provider_exception",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        included_count = (
            sum(group.count for group in result.groups)
            if result.group_by or result.group_by_fields
            else result.count
        )
        effective_limit, maximum_limit = self._structured_limits(request.limit)
        context = result.model_dump(mode="json")
        context.update(
            {
                "scope": "asset_aggregate",
                "requested_scope": "asset_aggregate",
                "direction": "none",
                "depth": 0,
                "candidate_node_count": result.count,
                "retrieved_node_count": included_count,
                "returned_node_count": included_count,
                "retrieval_complete": not result.truncated,
                "retrieval_truncated": result.truncated,
                "requested_scope_complete": not result.truncated,
                "complete_for_user_request": not result.truncated,
                "runtime_effective_limit": (
                    effective_limit
                    if request.operation.value == "group_count"
                    else None
                ),
                "runtime_max_limit": maximum_limit,
                "serialized_context_complete_for_retrieved_subset": True,
            }
        )
        return GraphProviderResult(
            provider="graph",
            status="available",
            context=context,
            provenance=ProviderProvenance(
                source="neo4j_structured_asset_projection",
                status="available",
            ),
            limitations=list(result.limitations),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
