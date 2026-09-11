"""Phase 4B.4 bounded structured-query continuity contracts."""

from __future__ import annotations

import json

from src.core.agent.structured_evidence import structured_query_identity
from src.core.graph.structured import StructuredQuerySpec
from src.core.identity import RequestIdentity
from src.core.memory.episodes import MemoryContextKey, TurnReference, WorkingMemory
from src.core.memory.persistence import (
    MAX_THREAD_STATE_BYTES,
    THREAD_STATE_SCHEMA_VERSION,
    ThreadMemoryState,
)
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.structured_query import (
    MAX_STRUCTURED_QUERY_RESULT_REFS,
    StructuredAggregateGroupRef,
    StructuredAssetRef,
    StructuredQueryContext,
)


def _identity() -> RequestIdentity:
    return RequestIdentity.resolve(
        conversation_id="conversation-a",
        session_id="session-a",
        request_id="request-a",
        user_id="user-a",
    )


def _search_context(
    *,
    version: str = "graph-v7",
    matched_total: int = 12,
    retrieval_truncated: bool = True,
    refs: tuple[StructuredAssetRef, ...] | None = None,
) -> StructuredQueryContext:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"role": "Domain Controller"},
        "sort": "asset_name",
        "direction": "asc",
        "limit": 50,
    })
    refs = refs or tuple(
        StructuredAssetRef(
            ip=f"192.0.2.{index}",
            graph_key=f"asset-{index}",
            display_name=f"dc-{index}",
        )
        for index in range(1, 4)
    )
    return StructuredQueryContext.create(
        query_identity=structured_query_identity(query, active_graph_version=version),
        active_graph_version=version,
        query=query,
        matched_total=matched_total,
        returned_count=len(refs),
        retrieval_truncated=retrieval_truncated,
        result_refs=refs,
        source_request_id="request-a",
        retrieved_at="2026-09-11T00:00:00+00:00",
        created_at="2026-09-11T00:00:01+00:00",
    )


def test_structured_query_context_is_small_and_independent_from_search_limit() -> None:
    refs = tuple(
        StructuredAssetRef(
            ip=f"198.51.100.{index}",
            graph_key="g" * 128,
            display_name="d" * 128,
        )
        for index in range(1, 21)
    )

    context = _search_context(refs=refs)

    assert len(context.result_refs) == MAX_STRUCTURED_QUERY_RESULT_REFS == 8
    assert context.query.limit == 50
    assert context.continuity_truncated
    assert len(json.dumps(context.to_payload()).encode("utf-8")) < 4_096


def test_result_fingerprint_is_stable_and_binds_bounded_snapshot() -> None:
    original = _search_context()
    replay = StructuredQueryContext.from_payload(original.to_payload())
    reversed_refs = _search_context(refs=tuple(reversed(original.result_refs)))
    changed_version = _search_context(version="graph-v8")
    changed_count = _search_context(matched_total=13)
    changed_truncation = _search_context(retrieval_truncated=False)

    assert replay.result_fingerprint == original.result_fingerprint
    assert len({
        original.result_fingerprint,
        reversed_refs.result_fingerprint,
        changed_version.result_fingerprint,
        changed_count.result_fingerprint,
        changed_truncation.result_fingerprint,
    }) == 5


def test_aggregate_context_roundtrips_without_entity_refs() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "filters": {"role": "Domain Controller"},
        "operation": "group_count",
        "group_by": "status",
    })
    context = StructuredQueryContext.create(
        query_identity=structured_query_identity(query, active_graph_version="graph-v7"),
        active_graph_version="graph-v7",
        query=query,
        count=12,
        aggregate_groups=(
            StructuredAggregateGroupRef(value="CONFIRMED", count=9),
            StructuredAggregateGroupRef(value="REVIEW", count=3),
        ),
        source_request_id="request-a",
        retrieved_at="2026-09-11T00:00:00+00:00",
        created_at="2026-09-11T00:00:01+00:00",
    )

    restored = StructuredQueryContext.from_payload(context.to_payload())

    assert restored == context
    assert restored.result_refs == ()
    assert [item.value for item in restored.aggregate_groups] == ["CONFIRMED", "REVIEW"]


