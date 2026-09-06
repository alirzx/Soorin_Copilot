"""Focused offline tests for Gate 8 memory, views, and compaction."""

from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from src.config.settings import get_settings
from src.core.agent.contracts import (
    ExecutionPlan,
    PlanStep,
    ReviewDecision,
    TaskSpec,
    ToolResult,
)
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.agent.reviewer import EvidenceReviewer
from src.core.agent.evidence_policy import (
    EvidenceRequirementPolicy,
    MemorySufficiencyGate,
    ViewSelector,
    apply_gap_plan,
    build_gap_plan,
    evidence_refs_from_validated_result,
    structured_memory_statement,
)
from src.core.context.compaction import (
    CurrentEvidenceProjection,
    build_delta_context,
    deduplicate_payloads,
    episodic_baseline_projections,
    fingerprint,
    investigation_baseline_from_results,
)
from src.core.context.compaction import (
    HistoricalBaselineProjection,
    current_evidence_projections,
    historical_baseline_projections,
)
from src.core.context.composer import ContextComposer
from src.core.context.entities import EntityResolver
from src.core.context.models import CopilotContextPackage, EntityResolution
from src.core.context.product_views import build_product_view, payload_inventory, select_product_views
from src.core.copilot.service import CopilotService
from src.core.memory.episodes import BaselineProjection, InvestigationBaseline, MemoryContextKey
from src.core.memory.long_term import LongTermMemoryRecord, MemoryPromotionPolicy, RetrievedLongTermMemory
from src.core.memory.persistence import THREAD_STATE_SCHEMA_VERSION, ThreadMemoryState
from src.core.memory.persistence import LocalPersistenceError
from src.core.memory.retrieval import LongTermMemorySelection
from src.core.memory.routing_state import SessionRoutingState, SessionRoutingStateStore
from src.core.memory.store import MemoryStore
from src.core.identity import RequestIdentity
from src.core.product_client import ProductApiClient


IP = "192.0.2.10"


def _task(
    request: str,
    capabilities: tuple[str, ...],
    *,
    detail: str = "standard",
) -> TaskSpec:
    return TaskSpec(
        request=request,
        intent="asset_investigation",
        scope="node_summary",
        direction="both",
        entities=(IP,),
        required_capabilities=capabilities,
        detail_level=detail,
    )


def _memory(
    statement: str = "The asset is an approved domain controller.",
    *,
    entity: str = IP,
    evidence_refs: tuple[str, ...] = (
        "evidence_class_asset_identity",
        "evidence_class_asset_role",
        "complete",
    ),
    memory_type: str = "approved_asset_fact",
    freshness: str = "current",
) -> RetrievedLongTermMemory:
    record = LongTermMemoryRecord(
        memory_id=f"mem_{abs(hash((statement, entity))) % 100000}",
        memory_type=memory_type,  # type: ignore[arg-type]
        user_id="user_test",
        entity_ids=(entity,),
        statement=statement,
        epistemic_status="analyst_confirmed",
        confidence=1.0,
        source_request_id="req_test",
        source_conversation_id="conv_test",
        evidence_refs=evidence_refs,
        provenance_category="analyst",
        status="active",
    )
    return RetrievedLongTermMemory(record, 1.0, "exact_entity", freshness)  # type: ignore[arg-type]


def test_requirement_policy_and_view_selector_are_deterministic() -> None:
    policy = EvidenceRequirementPolicy()
    task = _task("Why is this classified as a domain controller?", ("asset.get_detection", "asset.get_profile"))
    requirements = policy.derive(task)
    classes = {item.evidence_class for item in requirements.requirements}

    assert {"detection_classification", "detection_explanation", "detection_contradictions"} <= classes
    detection = requirements.for_capability("asset.get_detection")
    assert ViewSelector().select(detection) == ("overview", "evidence")
    assert select_product_views("detection", "Which assets resemble it?", "standard") == ("similarity",)
    assert select_product_views("detection", "Show its cluster cohort", "standard") == ("cluster",)
    assert select_product_views("asset_profile", "Analyze authentication identity", "standard") == ("identity",)
    assert select_product_views("detection", "Give raw exhaustive behavior", "deep") == ("full",)


def test_memory_gate_applies_six_checks_and_current_refresh_policy() -> None:
    gate = MemorySufficiencyGate()
    role = EvidenceRequirementPolicy().derive(_task("What was its approved role?", ("asset.get_profile",)))
    role_requirement = next(item for item in role.requirements if item.evidence_class == "asset_role")

    sufficient = gate._evaluate_one(role_requirement, (_memory(),))
    mismatch = gate._evaluate_one(role_requirement, (_memory(entity="192.0.2.99"),))
    no_coverage = gate._evaluate_one(
        role_requirement,
        (_memory(memory_type="hypothesis_resolution", evidence_refs=("complete",)),),
    )

    assert sufficient.decision == "memory_sufficient"
    assert mismatch.reason_code == "memory_entity_mismatch"
    assert no_coverage.reason_code == "memory_partial"

    current = EvidenceRequirementPolicy().derive(
        _task("What is its current classification confidence?", ("asset.get_detection",))
    )
    current_decision = gate._evaluate_one(
        current.requirements[0],
        (_memory(evidence_refs=("evidence_class_detection_classification", "complete")),),
    )
    assert current_decision.decision == "memory_sufficient_verification_required"


@pytest.mark.parametrize("capability, evidence_ref", (
    ("asset.get_detection", "evidence_class_detection_classification"),
    ("asset.get_profile", "evidence_class_asset_identity"),
))
def test_current_comparison_never_skips_live_requirement_for_historical_memory(
    capability: str,
    evidence_ref: str,
) -> None:
    task = replace(
        _task(
            "Re-check the current state and compare it with the previously validated state.",
            (capability,),
        ),
        evidence_mode="current_verification",
    )
    requirements = EvidenceRequirementPolicy().derive(task)
    decision = MemorySufficiencyGate()._evaluate_one(
        requirements.requirements[0],
        (_memory(evidence_refs=(evidence_ref, "complete")),),
    )

    assert requirements.requirements[0].freshness_class == "current_verification"
    assert decision.decision == "memory_sufficient_verification_required"


def test_promoted_matching_memory_satisfies_but_missing_or_wrong_entity_does_not() -> None:
    requirements = EvidenceRequirementPolicy().derive(
        _task("What was its approved role?", ("asset.get_profile",))
    )
    role = next(item for item in requirements.requirements if item.evidence_class == "asset_role")
    candidate = LongTermMemoryRecord.candidate(
        memory_type="validated_finding",
        user_id="user_test",
        entity_ids=(IP,),
        statement='{"role":"domain_controller"}',
        source_request_id="req_test",
        source_conversation_id="conv_test",
        evidence_refs=("source-step", "evidence_class_asset_role"),
    )
    promoted = MemoryPromotionPolicy.promote(
        candidate,
        epistemic_status="analyst_confirmed",
        confidence=1.0,
        provenance_category="analyst",
    )
    matching = RetrievedLongTermMemory(promoted, 1.0, "exact_entity", "current")

    gate = MemorySufficiencyGate()
    assert gate._evaluate_one(role, (matching,)).decision == "memory_sufficient"
    assert gate._evaluate_one(role, (_memory(evidence_refs=("source-step",)),)).reason_code == "memory_partial"
    assert gate._evaluate_one(role, (_memory(entity="192.0.2.99"),)).reason_code == "memory_entity_mismatch"


