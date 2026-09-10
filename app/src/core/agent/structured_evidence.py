"""Deterministic normalization for structured Asset-set evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.core.agent.contracts import (
    StructuredAssetAggregateEvidence,
    StructuredAssetSearchEvidence,
    StructuredAssetSetEvidence,
)
from src.core.graph.structured import StructuredQuerySpec


def structured_query_identity(
    query: StructuredQuerySpec | dict[str, Any],
    *,
    active_graph_version: str | None,
) -> str:
    """Hash only normalized semantic query fields and projection version."""
    if isinstance(query, StructuredQuerySpec):
        serialized = query.model_dump(mode="json", exclude_none=True)
    else:
        serialized = {key: value for key, value in query.items() if value is not None}
    mode = str(serialized.get("mode") or "")
    payload = {
        "mode": mode,
        "filters": dict(serialized.get("filters") or {}),
    }
    if mode == "search":
        payload.update(
            sort=str(serialized.get("sort") or "graph_key"),
            direction=str(serialized.get("direction") or "asc"),
        )
    elif mode == "aggregate":
        payload.update(
            operation=str(serialized.get("operation") or "count"),
            group_by=serialized.get("group_by"),
        )
    canonical = json.dumps(
        {
            "active_graph_version": active_graph_version,
            "query": payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "structured-asset-set:v1:" + hashlib.sha256(canonical).hexdigest()


def structured_evidence_from_context(
    capability: str,
    context: dict[str, Any],
    *,
    limitations: tuple[str, ...] = (),
) -> StructuredAssetSetEvidence | None:
    """Create a typed evidence envelope from the allow-listed provider context."""
    active_graph_version = _optional_string(context.get("active_graph_version"))
    filters = {
        str(key): value
        for key, value in dict(context.get("filters") or {}).items()
        if value is not None
    }
    retrieved_at = str(context.get("retrieved_at") or "")
    if capability == "graph.search_assets":
        query = {
            "mode": "search",
            "filters": filters,
            "sort": str(context.get("sort") or "graph_key"),
            "direction": str(context.get("direction") or "asc"),
        }
        rows = tuple(dict(row) for row in (context.get("rows") or ()) if isinstance(row, dict))
        return StructuredAssetSearchEvidence(
            capability="graph.search_assets",
            query_identity=structured_query_identity(query, active_graph_version=active_graph_version),
            normalized_filters=filters,
            active_graph_version=active_graph_version,
            sort=query["sort"],
            direction=query["direction"],
            matched_total=int(context.get("matched_total") or 0),
            returned_count=int(context.get("returned_count") or 0),
            truncated=bool(context.get("truncated", False)),
            rows=rows,
            retrieved_at=retrieved_at,
            limitations=limitations,
        )
    if capability == "graph.aggregate_assets":
        query = {
            "mode": "aggregate",
            "filters": filters,
            "operation": str(context.get("operation") or "count"),
            "group_by": context.get("group_by"),
        }
        groups = tuple(dict(group) for group in (context.get("groups") or ()) if isinstance(group, dict))
        return StructuredAssetAggregateEvidence(
            capability="graph.aggregate_assets",
            query_identity=structured_query_identity(query, active_graph_version=active_graph_version),
            normalized_filters=filters,
            active_graph_version=active_graph_version,
            operation=query["operation"],
            group_by=_optional_string(query.get("group_by")),
            count=int(context.get("count") or 0),
            groups=groups,
            truncated=bool(context.get("truncated", False)),
            retrieved_at=retrieved_at,
            limitations=limitations,
        )
    return None


def expected_structured_query_identity(
    query: StructuredQuerySpec,
    evidence: StructuredAssetSetEvidence,
) -> str:
    """Bind task semantics to the graph version reported by its evidence."""
    return structured_query_identity(
        query,
        active_graph_version=evidence.active_graph_version,
    )


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None
