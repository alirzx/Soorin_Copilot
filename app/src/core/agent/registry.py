"""Typed registry for approved read-only semantic capabilities."""

from __future__ import annotations

from dataclasses import dataclass
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


class EntityInput(BaseModel):
    entities: list[str] = Field(default_factory=list, max_length=2)
    request_id: str = ""
    session_id: str = ""
    route: Any | None = None


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
    status = str(getattr(result, "status", "unavailable"))
    payload = getattr(result, "raw_payload", None)
    if payload is None:
        payload = getattr(result, "context", None)
    limitations = tuple(getattr(result, "limitations", ()) or ())
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
    return ToolResult(
        status=status if status in {"ok", "empty", "not_configured", "unavailable", "invalid", "partial", "not_found"} else "ok",  # type: ignore[arg-type]
        entities=entities,
        source_capability=capability,
        retrieved_at=_now(),
        freshness="current" if status in {"available", "ok"} else "unknown",
        completeness="complete" if complete else "partial",
        facts=(EvidenceFact(capability, "Provider evidence payload", payload, entities[0] if len(entities) == 1 else None),)
        if payload is not None
        else (),
        limitations=limitations,
        total_count=1 if payload is not None else 0,
        included_count=1 if payload is not None else 0,
        truncated=not complete,
        error_classification=getattr(result, "error_type", None) or getattr(result, "error_reason", None),
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
            target = getattr(payload.route, "target_entity", None)
            result = graph_provider.provide(target, route=payload.route, request_id=payload.request_id)
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
        )

    specs = (
        ("asset.get_profile", EntityInput, (1, 1), profile),
        ("asset.get_detection", EntityInput, (1, 1), detection),
        ("graph.get_summary", EntityInput, (1, 1), graph("graph.get_summary")),
        ("graph.get_neighbors", EntityInput, (1, 1), graph("graph.get_neighbors")),
        ("graph.get_relationship", EntityInput, (2, 2), graph("graph.get_relationship")),
        ("graph.compare_assets", EntityInput, (2, 2), graph("graph.compare_assets")),
        ("graph.find_path", EntityInput, (2, 2), graph("graph.find_path")),
        ("knowledge.search", KnowledgeInput, (0, 0), knowledge),
    )
    for name, input_schema, cardinality, handler in specs:
        registry.register(
            CapabilitySpec(
                name=name,
                version="1.0",
                input_schema=input_schema,
                output_schema=ToolResult,
                required_entity_cardinality=cardinality,
                read_only=True,
                timeout_seconds=30.0,
                retry_policy=RetryPolicy(max_retries=0),
                planner_visible=True,
            ),
            handler,
        )
    return registry