def test_structured_result_adds_only_supported_evidence_classes_and_preserves_refs() -> None:
    requirements = EvidenceRequirementPolicy().derive(
        _task("What was its approved role?", ("asset.get_profile",))
    )
    result = ToolResult(
        status="ok",
        entities=(IP,),
        source_capability="asset.get_profile",
        retrieved_at="2026-08-08T00:00:00+00:00",
        freshness="current",
        completeness="complete",
        provider="asset_profile",
        view_payload={"views": {"overview": {"role": "domain_controller"}}},
        context_included=True,
        source_payload_complete=True,
        projection_usable=True,
        step_id="profile-step",
    )

    refs = evidence_refs_from_validated_result(
        requirements,
        result,
        existing_refs=("provider-evidence-7", "provider-evidence-7"),
    )
    statement = structured_memory_statement(result, refs)

    assert refs[0] == "provider-evidence-7"
    assert refs.count("provider-evidence-7") == 1
    assert "evidence_class_asset_identity" in refs
    assert "evidence_class_asset_role" in refs
    assert not any(ref.startswith("evidence_class_detection_") for ref in refs)
    assert statement is not None and "domain_controller" in statement


def test_workflow_proposes_candidate_from_validated_structured_evidence() -> None:
    requirements = EvidenceRequirementPolicy().derive(
        _task("What was its approved role?", ("asset.get_profile",))
    )
    result = ToolResult(
        status="ok",
        entities=(IP,),
        source_capability="asset.get_profile",
        retrieved_at="2026-08-08T00:00:00+00:00",
        freshness="current",
        completeness="complete",
        provider="asset_profile",
        view_payload={"views": {"overview": {"role": "domain_controller"}}},
        context_included=True,
        source_payload_complete=True,
        projection_usable=True,
        step_id="profile-step",
    )

    class Coordinator:
        def __init__(self) -> None:
            self.candidates: list[LongTermMemoryRecord] = []

        def create_candidate(self, memory: LongTermMemoryRecord) -> LongTermMemoryRecord:
            self.candidates.append(memory)
            return memory

    coordinator = Coordinator()
    nodes = object.__new__(CopilotWorkflowNodes)
    nodes.service = SimpleNamespace(long_term_memory_coordinator=coordinator)
    created = nodes._propose_long_term_candidates({
        "request_id": "req_test",
        "request_identity": RequestIdentity.resolve(
            user_id="user_test",
            conversation_id="conv_test",
            session_id="session_test",
            request_id="req_test",
        ),
        "evidence_requirements": requirements,
        "tool_results": [result],
    })

    assert created == 1
    assert coordinator.candidates[0].status == "candidate"
    assert "evidence_class_asset_role" in coordinator.candidates[0].evidence_refs
    assert "domain_controller" in coordinator.candidates[0].statement


def test_memory_gate_rejects_partial_exhaustive_and_contradictory_memory() -> None:
    task = _task("Give a deep exhaustive profile", ("asset.get_profile",), detail="deep")
    requirement = EvidenceRequirementPolicy().derive(task).requirements[0]
    gate = MemorySufficiencyGate()
    partial = _memory(evidence_refs=(f"evidence_class_{requirement.evidence_class}",))
    contradiction = _memory(
        "The asset is not a domain controller.",
        evidence_refs=(f"evidence_class_{requirement.evidence_class}", "complete", "contradiction"),
    )
    first = _memory(evidence_refs=(f"evidence_class_{requirement.evidence_class}", "complete"))

    assert gate._evaluate_one(requirement, (partial,)).reason_code == "memory_incomplete_live_refresh"
    assert gate._evaluate_one(requirement, (first, contradiction)).decision == "contradictory_memory"


def test_gap_plan_skips_only_fully_satisfied_call_and_records_memory_result() -> None:
    task = _task("What was its approved role?", ("asset.get_profile",))
    requirements = EvidenceRequirementPolicy().derive(task)
    decisions = MemorySufficiencyGate().evaluate(requirements, (_memory(),))
    plan = ExecutionPlan(
        task=task,
        steps=(PlanStep("profile", "asset.get_profile", arguments={"entities": [IP]}),),
        target_entities=(IP,),
    )

    updated, gap, memory_results = apply_gap_plan(plan, build_gap_plan(requirements, decisions))

    assert updated.steps == ()
    assert gap.skipped_capabilities == ("asset.get_profile",)
    assert memory_results[0].provider == "long_term_memory"
    assert memory_results[0].context_representation == "memory_reuse"


def test_profile_views_project_current_full_payload_and_bound_large_lists() -> None:
    payload = {
        "id": "asset-1", "hostname": "dc-01", "ip_address": IP, "risk_score": 77,
        "identity": {
            "kerberos": {"domain": "example.test", "is_kdc": True, "serviceClasses": list(range(30))},
            "ldap": {"isServer": True}, "ntlm": {"domain": "EXAMPLE"}, "smb": {"sysvol": True},
        },
        "trafficSeries": [{"at": index, "bytes": index * 10} for index in range(30)],
        "futureHugeField": [f"noise-{index}" for index in range(200)],
    }
    identity = build_product_view(
        payload, provider="asset_profile", views=("identity",), detail="standard",
        max_context_tokens=1000, purpose="identity",
    )
    overview = build_product_view(
        payload, provider="asset_profile", views=("overview",), detail="brief",
        max_context_tokens=1000, purpose="summary",
    )
    full = build_product_view(
        payload, provider="asset_profile", views=("full",), detail="deep",
        max_context_tokens=8000, purpose="exhaustive",
    )

    kerberos = identity.payload["views"]["identity"]["kerberos"]
    assert kerberos["serviceClasses"]["total_count"] == 30
    assert kerberos["serviceClasses"]["omitted_count"] == 18
    assert overview.payload["views"]["overview"]["risk_score"] == 77
    assert "futureHugeField" not in overview.payload["views"]["overview"]
    assert full.payload == payload
    assert identity.token_estimate < payload_inventory(payload).approx_tokens


