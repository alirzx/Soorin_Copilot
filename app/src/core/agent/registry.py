"""Typed registry for approved read-only semantic capabilities."""

from __future__ import annotations

import logging
import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from src.core.agent.contracts import (
    CapabilitySpec,
    EvidenceFact,
    EvidenceReceipt,
    RetryPolicy,
    ToolResult,
)
from src.core.agent.context_identity import knowledge_query_hash
from src.core.agent.structured_evidence import structured_evidence_from_context
from src.core.rag.models import KnowledgeSearchResult
from src.core.graph.structured import (
    AssetAggregateCapabilityInput,
    AssetSearchCapabilityInput,
)
from src.core.context.product_views import (
    MAX_PRODUCT_VIEW_TOKENS,
    MIN_PRODUCT_VIEW_TOKENS,
    ProductEvidenceView,
    approved_views,
    build_product_view,
    normalize_product_views,
    normalize_purpose,
)


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
    views: list[str] = Field(default_factory=list, max_length=6)
    detail: Literal["brief", "standard", "deep"] = "standard"
    max_context_tokens: int = Field(
        default=3000,
        ge=MIN_PRODUCT_VIEW_TOKENS,
        le=MAX_PRODUCT_VIEW_TOKENS,
    )
    purpose: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")


class KnowledgeInput(BaseModel):
    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=20)
    filters: dict[str, Any] | None = None
    request_id: str = ""
    purpose: str = Field(default="general_reference", max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    max_context_tokens: int = Field(default=3000, ge=1, le=8000)


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


def _provider_result(
    capability: str,
    entities: tuple[str, ...],
    result: Any,
    *,
    evidence_view: ProductEvidenceView | None = None,
) -> ToolResult:
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
    if evidence_view and "similarity" in evidence_view.selected_views:
        limitations = (*limitations, "Similarity is rule/tag/role affinity, not model embedding-space similarity.")
    if evidence_view and "cluster" in evidence_view.selected_views:
        limitations = (
            *limitations,
            "Cluster is rule/tag/role-affinity grouping, not unsupervised ML clustering; population one is weak cohort evidence.",
        )
    if invalid_provider_status:
        limitations = (*limitations, "Provider returned an unrecognized status.")
    complete = True
    if isinstance(payload, dict):
        retrieval_complete = bool(payload.get("retrieval_complete", True))
        serialization_complete = bool(
            payload.get("serialized_context_complete_for_retrieved_subset", True)
        )
        if "complete_for_user_request" in payload:
            request_complete = bool(payload["complete_for_user_request"])
        elif "requested_scope_complete" in payload:
            request_complete = bool(payload["requested_scope_complete"])
        else:
            request_complete = retrieval_complete
        complete = request_complete and serialization_complete
        if capability.startswith("graph.") and not retrieval_complete:
            limitations = (
                *limitations,
                "Broader graph retrieval was bounded; requested-scope completeness is reported separately."
                if request_complete
                else "Graph retrieval was incomplete for the requested scope.",
            )
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
    fact_payload = evidence_view.payload if evidence_view else payload
    receipt_payload: Any = payload
    receipt_schema_version = "graph-baseline-v1" if capability.startswith("graph.") else ""
    receipt_scope = "none"
    receipt_direction = "none"
    receipt_depth = 0
    if evidence_view is not None:
        # A full Product view remains raw for established presentation contracts,
        # but its immutable acquisition receipt always has the same view wrapper.
        receipt_payload = {
            "provider": evidence_view.provider,
            "views": {
                view: (
                    evidence_view.payload.get("views", {}).get(view)
                    if evidence_view.selected_views != ("full",)
                    and isinstance(evidence_view.payload, dict)
                    and isinstance(evidence_view.payload.get("views"), dict)
                    else evidence_view.payload
                )
                for view in evidence_view.selected_views
            },
            "projection_metadata": {
                "source_payload_complete": evidence_view.source_payload_complete,
                "selected_views": list(evidence_view.selected_views),
                "receipt_version": "v1",
            },
        }
        receipt_schema_version = evidence_view.schema_version
    elif capability.startswith("graph.") and isinstance(payload, dict):
        receipt_scope = str(payload.get("requested_scope") or payload.get("scope") or "none")
        receipt_direction = str(payload.get("direction") or "none")
        receipt_depth = int(payload.get("depth") or 0)
    receipt = EvidenceReceipt.from_payload(
        payload=receipt_payload,
        source_capability=capability,
        entities=entities,
        status=status,  # type: ignore[arg-type]
        freshness="stale" if stale else "current" if status in {"ok", "not_found"} else "unknown",
        completeness=completeness,  # type: ignore[arg-type]
        retrieved_at=str(getattr(result, "fetched_at", None) or _now()),
        source_payload_complete=evidence_view.source_payload_complete if evidence_view else payload is not None,
        projection_usable=evidence_view.projection_usable if evidence_view else payload is not None,
        truncated=not complete,
        projection_truncated=evidence_view.truncated if evidence_view else False,
        schema_version=receipt_schema_version or "evidence-receipt-v1",
        scope=receipt_scope,
        direction=receipt_direction,
        depth=receipt_depth,
    )
    return ToolResult(
        status=status,  # type: ignore[arg-type]
        entities=entities,
        source_capability=capability,
        retrieved_at=_now(),
        freshness="stale" if stale else "current" if status in {"ok", "not_found"} else "unknown",
        completeness=completeness,  # type: ignore[arg-type]
        facts=(EvidenceFact(capability, "Provider evidence payload", fact_payload, entities[0] if len(entities) == 1 else None),)
        if fact_payload is not None
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
        selected_views=evidence_view.selected_views if evidence_view else (),
        detail=evidence_view.detail if evidence_view else "standard",
        purpose=evidence_view.purpose if evidence_view else "",
        view_payload=evidence_view.payload if evidence_view else None,
        payload_inventory=evidence_view.inventory.as_dict() if evidence_view else {},
        included_paths=evidence_view.included_paths if evidence_view else (),
        omitted_section_count=evidence_view.omitted_path_count if evidence_view else 0,
        view_token_estimate=evidence_view.token_estimate if evidence_view else 0,
        source_payload_complete=evidence_view.source_payload_complete if evidence_view else payload is not None,
        projection_usable=evidence_view.projection_usable if evidence_view else payload is not None,
        usable_fact_count=evidence_view.usable_fact_count if evidence_view else int(payload is not None),
        projection_truncated=evidence_view.truncated if evidence_view else False,
        projection_omitted_count=evidence_view.projection_omitted_count if evidence_view else 0,
        projection_schema_version=evidence_view.schema_version if evidence_view else "",
        evidence_receipt=receipt,
    )


def build_capability_registry(
    *,
    asset_profile_provider: Any,
    detection_provider: Any,
    graph_provider: Any,
    knowledge_service: Any,
) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    product_fetches: dict[tuple[str, str, str, str], Any] = {}
    product_fetch_lock = threading.Lock()

    def fetch_product_once(
        provider: str,
        ip: str,
        request_id: str,
        session_id: str,
        view: str = "full",
    ) -> Any:
        key = (request_id, provider, ip, view)
        with product_fetch_lock:
            if key in product_fetches:
                return product_fetches[key]
            source = asset_profile_provider if provider == "asset_profile" else detection_provider
            try:
                result = source.fetch(ip, request_id, session_id=session_id, view=view)
            except TypeError as exc:
                if "view" not in str(exc):
                    raise
                result = source.fetch(ip, request_id, session_id=session_id)
            product_fetches[key] = result
            if len(product_fetches) > 512:
                product_fetches.pop(next(iter(product_fetches)))
            return result

    def product_result(capability: str, provider: str, payload: EntityInput) -> ToolResult:
        ip = payload.entities[0]
        selected = normalize_product_views(provider, tuple(payload.views) or ("overview",))
        if any(view not in approved_views(provider) for view in selected):
            raise ValueError("Product capability requested an unapproved evidence view.")
        fetch_views = selected if provider == "detection" else ("full",)
        fetched = [
            fetch_product_once(provider, ip, payload.request_id, payload.session_id, view)
            for view in fetch_views
        ]
        available = [item for item in fetched if getattr(item, "raw_payload", None) is not None]
        if not available:
            return _provider_result(capability, (ip,), fetched[0])
        if len(available) == 1:
            result = available[0]
            source_payload = result.raw_payload
        else:
            source_payload = {
                view_name: item.raw_payload
                for view_name, item in zip(fetch_views, fetched)
                if getattr(item, "raw_payload", None) is not None
            }
            serialized = json.dumps(source_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            result = replace(
                available[0],
                raw_payload=source_payload,
                serialized_json=serialized,
                raw_json_chars=len(serialized),
                raw_json_bytes=len(serialized.encode("utf-8")),
                raw_top_level_key_count=len(source_payload),
                limitations=list(dict.fromkeys(item for result_item in available for item in result_item.limitations)),
            )
        view = build_product_view(
            source_payload,
            provider=provider,
            views=selected,
            detail=payload.detail,
            max_context_tokens=payload.max_context_tokens,
            purpose=normalize_purpose(payload.purpose, "general_assessment"),
        )
        logger.info(
            "event=profile_projection_completed request_id=%s provider=%s target_ip=%s views=%s detail=%s purpose=%s usable_fact_count=%s omitted_paths=%s view_tokens=%s raw_payload_retained_internally=true model_representation=projected",
            payload.request_id,
            provider,
            ip,
            ",".join(view.selected_views),
            view.detail,
            view.purpose,
            view.usable_fact_count,
            view.omitted_path_count,
            view.token_estimate,
        )
        if provider == "detection":
            logger.info(
                "event=detection_view_retrieved request_id=%s target_ip=%s views=%s status=%s included_count=%s",
                payload.request_id,
                ip,
                ",".join(view.selected_views),
                getattr(result, "status", "unavailable"),
                view.usable_fact_count,
            )
        return _provider_result(capability, (ip,), result, evidence_view=view)

    def profile(payload: EntityInput) -> ToolResult:
        return product_result("asset.get_profile", "asset_profile", payload)

    def detection(payload: EntityInput) -> ToolResult:
        return product_result("asset.get_detection", "detection", payload)

    def structured_result(
        capability: str,
        result: Any,
        *,
        semantic_query_id: str | None = None,
    ) -> ToolResult:
        context = (
            dict(result.context)
            if isinstance(getattr(result, "context", None), dict)
            else {}
        )
        if semantic_query_id:
            context["semantic_query_id"] = semantic_query_id
            result = replace(result, context=context)
        normalized = _provider_result(capability, (), result)
        if capability == "graph.search_assets":
            total_count = int(context.get("matched_total") or 0)
            included_count = int(context.get("returned_count") or 0)
        else:
            total_count = int(context.get("count") or 0)
            included_count = int(context.get("retrieved_node_count") or 0)
        truncated = bool(context.get("truncated", False))
        structured_evidence = structured_evidence_from_context(
            capability,
            context,
            limitations=tuple(normalized.limitations),
        )
        return replace(
            normalized,
            total_count=total_count,
            included_count=included_count,
            omitted_count=max(0, total_count - included_count),
            truncated=truncated,
            completeness="partial" if truncated else normalized.completeness,
            evidence_type=(
                "graph_asset_search"
                if capability == "graph.search_assets"
                else "graph_asset_aggregate"
            ),
            normalized_query_hash=(
                structured_evidence.query_identity if structured_evidence else ""
            ),
            context_identity=(
                structured_evidence.query_identity if structured_evidence else ""
            ),
            structured_asset_set=structured_evidence,
            semantic_query_id=(
                structured_evidence.semantic_query_id if structured_evidence else ""
            ),
        )

    def structured_search(payload: AssetSearchCapabilityInput) -> ToolResult:
        result = graph_provider.search_assets(
            payload.to_request(),
        )
        return structured_result(
            "graph.search_assets",
            result,
            semantic_query_id=payload.semantic_query_id,
        )

    def structured_aggregate(payload: AssetAggregateCapabilityInput) -> ToolResult:
        result = graph_provider.aggregate_assets(
            payload.to_request(),
        )
        return structured_result(
            "graph.aggregate_assets",
            result,
            semantic_query_id=payload.semantic_query_id,
        )

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
        query_hash = knowledge_query_hash(payload.query)
        purpose = normalize_purpose(payload.purpose, "general_reference")
        result: KnowledgeSearchResult = knowledge_service.search(
            payload.query,
            top_k=payload.top_k,
            filters=payload.filters,
            request_id=payload.request_id,
        )
        logger.info(
            "event=knowledge_capability_result request_id=%s purpose=%s normalized_query_hash=%s status=%s included=%s truncated=%s",
            payload.request_id,
            purpose,
            query_hash,
            result.status,
            result.included_count,
            result.truncated,
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
            purpose=purpose,
            normalized_query_hash=query_hash,
        )

    specs = (
        ("asset.get_profile", "Current Product asset profile evidence.", EntityInput, (1, 1), "operational_product", "product", 0, profile),
        ("asset.get_detection", "Current Product asset-detection evidence.", EntityInput, (1, 1), "operational_product", "product", 0, detection),
        ("graph.get_summary", "Observed topology summary for one asset.", EntityInput, (1, 1), "graph_topology", "graph", 0, graph("graph.get_summary")),
        ("graph.get_neighbors", "Bounded observed neighbors for one asset.", EntityInput, (1, 1), "graph_topology", "graph", 2, graph("graph.get_neighbors")),
        ("graph.get_relationship", "Observed direct relationship for two assets.", EntityInput, (2, 2), "graph_topology", "graph", 1, graph("graph.get_relationship")),
        ("graph.compare_assets", "Bounded topology comparison for two assets.", EntityInput, (2, 2), "graph_topology", "graph", 1, graph("graph.compare_assets")),
        ("graph.find_path", "Bounded observed graph path for two assets.", EntityInput, (2, 2), "graph_topology", "graph", 2, graph("graph.find_path")),
        (
            "graph.search_assets",
            "Discover a bounded Asset set from the active Neo4j organizational projection using exact and range selectors.",
            AssetSearchCapabilityInput,
            (0, 0),
            "organizational_asset_set",
            "graph",
            0,
            structured_search,
        ),
        (
            "graph.aggregate_assets",
            "Count or bounded-group Assets in the active Neo4j organizational projection using exact and range selectors.",
            AssetAggregateCapabilityInput,
            (0, 0),
            "organizational_asset_set",
            "graph",
            0,
            structured_aggregate,
        ),
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
        elif name in {"graph.search_assets", "graph.aggregate_assets"}:
            timeout_seconds = min(30.0, float(getattr(graph_settings, "agent_request_timeout_seconds", 30)))
            maximum_result_scope = int(getattr(graph_settings, "graph_asset_search_max_limit", 200))
        else:
            timeout_seconds = min(30.0, float(getattr(graph_settings, "agent_request_timeout_seconds", 30)))
            maximum_result_scope = int(getattr(graph_settings, "graph_full_neighbors_hard_max", 5000))
        allowed_scopes = {
            "graph.get_summary": ("node_summary",),
            "graph.get_neighbors": ("one_hop", "two_hop", "full_neighbors"),
            "graph.get_relationship": ("one_hop",),
            "graph.compare_assets": ("multi_entity_comparison",),
            "graph.find_path": ("path",),
        }.get(name, ())
        allowed_depths = {
            "graph.get_summary": (0,),
            "graph.get_neighbors": (1, 2),
            "graph.get_relationship": (1,),
            "graph.compare_assets": (1,),
            "graph.find_path": (0,),
        }.get(name, ())
        planner_arguments = (
            ("entities", "views", "detail", "max_context_tokens", "purpose")
            if group == "product"
            else ("filters", "sort", "direction", "limit", "semantic_query_id")
            if name == "graph.search_assets"
            else ("filters", "operation", "group_by", "group_by_fields", "limit", "semantic_query_id")
            if name == "graph.aggregate_assets"
            else ("entities",)
            if group == "graph"
            else ("query", "top_k", "filters", "purpose", "max_context_tokens")
        )
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
                allowed_arguments=planner_arguments,
                allowed_views=approved_views("asset_profile" if name == "asset.get_profile" else "detection")
                if group == "product"
                else (),
                allowed_detail_levels=("brief", "standard", "deep") if group == "product" else (),
                allowed_purposes=(
                    "general_reference",
                    "interpret_evidence",
                    "recommended_response_actions",
                    "response_actions",
                    "investigation_procedure",
                    "hardening_guidance",
                ) if group == "knowledge" else (),
                allowed_scopes=allowed_scopes,
                allowed_depths=allowed_depths,
                reusable_locally=group == "product",
                parallelization="serialized_per_product" if group == "product" else "independent_when_dependencies_allow",
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
