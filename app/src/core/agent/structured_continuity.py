"""Derive bounded thread continuity only from reviewed structured evidence."""

from __future__ import annotations

from typing import Any

from src.core.agent.contracts import (
    StructuredAssetAggregateEvidence,
    StructuredAssetSearchEvidence,
)
from src.core.agent.structured_evidence import expected_structured_query_identity
from src.core.memory.persistence import utc_now
from src.core.memory.structured_query import (
    StructuredAggregateGroupRef,
    StructuredAssetRef,
    StructuredQueryContext,
)


_VALID_RESULT_STATUSES = {"ok", "partial", "empty", "not_found"}
_VALID_REVIEW_OUTCOMES = {"sufficient", "answer_with_limitations"}


def structured_query_context_from_state(state: dict[str, Any]) -> StructuredQueryContext | None:
    """Build continuity from the one validated task/result receipt, never prose."""
    task = state.get("task")
    query = getattr(task, "structured_query", None)
    review = state.get("review_decision")
    if query is None or getattr(review, "outcome", None) not in _VALID_REVIEW_OUTCOMES:
        return None
    for result in reversed(tuple(state.get("tool_results") or ())):
        evidence = getattr(result, "structured_asset_set", None)
        if evidence is None or getattr(result, "status", None) not in _VALID_RESULT_STATUSES:
            continue
        if evidence.query_identity != expected_structured_query_identity(query, evidence):
            continue
        if isinstance(evidence, StructuredAssetSearchEvidence):
            if (
                query.mode.value != "search"
                or result.source_capability != "graph.search_assets"
                or evidence.returned_count != len(evidence.rows)
                or evidence.matched_total < evidence.returned_count
            ):
                continue
            refs: list[StructuredAssetRef] = []
            invalid_ref_count = 0
            for row in evidence.rows:
                try:
                    refs.append(
                        StructuredAssetRef(
                            ip=row.get("ip"),
                            graph_key=row.get("graph_key"),
                            display_name=row.get("asset_name"),
                        )
                    )
                except (TypeError, ValueError):
                    invalid_ref_count += 1
            return StructuredQueryContext.create(
                query_identity=evidence.query_identity,
                active_graph_version=evidence.active_graph_version,
                query=query,
                matched_total=evidence.matched_total,
                returned_count=evidence.returned_count,
                retrieval_truncated=evidence.truncated,
                continuity_truncated=bool(invalid_ref_count),
                result_refs=tuple(refs),
                source_request_id=str(state.get("request_id") or ""),
                retrieved_at=evidence.retrieved_at,
                created_at=utc_now(),
            )
        if isinstance(evidence, StructuredAssetAggregateEvidence):
            if (
                query.mode.value != "aggregate"
                or result.source_capability != "graph.aggregate_assets"
                or evidence.count < 0
            ):
                continue
            try:
                groups = tuple(
                    StructuredAggregateGroupRef(
                        value=group.get("value"),
                        count=group.get("count"),
                    )
                    for group in evidence.groups
                )
            except (TypeError, ValueError):
                continue
            return StructuredQueryContext.create(
                query_identity=evidence.query_identity,
                active_graph_version=evidence.active_graph_version,
                query=query,
                count=evidence.count,
                retrieval_truncated=evidence.truncated,
                aggregate_groups=groups,
                source_request_id=str(state.get("request_id") or ""),
                retrieved_at=evidence.retrieved_at,
                created_at=utc_now(),
            )
    return None