def test_detection_views_preserve_contract_semantics_and_reduce_payload() -> None:
    full_payload = {
        "assetName": "dc-01", "ip": IP, "status": "classified", "suggestedType": "domain_controller",
        "modelConfidence": 0.98, "mappingConfidence": 0.91, "unknownScore": 0.02,
        "topPositiveFeatures": [f"feature-{index}" for index in range(40)],
        "ruleVotes": [{"rule": index, "vote": 1} for index in range(40)],
        "neighbors": [{"ip": f"192.0.2.{index}", "distance": index / 100} for index in range(30)],
        "clusterId": "role-dc", "population": 1, "purity": 1.0, "members": [{"ip": IP}],
        "rawSignals": ["x" * 100 for _ in range(200)],
    }
    overview = build_product_view(full_payload, provider="detection", views=("overview",), detail="brief", max_context_tokens=1000, purpose="classification")
    evidence = build_product_view(full_payload, provider="detection", views=("evidence",), detail="standard", max_context_tokens=1000, purpose="why")
    similarity = build_product_view(full_payload, provider="detection", views=("similarity",), detail="standard", max_context_tokens=1000, purpose="similar")
    cluster = build_product_view(full_payload, provider="detection", views=("cluster",), detail="standard", max_context_tokens=1000, purpose="cluster")
    full = build_product_view(full_payload, provider="detection", views=("full",), detail="deep", max_context_tokens=8000, purpose="raw")

    assert overview.payload["views"]["overview"]["suggested_type"] == "domain_controller"
    assert evidence.payload["views"]["evidence"]["rule_votes"]["format"] == "tsv"
    assert evidence.payload["views"]["evidence"]["rule_votes"]["omitted_count"] == 28
    assert "not model embedding-space similarity" in similarity.payload["views"]["similarity"]["semantic_limitation"]
    assert "not unsupervised ML clustering" in cluster.payload["views"]["cluster"]["semantic_limitation"]
    assert "Population one" in cluster.payload["views"]["cluster"]["semantic_limitation"]
    assert full.payload == full_payload
    assert overview.token_estimate < full.token_estimate


class _Response:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


class _Session:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def mount(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str], timeout: tuple[int, int]) -> _Response:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _Response({"assetFound": True, "ip": IP})

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: tuple[int, int],
        **_kwargs: Any,
    ) -> _Response:
        assert method == "GET"
        return self.get(url, headers=headers, timeout=timeout)


def test_detection_view_paths_reuse_product_auth_and_hwid() -> None:
    settings = replace(
        get_settings(),
        product_api_base_url="http://product.invalid",
        product_api_token="Bearer test-token",
        product_hwid="test-hwid",
        product_asset_detection_overview_path="/asset-detection/{ip}/overview",
        product_asset_detection_evidence_path="/asset-detection/{ip}/evidence",
        product_asset_detection_similarity_path="/asset-detection/{ip}/similarity",
        product_asset_detection_cluster_path="/asset-detection/{ip}/cluster",
        product_connect_timeout_seconds=1,
        product_read_timeout_seconds=1,
        product_max_retries=0,
    )
    session = _Session()
    with patch("src.core.product_client.client.requests.Session", return_value=session):
        client = ProductApiClient(settings)
    for view in ("overview", "evidence", "similarity", "cluster"):
        client.get_asset_detection(IP, view=view)

    assert [call["url"].rsplit("/", 1)[-1] for call in session.calls] == ["overview", "evidence", "similarity", "cluster"]
    assert all(call["headers"]["Authorization"] == "Bearer test-token" for call in session.calls)
    assert all(call["headers"]["x-hwid"] == "test-hwid" for call in session.calls)


def test_dedup_preserves_support_conflicts_and_timestamp_distinctions() -> None:
    result = deduplicate_payloads([
        ("profile", {"ip": IP, "role": "dc", "observedAt": "2026-01-01"}),
        ("detection", {"ip": IP, "role": "dc", "observedAt": "2026-01-02"}),
        ("memory", {"role": "server"}),
    ])

    assert result.collapsed_count == 2
    role = next(item for item in result.canonical_facts if item["fact"] == "role" and item["value"] == "dc")
    assert len(role["support"]) == 2
    assert any(item["fact"] == "role" and item["value"] == "server" for item in result.canonical_facts)
    assert result.payloads[0]["observedAt"] != result.payloads[1]["observedAt"]


def test_delta_requires_accessible_compatible_baseline() -> None:
    current = {"role": "dc", "risk": 7, "new": True}
    baseline = {"role": "dc", "risk": 4, "removed": True}

    absent = build_delta_context(current, baseline=None, current_identity=f"{IP}:overview")
    mismatch = build_delta_context(
        current, baseline=baseline, current_identity=f"{IP}:overview",
        baseline_identity="192.0.2.99:overview", baseline_accessible=True,
        baseline_schema_version="product-view-v1",
    )
    delta = build_delta_context(
        current, baseline=baseline, current_identity=f"{IP}:overview",
        baseline_identity=f"{IP}:overview", baseline_accessible=True,
        baseline_schema_version="product-view-v1",
    )

    assert absent.reason == "baseline_absent" and absent.payload == current
    assert mismatch.reason == "baseline_identity_mismatch"
    assert delta.created
    assert delta.payload["changed"] == {"risk": 7}
    assert delta.payload["new"] == {"new": True}
    assert delta.payload["removed"] == {"removed": True}


def test_offline_context_efficiency_matrix_uses_expected_minimum_views() -> None:
    profile = {
        "id": "asset-1", "hostname": "dc-01", "ip": IP, "riskScore": 88,
        "kerberos": {"domain": "example.test", "principals": list(range(100))},
        "alerts": [{"id": index, "severity": "high"} for index in range(50)],
        "openPorts": list(range(200)), "trafficSeries": [{"at": index, "bytes": index} for index in range(100)],
        "rawLogs": ["x" * 200 for _ in range(200)],
    }
    detection = {
        "ip": IP, "suggestedType": "domain_controller", "modelConfidence": 0.98,
        "topPositiveFeatures": list(range(100)),
        "ruleVotes": [{"rule": index, "vote": 1} for index in range(100)],
        "neighbors": [{"ip": f"198.51.100.{index}", "distance": index / 100} for index in range(50)],
        "clusterId": "dc", "population": 50, "members": [{"ip": f"198.51.100.{index}"} for index in range(50)],
        "rawSignals": ["x" * 200 for _ in range(200)],
    }
    cases = (
        ("asset_profile", profile, "quick asset summary", "standard", ("overview",)),
        ("detection", detection, "show classification confidence", "standard", ("overview",)),
        ("detection", detection, "why this classification and its rules", "standard", ("overview", "evidence")),
        ("asset_profile", profile, "analyze authentication identity", "standard", ("identity",)),
        ("asset_profile", profile, "analyze risk and alerts", "standard", ("security",)),
        ("detection", detection, "show similar assets", "standard", ("similarity",)),
        ("detection", detection, "show cluster cohort", "standard", ("cluster",)),
        ("asset_profile", profile, "analyze network activity", "standard", ("network", "activity")),
        ("detection", detection, "deep raw investigation", "deep", ("full",)),
    )
    for provider, payload, request, detail, expected_views in cases:
        views = select_product_views(provider, request, detail)
        projected = build_product_view(
            payload,
            provider=provider,
            views=views,
            detail=detail,
            max_context_tokens=8000,
            purpose="offline_efficiency",
        )
        assert views == expected_views
        if views == ("full",):
            assert projected.token_estimate == payload_inventory(payload).approx_tokens
        else:
            assert projected.token_estimate < payload_inventory(payload).approx_tokens


