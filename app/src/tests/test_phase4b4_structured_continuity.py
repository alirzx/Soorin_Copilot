"""Phase 4B.4 bounded structured-query continuity contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataclasses import replace

from src.core.agent.contracts import (
    ReviewDecision,
    ExecutionPlan,
    PlanStep,
    StructuredAssetAggregateEvidence,
    StructuredAssetSearchEvidence,
    TaskSpec,
    ToolResult,
)
from src.core.agent.structured_continuity import structured_query_context_from_state
from src.core.agent.structured_evidence import structured_query_identity
from src.core.graph.structured import StructuredQuerySpec
from src.core.identity import RequestIdentity
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.agent.evidence_policy import EvidenceRequirementPolicy, MemorySufficiencyGate, build_gap_plan
from src.core.context.models import EntityResolution, RouteDecision
from src.core.memory.episodes import MemoryContextKey, TurnReference, WorkingMemory
from src.core.memory.persistence import (
    MAX_THREAD_STATE_BYTES,
    THREAD_STATE_SCHEMA_VERSION,
    ThreadMemoryState,
)
from src.core.memory.persistence import LocalPersistenceOwnershipError
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.routing_state import SessionRoutingStateStore
from src.core.memory.store import MemoryStore
from src.core.memory.sqlite import LocalSQLiteDatabase, SQLiteThreadStateStore
from src.config.settings import get_settings
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
    changed_query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"role": "Domain Controller", "status": "CONFIRMED"},
        "sort": "asset_name",
        "direction": "asc",
        "limit": 50,
    })
    changed_identity = StructuredQueryContext.create(
        query_identity=structured_query_identity(
            changed_query,
            active_graph_version=original.active_graph_version,
        ),
        active_graph_version=original.active_graph_version,
        query=changed_query,
        matched_total=original.matched_total,
        returned_count=original.returned_count,
        retrieval_truncated=original.retrieval_truncated,
        result_refs=original.result_refs,
        source_request_id=original.source_request_id,
        retrieved_at=original.retrieved_at,
        created_at=original.created_at,
    )

    assert replay.result_fingerprint == original.result_fingerprint
    assert len({
        original.result_fingerprint,
        reversed_refs.result_fingerprint,
        changed_version.result_fingerprint,
        changed_count.result_fingerprint,
        changed_truncation.result_fingerprint,
        changed_identity.result_fingerprint,
    }) == 6


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


def test_mismatched_optional_query_identity_is_dropped_without_losing_core_state() -> None:
    identity = _identity()
    state = ThreadMemoryState.from_routing_state(
        identity,
        SessionRoutingState(
            active_entities=("203.0.113.8",),
            structured_query_context=_search_context(),
        ),
    )
    payload = state.to_payload()
    payload["structured_query_context"]["query"]["filters"]["status"] = "CONFIRMED"

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


def test_reviewed_search_evidence_creates_context_including_valid_empty_result() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED"},
    })
    identity = structured_query_identity(query, active_graph_version="graph-v1")
    evidence = StructuredAssetSearchEvidence(
        capability="graph.search_assets",
        query_identity=identity,
        normalized_filters={"status": "CONFIRMED"},
        active_graph_version="graph-v1",
        sort="graph_key",
        direction="asc",
        matched_total=0,
        returned_count=0,
        truncated=False,
        rows=(),
        retrieved_at="2026-09-11T00:00:00+00:00",
    )
    result = ToolResult(
        status="not_found",
        entities=(),
        source_capability="graph.search_assets",
        retrieved_at=evidence.retrieved_at,
        freshness="current",
        completeness="complete",
        structured_asset_set=evidence,
    )
    state = {
        "task": TaskSpec(
            request="show confirmed assets",
            intent="asset_search",
            scope="none",
            direction="none",
            entities=(),
            required_capabilities=("graph.search_assets",),
            structured_query=query,
        ),
        "review_decision": ReviewDecision(outcome="sufficient"),
        "tool_results": [result],
        "request_id": "request-empty",
    }

    context = structured_query_context_from_state(state)

    assert context is not None
    assert context.matched_total == 0
    assert context.result_refs == ()


def test_failed_mismatched_or_rejected_evidence_cannot_replace_context() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "filters": {"role": "Domain Controller"},
        "operation": "count",
    })
    identity = structured_query_identity(query, active_graph_version="graph-v1")
    evidence = StructuredAssetAggregateEvidence(
        capability="graph.aggregate_assets",
        query_identity=identity,
        normalized_filters={"role": "Domain Controller"},
        active_graph_version="graph-v1",
        operation="count",
        group_by=None,
        count=0,
        groups=(),
        truncated=False,
        retrieved_at="2026-09-11T00:00:00+00:00",
    )
    result = ToolResult(
        status="ok",
        entities=(),
        source_capability="graph.aggregate_assets",
        retrieved_at=evidence.retrieved_at,
        freshness="current",
        completeness="complete",
        structured_asset_set=evidence,
    )
    base = {
        "task": TaskSpec(
            request="count Domain Controllers",
            intent="asset_aggregate",
            scope="none",
            direction="none",
            entities=(),
            required_capabilities=("graph.aggregate_assets",),
            structured_query=query,
        ),
        "review_decision": ReviewDecision(outcome="sufficient"),
        "tool_results": [result],
        "request_id": "request-count",
    }

    valid = structured_query_context_from_state(base)
    failed = structured_query_context_from_state({**base, "tool_results": [replace(result, status="unavailable")]})
    mismatched = structured_query_context_from_state({
        **base,
        "tool_results": [replace(
            result,
            structured_asset_set=replace(evidence, query_identity="structured-asset-set:v1:" + ("0" * 64)),
        )],
    })
    rejected = structured_query_context_from_state({
        **base,
        "review_decision": ReviewDecision(outcome="missing_required_evidence"),
    })

    assert valid is not None and valid.count == 0
    assert failed is None
    assert mismatched is None
    assert rejected is None


def test_update_memory_writes_context_without_turning_rows_into_active_entities_or_ltm() -> None:
    identity = _identity()
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"role": "Domain Controller"},
    })
    query_identity = structured_query_identity(query, active_graph_version="graph-v2")
    evidence = StructuredAssetSearchEvidence(
        capability="graph.search_assets",
        query_identity=query_identity,
        normalized_filters={"role": "Domain Controller"},
        active_graph_version="graph-v2",
        sort="graph_key",
        direction="asc",
        matched_total=2,
        returned_count=2,
        truncated=False,
        rows=(
            {"ip": "192.0.2.20", "graph_key": "asset-20", "asset_name": "dc-20"},
            {"ip": "192.0.2.21", "graph_key": "asset-21", "asset_name": "dc-21"},
        ),
        retrieved_at="2026-09-11T00:00:00+00:00",
    )
    result = ToolResult(
        status="ok",
        entities=(),
        source_capability="graph.search_assets",
        retrieved_at=evidence.retrieved_at,
        freshness="current",
        completeness="complete",
        structured_asset_set=evidence,
    )
    persisted: list[SessionRoutingState] = []

    class _NoLTM:
        def process_candidate(self, *_args, **_kwargs):
            raise AssertionError("structured query continuity must not become LTM")

    service = SimpleNamespace(
        settings=replace(get_settings(), chat_store_history=False),
        memory_store=MemoryStore(20),
        routing_state_store=SessionRoutingStateStore(),
        long_term_memory_coordinator=_NoLTM(),
        persist_thread_continuity=lambda _identity, state: persisted.append(state),
        persist_completed_local_turn=lambda *_args, **_kwargs: None,
    )
    task = TaskSpec(
        request="show Domain Controllers",
        intent="asset_search",
        scope="none",
        direction="none",
        entities=(),
        required_capabilities=("graph.search_assets",),
        structured_query=query,
    )
    state = {
        "task": task,
        "tool_results": [result],
        "synthesis_result": {"answer": "two results"},
        "memory_context_key": MemoryContextKey.from_task(task),
        "pending_working_facts": (),
        "session_id": identity.session_id,
        "request_id": identity.request_id,
        "request_identity": identity,
        "message": task.request,
        "evidence_pack": SimpleNamespace(limitations=()),
        "active_entity_state": SessionRoutingState(active_entities=("203.0.113.8",)),
        "resolved_entities": EntityResolution(status="none"),
        "routing_result": RouteDecision(
            use_graph=True,
            reason="structured",
            intent="asset_search",
            structured_query=query,
        ),
        "execution_plan": ExecutionPlan(
            task=task,
            steps=(PlanStep("search", "graph.search_assets"),),
            plan_id="structured-plan",
        ),
        "review_decision": ReviewDecision(outcome="sufficient"),
        "turn_policy": SimpleNamespace(
            episode_transition="keep",
            operation="follow_up",
            operational_state_mutation_allowed=True,
        ),
    }

    updated = CopilotWorkflowNodes(service).update_memory(state)["active_entity_state"]

    assert updated.active_entities == ("203.0.113.8",)
    assert updated.active_ip == "203.0.113.8"
    assert updated.structured_query_context is not None
    assert [item.ip for item in updated.structured_query_context.result_refs] == [
        "192.0.2.20", "192.0.2.21"
    ]
    assert persisted == [updated]


def test_structured_context_survives_owned_sqlite_restore_and_is_cross_thread_isolated(
    tmp_path: Path,
) -> None:
    identity = _identity()
    database = LocalSQLiteDatabase(tmp_path / "structured-continuity.sqlite3")
    database.initialize()
    store = SQLiteThreadStateStore(database)
    state = ThreadMemoryState.from_routing_state(
        identity,
        SessionRoutingState(structured_query_context=_search_context()),
    )
    store.save(identity=identity, state=state, expected_revision=0)

    restored = store.load(identity=identity)
    other_thread = RequestIdentity.resolve(
        user_id="user-a",
        conversation_id="conversation-b",
        session_id="session-b",
        request_id="request-b",
    )
    wrong_owner = RequestIdentity.resolve(
        user_id="user-b",
        conversation_id="conversation-a",
        session_id="session-a",
        request_id="request-c",
    )

    assert restored is not None and restored.structured_query_context is not None
    assert store.load(identity=other_thread) is None
    with pytest.raises(LocalPersistenceOwnershipError):
        store.load(identity=wrong_owner)


def test_corrupted_persisted_ref_drops_only_optional_context() -> None:
    identity = _identity()
    state = ThreadMemoryState.from_routing_state(
        identity,
        SessionRoutingState(
            active_entities=("203.0.113.8",),
            structured_query_context=_search_context(),
        ),
    )
    payload = state.to_payload()
    payload["structured_query_context"]["result_refs"][0]["ip"] = "not-an-ip"

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


def test_structured_context_never_satisfies_gate8_or_skips_current_capabilities() -> None:
    context = _search_context()
    tasks = (
        TaskSpec(
            request="rerun the set",
            intent="asset_search",
            scope="none",
            direction="none",
            entities=(),
            required_capabilities=("graph.search_assets",),
            structured_query=context.query,
        ),
        TaskSpec(
            request="inspect selected Asset",
            intent="asset_investigation",
            scope="node_summary",
            direction="both",
            entities=(context.result_refs[0].ip,),
            required_capabilities=("asset.get_profile", "asset.get_detection"),
        ),
    )

    for task in tasks:
        requirements = EvidenceRequirementPolicy().derive(task)
        decisions = MemorySufficiencyGate().evaluate(requirements, ())
        gap = build_gap_plan(requirements, decisions)
        assert gap.skipped_capabilities == ()
        assert {item.requirement.capability for item in gap.gaps} == set(
            task.required_capabilities
        )


def test_old_graph_version_ref_can_identify_asset_but_current_set_remains_live_direct() -> None:
    context = _search_context(version="graph-v1")
    selected = context.result_refs[0]
    current_query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"role": "Domain Controller"},
    })
    current_task = TaskSpec(
        request="show the current matching set",
        intent="asset_search",
        scope="none",
        direction="none",
        entities=(),
        required_capabilities=("graph.search_assets",),
        structured_query=current_query,
        workflow_mode="direct",
    )

    assert selected.ip == "192.0.2.1"
    assert context.active_graph_version == "graph-v1"
    requirements = EvidenceRequirementPolicy().derive(current_task)
    gap = build_gap_plan(requirements, MemorySufficiencyGate().evaluate(requirements, ()))
    assert tuple(item.requirement.capability for item in gap.gaps) == ("graph.search_assets",)
    assert current_task.workflow_mode == "direct"


def test_aggregate_group_continuity_is_bounded_and_historical_payload_is_explicit() -> None:
    query = StructuredQuerySpec.model_validate({
        "mode": "aggregate",
        "filters": {"role": "Domain Controller"},
        "operation": "group_count",
        "group_by": "status",
    })
    groups = tuple(
        StructuredAggregateGroupRef(value=f"group-{index}", count=index)
        for index in range(20)
    )
    context = StructuredQueryContext.create(
        query_identity=structured_query_identity(query, active_graph_version="graph-v1"),
        active_graph_version="graph-v1",
        query=query,
        count=190,
        aggregate_groups=groups,
        source_request_id="aggregate-groups",
        retrieved_at="2026-09-11T00:00:00+00:00",
        created_at="2026-09-11T00:00:01+00:00",
    )
    historical = context.historical_context_payload()

    assert len(context.aggregate_groups) == 8
    assert context.continuity_truncated
    assert context.result_refs == ()
    assert "Historical structured-query continuity only" in historical["authority"]
    assert "not current operational evidence" in historical["authority"]
