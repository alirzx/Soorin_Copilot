"""Stable identities for independently reviewable model-context results."""

from __future__ import annotations

from typing import Any, Iterable


COMMUTATIVE_GRAPH_CAPABILITIES = {"graph.compare_assets"}


def context_result_identity(
    capability: str,
    entities: Iterable[str],
    *,
    scope: str = "",
    purpose: str = "",
    query_hash: str = "",
) -> str:
    values = tuple(str(value) for value in entities if value)
    if capability in COMMUTATIVE_GRAPH_CAPABILITIES:
        values = tuple(sorted(values))
    entity_part = "|".join(values)
    parts = [capability]
    if entity_part:
        parts.append(entity_part)
    if capability == "graph.get_neighbors" and scope:
        parts.append(scope)
    if capability in {"graph.search_assets", "graph.aggregate_assets"} and query_hash:
        parts.append(query_hash)
    if capability == "knowledge.search":
        if query_hash:
            parts.append(query_hash)
        if purpose:
            parts.append(purpose)
    elif capability.startswith("asset.") and purpose:
        parts.append(purpose)
    return ":".join(parts)


def identity_for_tool_result(result: Any) -> str:
    payload = result.raw_payload if isinstance(result.raw_payload, dict) else {}
    return context_result_identity(
        result.source_capability,
        result.entities,
        scope=str(payload.get("requested_scope") or payload.get("scope") or ""),
        purpose=result.purpose,
        query_hash=result.normalized_query_hash,
    )