def test_reviewer_keeps_normal_caveats_without_limiting_success() -> None:
    task = _task("Check the current identity.", ("asset.get_profile",))
    result = ToolResult(
        status="ok",
        entities=(IP,),
        source_capability="asset.get_profile",
        retrieved_at="2026-08-16T00:00:00+00:00",
        freshness="current",
        completeness="complete",
        limitations=("Product evidence is point-in-time.",),
        context_included=True,
        context_representation="projected",
        source_payload_complete=True,
        projection_usable=True,
    )

    decision = EvidenceReviewer().review(task, [result])

    assert decision.outcome == "sufficient"
    assert decision.caveats == ("Product evidence is point-in-time.",)
    assert decision.material_limitations == ()


def test_reviewer_required_provider_failure_is_material() -> None:
    task = _task("Check the current identity.", ("asset.get_profile",))
    result = ToolResult(
        status="unavailable",
        entities=(IP,),
        source_capability="asset.get_profile",
        retrieved_at="2026-08-16T00:00:00+00:00",
        freshness="unknown",
        completeness="unknown",
    )

    decision = EvidenceReviewer().review(task, [result])

    assert decision.outcome == "safe_failure"
    assert decision.material_limitations


def _structured_baseline(
    *,
    entity: str = IP,
    view: str = "overview",
    schema_version: str = "product-view-v1",
    freshness: str = "historical",
    candidate: bool = False,
) -> RetrievedLongTermMemory:
    statement = json.dumps({
        "source_capability": "asset.get_profile",
        "entities": [entity],
        "evidence_classes": ["asset_identity", "asset_role"],
        "selected_views": [view],
        "schema_version": schema_version,
        "completeness": "complete",
        "projection_complete": True,
        "evidence": {
            "provider": "asset_profile",
            "views": {view: {"role": "server", "risk": 4}},
        },
    }, separators=(",", ":"), sort_keys=True)
    record = LongTermMemoryRecord.candidate(
        memory_type="validated_finding",
        user_id="user_test",
        entity_ids=(entity,),
        statement=statement,
        source_request_id="baseline-request",
        source_conversation_id="baseline-conversation",
    )
    if not candidate:
        record = MemoryPromotionPolicy.promote(
            record,
            epistemic_status="analyst_confirmed",
            confidence=1.0,
            provenance_category="analyst",
        )
    return RetrievedLongTermMemory(record, 1.0, "exact_entity", freshness)  # type: ignore[arg-type]


def _current_profile_projection() -> tuple[Any, ...]:
    result = ToolResult(
        status="ok",
        entities=(IP,),
        source_capability="asset.get_profile",
        retrieved_at="2026-08-16T00:00:00+00:00",
        freshness="current",
        completeness="complete",
        selected_views=("overview",),
        view_payload={
            "provider": "asset_profile",
            "views": {"overview": {"role": "server", "risk": 7}},
        },
        projection_schema_version="product-view-v1",
        projection_usable=True,
        source_payload_complete=True,
    )
    return current_evidence_projections((result,), owner_id="user_test")


def test_production_composer_uses_only_compatible_authoritative_baseline() -> None:
    current = _current_profile_projection()
    baselines = historical_baseline_projections((_structured_baseline(),))
    composer = ContextComposer(get_settings())
    text = composer.compose(
        CopilotContextPackage(entities=EntityResolver().resolve(IP)),
        current_projections=current,
        historical_baselines=baselines,
        request_id="delta-compatible",
    )

    assert "[SOORIN_DELTA_CONTEXT_JSON]" in text
    assert composer.last_delta_contexts
    assert composer.last_delta_contexts[0]["delta"]["changed"] == {"risk": 7}
    assert composer.last_delta_contexts[0]["baseline"]["provenance"] == "analyst"
    task = _task(
        "Is this asset still showing the same identity contradiction?",
        ("asset.get_profile", "asset.get_detection"),
    )
    assert task.required_capabilities


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    (
        ({"entity": "192.0.2.99"}, "wrong_entity"),
        ({"view": "identity"}, "wrong_view"),
        ({"schema_version": "product-view-v0"}, "wrong_schema"),
        ({"authoritative": False, "status": "candidate"}, "candidate_only"),
        ({"authoritative": False, "status": "superseded"}, "superseded"),
        ({"authoritative": False, "status": "invalidated"}, "invalidated"),
        ({"freshness": "expired"}, "stale"),
        ({"accessible": False}, "inactive"),
        ({"owner_id": "different-user"}, "wrong_owner"),
        ({"capability": "asset.get_detection"}, "wrong_capability"),
        ({"memory_type": "investigation_outcome"}, "wrong_memory_type"),
        ({"evidence_classes": ()}, "wrong_evidence_class"),
        ({"unresolved_conflict": True}, "unresolved_conflict"),
        ({"complete": False}, "incomplete_baseline"),
    ),
)
def test_production_composer_fails_closed_for_incompatible_baseline(
    change: dict[str, Any],
    expected_reason: str,
) -> None:
    baseline = historical_baseline_projections((_structured_baseline(),))[0]
    baseline = replace(baseline, **change)
    composer = ContextComposer(get_settings())
    text = composer.compose(
        CopilotContextPackage(entities=EntityResolver().resolve(IP)),
        current_projections=_current_profile_projection(),
        historical_baselines=(baseline,),
        request_id="delta-incompatible",
    )

    assert "[SOORIN_DELTA_CONTEXT_JSON]" not in text
    assert composer.last_delta_contexts == ()
    assert composer.last_delta_skip_reason == expected_reason

def _complete_product_result(
    capability: str = "asset.get_profile",
    *,
    view: str = "overview",
    payload: dict[str, Any] | None = None,
) -> ToolResult:
    return ToolResult(
        status="ok",
        entities=(IP,),
        source_capability=capability,
        retrieved_at="2026-08-20T00:00:00+00:00",
        valid_at="2026-08-20T00:00:00+00:00",
        freshness="current",
        completeness="complete",
        context_included=True,
        selected_views=(view,),
        view_payload={
            "provider": capability,
            "views": {view: payload or {"role": "server", "risk": 7}},
        },
        projection_schema_version="product-view-v1",
        projection_usable=True,
        source_payload_complete=True,
    )


