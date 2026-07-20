"""Typed registry for approved read-only semantic capabilities."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field

from src.core.agent.contracts import (
    CapabilitySpec,
    EvidenceFact,
    RetryPolicy,
    ToolResult,
)
from src.core.rag.models import KnowledgeSearchResult


logger = logging.getLogger(__name__)


class EntityInput(BaseModel):
    entities: list[str] = Field(default_factory=list, max_length=2)
    request_id: str = ""
    session_id: str = ""
    route: Any | None = None
    scope: str | None = None
    direction: str | None = None
    depth: int | None = Field(default=None, ge=0, le=2)
    relationship_mode: str | None = None


class KnowledgeInput(BaseModel):
    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=20)
    filters: dict[str, Any] | None = None
    request_id: str = ""


CapabilityHandler = Callable[[BaseModel], ToolResult]


class CapabilityRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, CapabilitySpec] = {}
        self._handlers: dict[str, CapabilityHandler] = {}

    def register(self, spec: CapabilitySpec, handler: CapabilityHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Capability already registered: {spec.name}")
        if not spec.read_only:
            raise ValueError("Initial Copilot capabilities must be read-only.")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def get(self, name: str) -> CapabilitySpec:
        return self._specs[name]

    def list(self, *, planner_visible: bool | None = None) -> tuple[CapabilitySpec, ...]:
        specs = self._specs.values()
        if planner_visible is not None:
            specs = (spec for spec in specs if spec.planner_visible is planner_visible)
        return tuple(sorted(specs, key=lambda item: item.name))

    def execute(self, name: str, payload: dict[str, Any]) -> ToolResult:
        spec = self.get(name)
        minimum, maximum = spec.required_entity_cardinality
        parsed = spec.input_schema.model_validate(payload)
        entities = tuple(getattr(parsed, "entities", ()) or ())
        if not minimum <= len(entities) <= maximum:
            return ToolResult(
                status="invalid",
                entities=entities,
                source_capability=name,
                retrieved_at=_now(),
                freshness="unknown",
                completeness="unknown",
                limitations=("Capability entity cardinality requirement was not met.",),
                error_classification="entity_cardinality_invalid",
            )
        return self._handlers[name](parsed)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provider_result(capability: str, entities: tuple[str, ...], result: Any) -> ToolResult:
    provider_status = str(getattr(result, "status", "unavailable"))
    status_map = {"available": "ok", "not_found": "not_found"}
    status = status_map.get(provider_status, provider_status)
    known_statuses = {"ok", "empty", "not_configured", "unavailable", "invalid", "partial", "not_found"}
    invalid_provider_status = status not in known_statuses
    if invalid_provider_status:
        status = "invalid"
    payload = getattr(result, "raw_payload", None)
    if payload is None:
        payload = getattr(result, "context", None)
    limitations = tuple(getattr(result, "limitations", ()) or ())
    if invalid_provider_status:
        limitations = (*limitations, "Provider returned an unrecognized status.")
    complete = True
    if isinstance(payload, dict):
        retrieval_complete = bool(payload.get("retrieval_complete", True))
        serialization_complete = bool(
            payload.get("serialized_context_complete_for_retrieved_subset", True)
        )
        request_complete = bool(payload.get("complete_for_user_request", retrieval_complete))
        complete = retrieval_complete and serialization_complete and request_complete
        if capability.startswith("graph.") and not retrieval_complete:
            limitations = (*limitations, "Graph retrieval was incomplete.")
        if capability.startswith("graph.") and not serialization_complete:
            limitations = (*limitations, "Graph serialization was incomplete.")
    total_count: int | None = 1 if payload is not None else 0
    included_count: int | None = total_count
    omitted_count: int | None = 0
    if isinstance(payload, dict) and capability.startswith("graph."):
        total_count = payload.get("candidate_node_count", payload.get("total_node_count"))
        included_count = payload.get("retrieved_node_count", payload.get("returned_node_count"))
        if isinstance(total_count, int) and isinstance(included_count, int):
            omitted_count = max(0, total_count - included_count)
        else:
            omitted_count = None
    stale = bool(getattr(result, "stale", False))
    cache_hit = bool(getattr(result, "cache_hit", False))
    completeness = (
        "unknown"
        if status not in {"ok", "empty", "not_found", "partial"}
        else "partial"
        if status == "partial" or not complete
        else "complete"
    )
    safe_error_code = (
        "invalid_provider_status"
        if invalid_provider_status
        else getattr(result, "error_type", None) or getattr(result, "error_reason", None)
    )
    return ToolResult(
        status=status,  # type: ignore[arg-type]
        entities=entities,
        source_capability=capability,
        retrieved_at=_now(),
        freshness="stale" if stale else "current" if status in {"ok", "not_found"} else "unknown",
        completeness=completeness,  # type: ignore[arg-type]
        facts=(EvidenceFact(capability, "Provider evidence payload", payload, entities[0] if len(entities) == 1 else None),)
        if payload is not None
        else (),
        limitations=limitations,
        total_count=total_count,
        included_count=included_count,
        omitted_count=omitted_count,
        truncated=not complete,
        error_classification=safe_error_code,
        safe_error_code=safe_error_code,
        provider=str(getattr(result, "provider", "")),
        valid_at=getattr(result, "fetched_at", None),
        latency_ms=int(getattr(result, "latency_ms", 0) or 0),
        cache_status="stale" if stale else "hit" if cache_hit else "miss",
        evidence_type="graph_topology" if capability.startswith("graph.") else "operational_product",
        raw_payload=payload,
        provider_result=result,
    )


def build_capability_registry(
    *,
    asset_profile_provider: Any,
    detection_provider: Any,
    graph_provider: Any,
    knowledge_service: Any,
) -> CapabilityRegistry:
    registry = CapabilityRegistry()

    def profile(payload: EntityInput) -> ToolResult:
        ip = payload.entities[0]
        result = asset_profile_provider.fetch(ip, payload.request_id, session_id=payload.session_id)
        return _provider_result("asset.get_profile", (ip,), result)

    def detection(payload: EntityInput) -> ToolResult:
        ip = payload.entities[0]
        result = detection_provider.fetch(ip, payload.request_id, session_id=payload.session_id)
        return _provider_result("asset.get_detection", (ip,), result)

    def graph(capability: str) -> CapabilityHandler:
        def run(payload: EntityInput) -> ToolResult:
            route = payload.route
            scope_defaults = {
                "graph.get_summary": "node_summary",
                "graph.get_neighbors": "one_hop",
                "graph.get_relationship": "one_hop",
                "graph.compare_assets": "multi_entity_comparison",
                "graph.find_path": "path",
            }
            if route is not None:
                requested_scope = payload.scope or scope_defaults[capability]
                if capability == "graph.get_neighbors" and payload.scope is None and getattr(route, "scope", None) in {"one_hop", "full_neighbors", "two_hop"}:
                    requested_scope = route.scope
                route = replace(
                    route,
                    scope=requested_scope,
                    direction=payload.direction or getattr(route, "direction", "both"),
                    depth=payload.depth if payload.depth is not None else getattr(route, "depth", 0),
                    relationship_mode=payload.relationship_mode or getattr(route, "relationship_mode", "none"),
                )
            target = getattr(route, "target_entity", None)
            result = graph_provider.provide(target, route=route, request_id=payload.request_id)
            return _provider_result(capability, tuple(payload.entities), result)

        return run

    def knowledge(payload: KnowledgeInput) -> ToolResult:
        result: KnowledgeSearchResult = knowledge_service.search(
            payload.query,
            top_k=payload.top_k,
            filters=payload.filters,
            request_id=payload.request_id,
        )
        return ToolResult(
            status=result.status,
            entities=(),
            source_capability="knowledge.search",
            retrieved_at=result.retrieved_at,
            freshness=result.freshness,
            completeness="partial" if result.truncated else "complete",
            facts=tuple(
                EvidenceFact("knowledge.search", "Retrieved knowledge chunk", chunk.text)
                for chunk in result.chunks
            ),
            limitations=result.limitations,
            total_count=result.total_candidates,
            included_count=result.included_count,
            truncated=result.truncated,
            error_classification=result.error_classification,
            safe_error_code=result.error_classification,
            provider=result.backend,
            evidence_type="documentation",
            citations=tuple(
                {
                    "chunk_id": citation.chunk_id,
                    "relative_path": citation.relative_path,
                    "section": citation.section,
                    "title": citation.title,
                }
                for citation in result.citations
            ),
            omitted_count=max(0, (result.total_candidates or 0) - result.included_count)
            if result.total_candidates is not None
            else None,
            raw_payload=result,
            provider_result=result,
        )

    specs = (
        ("asset.get_profile", "Current Product asset profile evidence.", EntityInput, (1, 1), "operational_product", "product", 0, profile),
        ("asset.get_detection", "Current Product asset-detection evidence.", EntityInput, (1, 1), "operational_product", "product", 0, detection),
        ("graph.get_summary", "Observed topology summary for one asset.", EntityInput, (1, 1), "graph_topology", "graph", 0, graph("graph.get_summary")),
        ("graph.get_neighbors", "Bounded observed neighbors for one asset.", EntityInput, (1, 1), "graph_topology", "graph", 2, graph("graph.get_neighbors")),
        ("graph.get_relationship", "Observed direct relationship for two assets.", EntityInput, (2, 2), "graph_topology", "graph", 1, graph("graph.get_relationship")),
        ("graph.compare_assets", "Bounded topology comparison for two assets.", EntityInput, (2, 2), "graph_topology", "graph", 1, graph("graph.compare_assets")),
        ("graph.find_path", "Bounded observed graph path for two assets.", EntityInput, (2, 2), "graph_topology", "graph", 2, graph("graph.find_path")),
        ("knowledge.search", "Approved SOC documentation retrieval.", KnowledgeInput, (0, 0), "documentation", "knowledge", 0, knowledge),
    )
    for name, description, input_schema, cardinality, evidence_type, group, max_depth, handler in specs:
        product_settings = getattr(asset_profile_provider, "settings", None)
        knowledge_settings = getattr(knowledge_service, "settings", None)
        graph_settings = getattr(graph_provider, "settings", None)
        if group == "product":
            timeout_seconds = float(getattr(product_settings, "product_read_timeout_seconds", 30))
            maximum_result_scope = 1
        elif group == "knowledge":
            timeout_seconds = float(getattr(knowledge_settings, "rag_qdrant_timeout_seconds", 10))
            maximum_result_scope = int(getattr(knowledge_settings, "rag_top_k", 5))
        else:
            timeout_seconds = min(30.0, float(getattr(graph_settings, "agent_request_timeout_seconds", 30)))
            maximum_result_scope = int(getattr(graph_settings, "graph_full_neighbors_hard_max", 5000))
        registry.register(
            CapabilitySpec(
                name=name,
                version="1.0",
                input_schema=input_schema,
                output_schema=ToolResult,
                required_entity_cardinality=cardinality,
                read_only=True,
                timeout_seconds=timeout_seconds,
                retry_policy=RetryPolicy(max_retries=0),
                planner_visible=True,
                description=description,
                evidence_type=evidence_type,
                side_effect_class="read_only",
                cache_policy="provider_managed",
                freshness_policy="current_when_available" if group != "knowledge" else "indexed",
                maximum_graph_depth=max_depth,
                maximum_result_scope=maximum_result_scope,
                required_permissions=("read",),
                concurrency_group=group,
            ),
            handler,
        )
    logger.info(
        "event=capability_registry_initialized capability_count=%s planner_visible_count=%s read_only=true capabilities=%s",
        len(registry.list()),
        len(registry.list(planner_visible=True)),
        ",".join(spec.name for spec in registry.list()),
    )
    return registry