def test_thread_state_roundtrips_context_and_old_v3_v4_load_without_it() -> None:
    identity = _identity()
    context = _search_context()
    state = ThreadMemoryState.from_routing_state(
        identity,
        SessionRoutingState(
            active_entities=("203.0.113.8",),
            structured_query_context=context,
        ),
    )
    payload = state.to_payload()

    restored = ThreadMemoryState.from_payload(
        payload,
        thread_key=identity.thread_key,
        user_id=identity.user_id,
        conversation_id=identity.conversation_id,
        session_id=identity.session_id,
        updated_at=state.updated_at,
        revision=1,
        schema_version=THREAD_STATE_SCHEMA_VERSION,
    )

    assert restored.structured_query_context == context
    assert restored.to_routing_state().structured_query_context == context
    legacy_payload = dict(payload)
    legacy_payload.pop("structured_query_context")
    for legacy_version in (3, 4):
        legacy = ThreadMemoryState.from_payload(
            legacy_payload,
            thread_key=identity.thread_key,
            user_id=identity.user_id,
            conversation_id=identity.conversation_id,
            session_id=identity.session_id,
            updated_at=state.updated_at,
            revision=1,
            schema_version=legacy_version,
        )
        assert legacy.active_entities == ("203.0.113.8",)
        assert legacy.structured_query_context is None


def test_bad_optional_fingerprint_is_dropped_without_losing_core_state() -> None:
    identity = _identity()
    state = ThreadMemoryState.from_routing_state(
        identity,
        SessionRoutingState(
            active_entities=("203.0.113.8",),
            structured_query_context=_search_context(),
        ),
    )
    payload = state.to_payload()
    payload["structured_query_context"]["result_fingerprint"] = (
        "structured-query-context:v1:" + ("0" * 64)
    )

    restored = ThreadMemoryState.from_payload(
        payload,
        thread_key=identity.thread_key,
        user_id=identity.user_id,
        conversation_id=identity.conversation_id,
        session_id=identity.session_id,
        updated_at=state.updated_at,
        revision=1,
        schema_version=THREAD_STATE_SCHEMA_VERSION,
    )

    assert restored.active_entities == ("203.0.113.8",)
    assert restored.structured_query_context is None


def test_thread_size_pressure_sheds_optional_structured_context_first() -> None:
    identity = _identity()
    key = MemoryContextKey(
        entities=("203.0.113.8",),
        topic_family="asset_investigation",
        relationship_mode="none",
        scope_family="asset",
    )
    references = tuple(
        TurnReference(
            request_id=f"request-{index}",
            context_key=key,
            user_digest="u" * 360,
            assistant_digest="a" * 560,
        )
        for index in range(10)
    )
    large_refs = tuple(
        StructuredAssetRef(
            ip=f"198.51.100.{index}",
            graph_key="g" * 128,
            display_name="d" * 128,
        )
        for index in range(1, 9)
    )
    state = ThreadMemoryState.from_routing_state(
        identity,
        SessionRoutingState(structured_query_context=_search_context(refs=large_refs)),
        working_memory=WorkingMemory(
            session_id=identity.session_id,
            context_key=key,
            episode_id="episode-a",
            compact_summary="s" * 3_000,
        ),
        recent_turn_references=references,
    )

    payload = state.to_payload()

    assert len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) <= MAX_THREAD_STATE_BYTES
    assert payload["working_memory"]["compact_summary"] == "s" * 3_000
    assert len(payload["recent_turn_references"]) == 10
    retained = payload.get("structured_query_context")
    assert retained is None or len(retained["result_refs"]) < MAX_STRUCTURED_QUERY_RESULT_REFS