def _complete_graph_result() -> ToolResult:
    return ToolResult(
        status="ok",
        entities=(IP,),
        source_capability="graph.get_summary",
        retrieved_at="2026-08-20T00:00:00+00:00",
        valid_at="2026-08-20T00:00:00+00:00",
        freshness="current",
        completeness="complete",
        context_included=True,
        projection_usable=True,
        source_payload_complete=True,
        provider_result=SimpleNamespace(context={
            "target_ip": IP,
            "requested_scope": "node_summary",
            "direction": "both",
            "depth": 0,
            "inbound_total": 1,
            "outbound_total": 1,
            "bidirectional_total": 0,
            "complete_for_user_request": True,
            "retrieval_truncated": False,
            "nodes": [{"id": IP}],
        }),
    )


@pytest.mark.parametrize(
    ("result", "capability"),
    (
        (_complete_product_result(), "asset.get_profile"),
        (
            _complete_product_result(
                "asset.get_detection",
                payload={"classification": "server", "confidence": 0.9},
            ),
            "asset.get_detection",
        ),
        (
            ToolResult(
                status="ok",
                entities=(IP,),
                source_capability="graph.get_summary",
                retrieved_at="2026-08-20T00:00:00+00:00",
                valid_at="2026-08-20T00:00:00+00:00",
                freshness="current",
                completeness="complete",
                context_included=True,
                projection_usable=True,
                source_payload_complete=True,
                provider_result=SimpleNamespace(context={
                    "target_ip": IP,
                    "requested_scope": "one_hop",
                    "direction": "both",
                    "depth": 1,
                    "inbound_total": 1,
                    "outbound_total": 1,
                    "bidirectional_total": 0,
                    "complete_for_user_request": True,
                    "retrieval_truncated": False,
                    "nodes": [
                        {"id": IP},
                        {"id": "192.0.2.11", "inbound": True, "hop": 1},
                        {"id": "192.0.2.12", "outbound": True, "hop": 1},
                    ],
                }),
            ),
            "graph.get_summary",
        ),
    ),
)
def test_complete_normalized_evidence_captures_bounded_baseline(
    result: ToolResult,
    capability: str,
) -> None:
    baseline = investigation_baseline_from_results(
        (result,),
        owner_id="user_test",
        source_request_id="capture-request",
        scope="one_hop",
        required_capabilities=(capability,),
    )

    assert baseline is not None
    assert baseline.owner_id == "user_test"
    assert {item.capability for item in baseline.projections} == {capability}
    assert all(item.completeness == "complete" for item in baseline.projections)
    if capability.startswith("graph."):
        projection = baseline.projections[0]
        assert projection.scope == "one_hop"
        assert projection.direction == "both"
        assert projection.depth == 1
        assert {item["direction"] for item in projection.payload["peers"]} == {
            "inbound", "outbound"
        }


def test_mapping_provider_result_is_supported_for_graph_baseline_projection() -> None:
    result = _complete_graph_result()
    mapped = replace(
        result,
        provider_result={"context": result.provider_result.context},  # type: ignore[union-attr]
    )

    baseline = investigation_baseline_from_results(
        (mapped,),
        owner_id="user_test",
        source_request_id="mapping-graph-request",
        scope="node_summary",
        required_capabilities=("graph.get_summary",),
    )

    assert baseline is not None
    assert baseline.projections[0].capability == "graph.get_summary"


def test_oversized_required_detection_is_compacted_not_dropped() -> None:
    result = _complete_product_result(
        "asset.get_detection",
        view="full",
        payload={
            "classification": "server",
            "risk": 9,
            "status": "review",
            "evidence": [
                {"feature": f"feature-{index}", "score": index / 1000}
                for index in range(1000)
            ],
        },
    )

    baseline = investigation_baseline_from_results(
        (result,),
        owner_id="user_test",
        source_request_id="oversized-detection",
        scope="node_summary",
        required_capabilities=("asset.get_detection",),
    )

    assert baseline is not None
    projection = baseline.projections[0]
    assert projection.view == "canonical"
    assert projection.payload["facts"]["views.full.classification"] == "server"
    assert projection.payload["facts"]["views.full.risk"] == 9
    assert len(json.dumps(projection.payload).encode("utf-8")) < 6_000


def test_multi_view_pair_baseline_keeps_graph_comparison_under_projection_limit() -> None:
    def multi(entity: str, capability: str) -> ToolResult:
        return replace(
            _complete_product_result(capability),
            entities=(entity,),
            selected_views=("identity", "risk", "behavior", "evidence"),
            view_payload={
                "provider": capability,
                "views": {
                    name: {"entity": entity, "status": name, "risk": 7}
                    for name in ("identity", "risk", "behavior", "evidence")
                },
            },
        )

    graph = replace(
        _complete_graph_result(),
        entities=("192.0.2.10", "192.0.2.20"),
        source_capability="graph.compare_assets",
    )
    results = (
        multi("192.0.2.10", "asset.get_profile"),
        multi("192.0.2.20", "asset.get_profile"),
        multi("192.0.2.10", "asset.get_detection"),
        multi("192.0.2.20", "asset.get_detection"),
        graph,
    )

    baseline = investigation_baseline_from_results(
        results,
        owner_id="user_test",
        source_request_id="pair-baseline",
        scope="multi_entity_comparison",
        required_capabilities=(
            "asset.get_profile", "asset.get_detection", "graph.compare_assets"
        ),
    )

    assert baseline is not None
    assert len(baseline.projections) == 5
    assert {item.capability for item in baseline.projections} == {
        "asset.get_profile", "asset.get_detection", "graph.compare_assets"
    }


def test_high_peer_graph_baseline_is_bounded_and_keeps_active_version() -> None:
    graph = replace(
        _complete_graph_result(),
        provider_result=SimpleNamespace(context={
            "target_ip": IP,
            "requested_scope": "full_neighbors",
            "direction": "both",
            "depth": 1,
            "active_graph_version": "published-v9",
            "retrieval_complete": True,
            "retrieval_truncated": False,
            "complete_for_user_request": True,
            "nodes": [
                {"id": IP},
                *(
                    {"id": f"192.0.2.{index}", "outbound": True, "hop": 1}
                    for index in range(1, 200)
                ),
            ],
        }),
    )

    baseline = investigation_baseline_from_results(
        (graph,),
        owner_id="user_test",
        source_request_id="high-peer-graph",
        scope="full_neighbors",
        required_capabilities=("graph.get_summary",),
    )

    assert baseline is not None
    projection = baseline.projections[0]
    assert projection.payload["active_graph_version"] == "published-v9"
    assert len(projection.payload["peers"]) == 48
    assert len(json.dumps(projection.payload).encode("utf-8")) < 6_000


def test_weak_or_memory_only_turn_cannot_create_baseline() -> None:
    complete = _complete_product_result()
    failed = replace(complete, status="unavailable", completeness="unknown")
    partial = replace(complete, status="partial", completeness="partial")
    excluded = replace(complete, context_included=False)

    for results in ((), (failed,), (partial,), (excluded,)):
        assert investigation_baseline_from_results(
            results,
            owner_id="user_test",
            source_request_id="weak-request",
            scope="node_summary",
            required_capabilities=("asset.get_profile",),
        ) is None


def test_recursive_delta_classifies_nested_keyed_and_ambiguous_values() -> None:
    baseline = {
        "counts": {"inbound": 1, "outbound": 2},
        "role": "server",
        "peers": [
            {"id": "peer-a", "direction": "outbound"},
            {"id": "peer-b", "direction": "inbound"},
        ],
        "path_nodes": ["source", "old", "target"],
        "removed_field": True,
    }
    current = {
        "counts": {"inbound": 2, "outbound": 2},
        "role": "server",
        "peers": [
            {"id": "peer-a", "direction": "bidirectional"},
            {"id": "peer-c", "direction": "outbound"},
        ],
        "path_nodes": ["source", "new", "target"],
        "new_field": True,
    }
    delta = build_delta_context(
        current,
        baseline=baseline,
        current_identity="graph",
        baseline_identity="graph",
        schema_version="graph-baseline-v1",
        baseline_schema_version="graph-baseline-v1",
        baseline_accessible=True,
    )

    states = delta.payload["states"]
    assert delta.created
    assert any(item["path"] == "counts.inbound" for item in states["changed"])
    assert any(item["path"] == "role" for item in states["unchanged"])
    assert any(item["path"] == "peers[id=peer-c]" for item in states["new"])
    assert any(item["path"] == "peers[id=peer-b]" for item in states["missing"])
    assert any(
        item["path"] == "peers[id=peer-a].direction"
        for item in states["changed"]
    )
    assert any(item["path"] == "path_nodes" for item in states["incomparable"])


def test_partial_current_delta_never_turns_unobserved_data_into_removal() -> None:
    delta = build_delta_context(
        {"peers": [{"id": "peer-a", "direction": "outbound"}]},
        baseline={
            "peers": [
                {"id": "peer-a", "direction": "outbound"},
                {"id": "peer-b", "direction": "inbound"},
            ],
            "count": 2,
        },
        current_identity="graph",
        baseline_identity="graph",
        schema_version="graph-baseline-v1",
        baseline_schema_version="graph-baseline-v1",
        baseline_accessible=True,
        current_complete=False,
    )

    assert delta.created
    assert delta.reason == "compatible_partial_current_baseline"
    assert delta.payload["removed"] == {}
    assert delta.payload["states"]["missing"] == []
    assert delta.payload["states"]["incomparable"]


def _episode_graph_projection(
    *,
    payload: dict[str, Any] | None = None,
    completeness: str = "complete",
) -> BaselineProjection:
    data = payload or {
        "counts": {"inbound_total": 1, "outbound_total": 1},
        "peers": [{"id": "192.0.2.11", "direction": "inbound", "hop": 1}],
    }
    return BaselineProjection(
        capability="graph.get_summary",
        entity_ids=(IP,),
        view="graph",
        schema_version="graph-baseline-v1",
        evidence_classes=("graph_topology",),
        payload=data,
        valid_at="2026-08-19T00:00:00+00:00",
        completeness=completeness,  # type: ignore[arg-type]
        fingerprint=fingerprint(data),
        scope="one_hop",
        direction="both",
        depth=1,
    )


def test_episodic_graph_baseline_is_selected_and_not_reported_absent() -> None:
    projection = _episode_graph_projection()
    baseline = InvestigationBaseline(
        entity_ids=(IP,),
        captured_at=projection.valid_at,
        source_request_id="episode-baseline",
        scope="one_hop",
        projections=(projection,),
        owner_id="user_test",
    )
    current = CurrentEvidenceProjection(
        owner_id="user_test",
        entity=IP,
        entity_ids=(IP,),
        capability="graph.get_summary",
        view="graph",
        schema_version="graph-baseline-v1",
        payload={
            "counts": {"inbound_total": 2, "outbound_total": 1},
            "peers": [
                {"id": "192.0.2.11", "direction": "inbound", "hop": 1},
                {"id": "192.0.2.12", "direction": "outbound", "hop": 1},
            ],
        },
        retrieved_at="2026-08-20T00:00:00+00:00",
        complete=True,
        scope="one_hop",
        direction="both",
        depth=1,
    )
    composer = ContextComposer(get_settings())
    composer.compose(
        CopilotContextPackage(entities=EntityResolver().resolve(IP)),
        current_projections=(current,),
        historical_baselines=episodic_baseline_projections((baseline,)),
        request_id="episode-delta",
    )

    assert composer.last_baseline_status == "available"
    assert composer.last_baseline_present
    assert composer.last_baseline_compatible
    assert composer.last_delta_contexts


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ({"scope": "two_hop"}, "wrong_scope"),
        ({"direction": "inbound"}, "wrong_direction"),
        ({"depth": 2}, "wrong_depth"),
        ({"schema_version": "graph-baseline-v0"}, "wrong_schema"),
        ({"entity_ids": ("192.0.2.99",), "entity": "192.0.2.99"}, "wrong_entity"),
        ({"owner_id": "other-user"}, "wrong_owner"),
    ),
)
def test_graph_baseline_requires_exact_operational_compatibility(
    change: dict[str, Any],
    reason: str,
) -> None:
    baseline = episodic_baseline_projections((
        InvestigationBaseline(
            entity_ids=(IP,),
            captured_at="2026-08-19T00:00:00+00:00",
            source_request_id="graph-compatible",
            scope="one_hop",
            projections=(_episode_graph_projection(),),
            owner_id="user_test",
        ),
    ))[0]
    current = CurrentEvidenceProjection(
        owner_id="user_test",
        entity=IP,
        entity_ids=(IP,),
        capability="graph.get_summary",
        view="graph",
        schema_version="graph-baseline-v1",
        payload={"counts": {"inbound_total": 2}},
        retrieved_at="2026-08-20T00:00:00+00:00",
        scope="one_hop",
        direction="both",
        depth=1,
    )
    composer = ContextComposer(get_settings())
    composer.compose(
        CopilotContextPackage(entities=EntityResolver().resolve(IP)),
        current_projections=(current,),
        historical_baselines=(replace(baseline, **change),),
        request_id="graph-incompatible",
    )

    assert composer.last_delta_skip_reason == reason
    assert composer.last_baseline_status == "incompatible"


def test_partial_graph_observation_uses_baseline_without_false_removal() -> None:
    baseline = InvestigationBaseline(
        entity_ids=(IP,),
        captured_at="2026-08-19T00:00:00+00:00",
        source_request_id="graph-partial",
        scope="one_hop",
        projections=(_episode_graph_projection(),),
        owner_id="user_test",
    )
    current = CurrentEvidenceProjection(
        owner_id="user_test",
        entity=IP,
        entity_ids=(IP,),
        capability="graph.get_summary",
        view="graph",
        schema_version="graph-baseline-v1",
        payload={"counts": {"inbound_total": 1}},
        retrieved_at="2026-08-20T00:00:00+00:00",
        complete=False,
        scope="one_hop",
        direction="both",
        depth=1,
    )
    composer = ContextComposer(get_settings())
    composer.compose(
        CopilotContextPackage(entities=EntityResolver().resolve(IP)),
        current_projections=(current,),
        historical_baselines=episodic_baseline_projections((baseline,)),
    )

    assert composer.last_baseline_status == "partial"
    assert composer.last_delta_contexts
    states = composer.last_delta_contexts[0]["delta"]["states"]
    assert states["missing"] == []
    assert states["incomparable"]


def test_active_structured_ltm_baseline_is_independent_of_prose_budget() -> None:
    active_baseline = _structured_baseline()
    selection = LongTermMemorySelection(
        status="empty",
        memories=(),
        limitations=("long_term_memory_context_truncated",),
    )

    class Retriever:
        def retrieve(self, **_kwargs: Any) -> LongTermMemorySelection:
            return selection

    class Store:
        def list(self, *, statuses: tuple[str, ...], **_kwargs: Any) -> tuple[Any, ...]:
            return (active_baseline.memory,) if statuses == ("active",) else ()

    service = SimpleNamespace(
        long_term_memory_retriever=Retriever(),
        long_term_memory_store=Store(),
    )
    resolved = CopilotService.retrieve_long_term_memory(
        service,
        identity=RequestIdentity.resolve(
            user_id="user_test",
            conversation_id="conversation-test",
            session_id="session-test",
            request_id="request-test",
        ),
        message="Verify it now.",
        entity_ids=(IP,),
    )

    assert resolved.memories == ()
    assert [item.memory.memory_id for item in resolved.baseline_memories] == [
        active_baseline.memory.memory_id
    ]


def test_inventory_failure_preserves_retrieved_canonical_memory() -> None:
    retrieved = _memory("Canonical exact memory survives inventory failure.")
    selection = LongTermMemorySelection(status="ok", memories=(retrieved,), selected_count=1)

    class Retriever:
        def retrieve(self, **_kwargs: Any) -> LongTermMemorySelection:
            return selection

    class BrokenInventory:
        def list(self, **_kwargs: Any) -> tuple[Any, ...]:
            raise LocalPersistenceError("inventory unavailable")

    service = SimpleNamespace(
        long_term_memory_retriever=Retriever(),
        long_term_memory_store=BrokenInventory(),
    )
    resolved = CopilotService.retrieve_long_term_memory(
        service,
        identity=RequestIdentity.resolve(
            user_id="user_test",
            conversation_id="conversation-test",
            session_id="session-inventory-failure",
            request_id="inventory-failure-request",
        ),
        message="What did we validate?",
        entity_ids=(IP,),
    )

    assert resolved.status == "ok"
    assert resolved.memories == (retrieved,)
    assert "long_term_memory_inventory_unavailable" in resolved.limitations


def test_detach_clears_prior_routing_state_but_failed_turn_keeps_it() -> None:
    def run_update(transition: str) -> SessionRoutingState:
        task = TaskSpec(
            request="Explain Kerberos generally.",
            intent="general_knowledge",
            scope="none",
            direction="none",
            entities=(),
            required_capabilities=(),
        )
        identity = RequestIdentity.resolve(
            user_id="user_test",
            conversation_id=f"conversation-{transition}",
            session_id=f"session-{transition}",
            request_id=f"request-{transition}",
        )
        previous = SessionRoutingState(
            active_ip=IP,
            active_entities=(IP,),
            last_resolved_entities=(IP,),
            previous_entity_count=1,
            previous_entity_mode="single",
        )
        service = SimpleNamespace(
            settings=get_settings(),
            memory_store=MemoryStore(20),
            routing_state_store=SessionRoutingStateStore(),
            long_term_memory_coordinator=None,
            persist_thread_continuity=lambda *_args, **_kwargs: None,
            persist_completed_local_turn=lambda *_args, **_kwargs: None,
        )
        state = {
            "task": task,
            "tool_results": (),
            "synthesis_result": {"answer": "General answer."},
            "memory_context_key": MemoryContextKey.from_task(task),
            "pending_working_facts": (),
            "session_id": identity.session_id,
            "request_id": identity.request_id,
            "request_identity": identity,
            "message": task.request,
            "evidence_pack": SimpleNamespace(limitations=()),
            "active_entity_state": previous,
            "resolved_entities": EntityResolution(status="none"),
            "routing_result": SimpleNamespace(
                intent="general_knowledge", scope="none", direction="none", depth=0,
                use_detection=False, use_asset_profile=False,
            ),
            "execution_plan": ExecutionPlan(task=task, steps=(), plan_id=f"plan-{transition}"),
            "review_decision": ReviewDecision(outcome="sufficient"),
            "turn_policy": SimpleNamespace(episode_transition=transition),
        }
        return CopilotWorkflowNodes(service).update_memory(state)["active_entity_state"]

    detached = run_update("detach")
    failed = run_update("keep")

    assert detached.active_entities == ()
    assert detached.active_ip is None
    assert detached.last_resolved_entities == ()
    assert detached.previous_entity_count == 0
    assert detached.previous_entity_mode == "none"
    assert failed.active_entities == (IP,)
    assert failed.active_ip == IP
    assert failed.last_resolved_entities == (IP,)

def test_candidate_ltm_never_becomes_authoritative_delta_baseline() -> None:
    baseline = historical_baseline_projections((_structured_baseline(candidate=True),))[0]
    assert not baseline.authoritative
    composer = ContextComposer(get_settings())
    composer.compose(
        CopilotContextPackage(entities=EntityResolver().resolve(IP)),
        current_projections=_current_profile_projection(),
        historical_baselines=(baseline,),
    )
    assert composer.last_delta_skip_reason == "candidate_only"


def test_update_memory_captures_baseline_from_tool_results_not_assistant_prose() -> None:
    task = _task("Analyze this asset.", ("asset.get_profile",))
    key = MemoryContextKey.from_task(task)
    memory = MemoryStore(20)
    service = SimpleNamespace(
        settings=get_settings(),
        memory_store=memory,
        routing_state_store=SessionRoutingStateStore(),
        long_term_memory_coordinator=None,
        persist_thread_continuity=lambda *_args, **_kwargs: None,
        persist_completed_local_turn=lambda *_args, **_kwargs: None,
    )
    identity = RequestIdentity.resolve(
        user_id="user_test",
        conversation_id="conversation-test",
        session_id="baseline-update",
        request_id="baseline-update-request",
    )
    state = {
        "task": task,
        "tool_results": [_complete_product_result()],
        "synthesis_result": {"answer": "assistant prose must not become baseline"},
        "memory_context_key": key,
        "pending_working_facts": (),
        "session_id": identity.session_id,
        "request_id": identity.request_id,
        "request_identity": identity,
        "message": "Analyze this asset.",
        "evidence_pack": SimpleNamespace(limitations=()),
        "active_entity_state": SessionRoutingState(active_entities=(IP,)),
        "resolved_entities": EntityResolver().resolve(IP),
        "routing_result": SimpleNamespace(
            intent="asset_investigation",
            scope="node_summary",
            direction="both",
            depth=0,
            use_detection=False,
            use_asset_profile=True,
        ),
        "execution_plan": ExecutionPlan(
            task=task,
            steps=(PlanStep("profile", "asset.get_profile"),),
            plan_id="baseline-plan",
        ),
        "review_decision": ReviewDecision(outcome="sufficient"),
    }

    update = CopilotWorkflowNodes(service).update_memory(state)
    baseline = memory.repository.get_working(identity.session_id).baseline  # type: ignore[union-attr]

    assert update["memory_update_result"]["investigation_baseline_write_count"] == 1
    assert baseline is not None
    assert baseline.source_request_id == identity.request_id
    assert baseline.projections[0].payload == {"role": "server", "risk": 7}
    assert "assistant prose" not in str(baseline.projections[0].payload)


def _run_baseline_update(
    task: TaskSpec,
    results: tuple[ToolResult, ...],
    *,
    request_id: str,
    fallback_used: bool = False,
    limitations: tuple[str, ...] = (),
) -> tuple[dict[str, Any], MemoryStore, RequestIdentity]:
    key = MemoryContextKey.from_task(task)
    memory = MemoryStore(20)
    service = SimpleNamespace(
        settings=get_settings(),
        memory_store=memory,
        routing_state_store=SessionRoutingStateStore(),
        long_term_memory_coordinator=None,
        persist_thread_continuity=lambda *_args, **_kwargs: None,
        persist_completed_local_turn=lambda *_args, **_kwargs: None,
    )
    identity = RequestIdentity.resolve(
        user_id="user_test",
        conversation_id="conversation-test",
        session_id=f"session-{request_id}",
        request_id=request_id,
    )
    state = {
        "task": task,
        "tool_results": list(results),
        "synthesis_result": {
            "answer": "Grounded current investigation.",
            "status": "completed_with_limitations" if fallback_used else "completed",
        },
        "memory_context_key": key,
        "pending_working_facts": (),
        "session_id": identity.session_id,
        "request_id": identity.request_id,
        "request_identity": identity,
        "message": task.request,
        "evidence_pack": SimpleNamespace(limitations=limitations),
        "active_entity_state": SessionRoutingState(active_entities=(IP,)),
        "resolved_entities": EntityResolver().resolve(IP),
        "routing_result": SimpleNamespace(
            intent="asset_investigation",
            scope="node_summary",
            direction="both",
            depth=0,
            use_detection=True,
            use_asset_profile=True,
        ),
        "execution_plan": ExecutionPlan(
            task=task,
            steps=tuple(
                PlanStep(f"step-{index}", capability)
                for index, capability in enumerate(task.required_capabilities)
            ),
            plan_id="baseline-plan",
        ),
        "review_decision": ReviewDecision(outcome="sufficient"),
        "fallback_used": fallback_used,
        "limitation_reasons": list(limitations),
    }
    return CopilotWorkflowNodes(service).update_memory(state), memory, identity


def test_complete_sufficient_multisource_baseline_survives_fallback_and_thread_state_roundtrip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    task = _task(
        "Analyze the current asset.",
        ("asset.get_profile", "asset.get_detection", "graph.get_summary"),
    )
    results = (
        _complete_product_result(),
        _complete_product_result(
            "asset.get_detection",
            payload={"classification": "server", "confidence": 0.9},
        ),
        _complete_graph_result(),
    )

    update, memory, identity = _run_baseline_update(
        task,
        results,
        request_id="complete-fallback-baseline",
        fallback_used=True,
        limitations=("validated_plan_fallback_used",),
    )
    baseline = memory.repository.get_working(identity.session_id).baseline  # type: ignore[union-attr]

    assert update["memory_update_result"]["investigation_baseline_write_count"] == 1
    assert baseline is not None
    assert {item.capability for item in baseline.projections} == {
        "asset.get_profile",
        "asset.get_detection",
        "graph.get_summary",
    }
    assert "event=investigation_baseline_persisted" in caplog.text
    assert f"entity={IP}" in caplog.text

    state = ThreadMemoryState.from_routing_state(
        identity,
        update["active_entity_state"],
        **memory.durable_components(identity.session_id, turn_limit=4, episode_limit=4),
    )
    restored_state = ThreadMemoryState.from_payload(
        state.to_payload(),
        thread_key=identity.thread_key,
        user_id=identity.user_id,
        conversation_id=identity.conversation_id,
        session_id=identity.session_id,
        updated_at=state.updated_at,
        revision=1,
        schema_version=THREAD_STATE_SCHEMA_VERSION,
    )
    restored_memory = MemoryStore(20)
    restored_memory.restore_durable_state(restored_state)
    restored = restored_memory.repository.get_working(identity.session_id)
    assert restored is not None
    assert restored.baseline == baseline


def test_memory_only_update_does_not_capture_baseline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    task = replace(
        _task("Use memory only.", ("asset.get_profile",)),
        evidence_mode="memory_only",
        temporal_mode="historical",
    )

    update, memory, identity = _run_baseline_update(
        task,
        (_complete_product_result(),),
        request_id="memory-only-no-baseline",
    )
    working = memory.repository.get_working(identity.session_id)

    assert update["memory_update_result"]["investigation_baseline_write_count"] == 0
    assert working is not None and working.baseline is None
    assert "reason=ineligible_evidence_mode" in caplog.text


@pytest.mark.parametrize(
    "unsafe_result",
    (
        replace(_complete_product_result(), status="unavailable", completeness="unknown"),
        replace(_complete_product_result(), status="partial", completeness="partial"),
    ),
)
def test_failed_or_partial_update_does_not_capture_baseline(
    unsafe_result: ToolResult,
) -> None:
    task = _task("Analyze the current asset.", ("asset.get_profile",))

    update, memory, identity = _run_baseline_update(
        task,
        (unsafe_result,),
        request_id=f"unsafe-{unsafe_result.status}",
    )
    working = memory.repository.get_working(identity.session_id)

    assert update["memory_update_result"]["investigation_baseline_write_count"] == 0
    assert working is not None and working.baseline is None
