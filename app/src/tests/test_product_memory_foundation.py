from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from src.core.agent.contracts import ToolResult
from src.core.context.compaction import fingerprint
from src.core.identity import RequestIdentity
from src.core.memory.baselines import BaselineProjection, InvestigationBaseline
from src.core.memory.episodes import MemoryContextKey, WorkingFact, WorkingMemory
from src.core.memory.long_term import LongTermMemoryRecord, PromotionDecision
from src.core.memory.retrieval import LongTermMemoryCoordinator
from src.core.memory.persistence import (
    MAX_THREAD_STATE_BYTES,
    LocalPersistenceConflictError,
    THREAD_STATE_SCHEMA_VERSION,
    ThreadMemoryState,
)
from src.core.memory.product import (
    PRODUCT_THREAD_STATE_SCHEMA_VERSION,
    ProductLongTermMemoryStore,
    ProductThreadStateStore,
)
from src.core.memory.routing_state import SessionRoutingState
from src.core.product_client.memory_client import (
    ProductMemoryClient,
    ProductMemoryConflictError,
    ProductMemoryNotFoundError,
)


class FakeProductClient:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []
        self.settings = SimpleNamespace(
            product_memory_thread_state_path="/configured/thread",
            product_memory_ltm_path="/configured/ltm",
        )
    def _request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.error: raise self.error
        payload = self.payload(method, path, kwargs) if callable(self.payload) else self.payload
        return payload, 200, 0.01


def test_memory_transport_scopes_owner_header_without_global_client_change():
    client = FakeProductClient({"revision": 1, "schemaVersion": 3, "stateJson": {}})
    ProductMemoryClient(client).get_thread_state(user_id="local-owner", conversation_id="chat-1")
    method, path, kwargs = client.calls[0]
    assert (method, path) == ("GET", "/configured/thread/chat-1")
    assert kwargs["extra_headers"]["X-User-ID"] == "local-owner"
    assert kwargs["operation"] == "memory_thread_load"


def test_product_thread_state_404_means_absent():
    store = ProductThreadStateStore(ProductMemoryClient(FakeProductClient(error=ProductMemoryNotFoundError("missing"))))
    identity = RequestIdentity.resolve(user_id="owner", conversation_id="chat-1", session_id="session-1")
    assert store.load(identity=identity) is None


def test_product_thread_state_save_uses_canonical_payload_and_revision():
    fake = FakeProductClient({"revision": 1, "sessionId": "session-1", "updatedAt": "2026-01-01T00:00:00+00:00"})
    store = ProductThreadStateStore(ProductMemoryClient(fake))
    identity = RequestIdentity.resolve(user_id="owner", conversation_id="chat-1", session_id="session-1")
    state = ThreadMemoryState.from_routing_state(identity, SessionRoutingState())
    saved = store.save(identity=identity, state=state, expected_revision=0)
    assert saved.revision == 1
    body = fake.calls[0][2]["json_body"]
    assert body == {
        "sessionId": "session-1",
        "expectedRevision": 0,
        "schemaVersion": PRODUCT_THREAD_STATE_SCHEMA_VERSION,
        "conversationId": "chat-1",
        "stateJson": state.to_payload(),
    }


def _thread_state_with_baseline():
    identity = RequestIdentity.resolve(
        user_id="owner",
        conversation_id="chat-1",
        session_id="session-1",
    )
    entity = "192.0.2.1"
    captured_at = "2026-08-20T08:00:00+00:00"
    evidence = {"role": "server", "risk": 4}
    projection = BaselineProjection(
        capability="asset.get_profile",
        entity_ids=(entity,),
        view="overview",
        schema_version="product-view-v1",
        evidence_classes=("asset_identity", "asset_role"),
        payload=evidence,
        valid_at=captured_at,
        completeness="complete",
        fingerprint=fingerprint(evidence),
    )
    baseline = InvestigationBaseline(
        entity_ids=(entity,),
        captured_at=captured_at,
        source_request_id="baseline-request",
        scope="node_summary",
        projections=(projection,),
        owner_id="owner",
    )
    context_key = MemoryContextKey(
        entities=(entity,),
        topic_family="asset_investigation",
        scope_family="asset",
    )
    state = ThreadMemoryState.from_routing_state(
        identity,
        SessionRoutingState(
            active_entities=(entity,),
            previous_intent="asset_investigation",
            previous_scope="node_summary",
        ),
        working_memory=WorkingMemory(
            session_id=identity.session_id,
            context_key=context_key,
            episode_id="active-episode",
            compact_summary="Asset appears to be a web endpoint",
            working_facts=(
                WorkingFact(
                    key="tcp-443",
                    value="Host responds on TCP/443",
                    scope="entity",
                    entity_ids=(entity,),
                ),
            ),
            last_providers=("product_profile",),
            last_scope="node_summary",
            baseline=baseline,
        ),
    )
    return identity, state, baseline


def test_product_thread_state_baseline_uses_v3_wire_and_current_internal_schema():
    identity, state, baseline = _thread_state_with_baseline()
    fake = FakeProductClient(
        {
            "revision": 1,
            "sessionId": identity.session_id,
            "updatedAt": "2026-08-20T08:01:00+00:00",
        }
    )

    ProductThreadStateStore(ProductMemoryClient(fake)).save(
        identity=identity,
        state=state,
        expected_revision=0,
    )

    body = fake.calls[0][2]["json_body"]
    assert state.schema_version == THREAD_STATE_SCHEMA_VERSION == 4
    assert body["schemaVersion"] == PRODUCT_THREAD_STATE_SCHEMA_VERSION == 3
    assert body["stateJson"]["working_memory"]["baseline"]["source_request_id"] == (
        baseline.source_request_id
    )
    assert len(
        json.dumps(body["stateJson"], separators=(",", ":")).encode("utf-8")
    ) <= MAX_THREAD_STATE_BYTES


def test_product_v3_load_normalizes_baseline_state_to_current_internal_schema():
    identity, state, baseline = _thread_state_with_baseline()
    fake = FakeProductClient(
        {
            "schemaVersion": PRODUCT_THREAD_STATE_SCHEMA_VERSION,
            "revision": 1,
            "sessionId": identity.session_id,
            "updatedAt": "2026-08-20T08:01:00+00:00",
            "stateJson": state.to_payload(),
        }
    )

    restored = ProductThreadStateStore(ProductMemoryClient(fake)).load(identity=identity)

    assert restored is not None
    assert restored.schema_version == THREAD_STATE_SCHEMA_VERSION
    assert restored.active_entities == ("192.0.2.1",)
    assert restored.previous_intent == "asset_investigation"
    assert restored.working_memory is not None
    assert restored.working_memory.episode_id == "active-episode"
    assert restored.working_memory.compact_summary == "Asset appears to be a web endpoint"
    assert restored.working_memory.working_facts[0].value == "Host responds on TCP/443"
    assert restored.working_memory.baseline == baseline


def test_product_old_v3_state_without_baseline_remains_loadable():
    identity = RequestIdentity.resolve(
        user_id="owner",
        conversation_id="chat-legacy",
        session_id="session-legacy",
    )
    legacy = ThreadMemoryState.from_routing_state(
        identity,
        SessionRoutingState(
            active_entities=("192.0.2.9",),
            previous_intent="asset_investigation",
        ),
    )
    payload = legacy.to_payload()
    assert payload.get("working_memory") is None
    fake = FakeProductClient(
        {
            "schemaVersion": PRODUCT_THREAD_STATE_SCHEMA_VERSION,
            "revision": 7,
            "sessionId": identity.session_id,
            "stateJson": payload,
        }
    )

    restored = ProductThreadStateStore(ProductMemoryClient(fake)).load(identity=identity)

    assert restored is not None
    assert restored.schema_version == THREAD_STATE_SCHEMA_VERSION
    assert restored.revision == 7
    assert restored.active_entities == ("192.0.2.9",)
    assert restored.working_memory is None

def test_product_ltm_create_preserves_candidate_domain_values_and_lossless_refs():
    record = __import__("src.core.memory.long_term", fromlist=["LongTermMemoryRecord"]).LongTermMemoryRecord.candidate(
        memory_type="validated_finding", user_id="owner", entity_ids=("192.0.2.1",), statement="{}",
        source_request_id="request-1", source_conversation_id="chat-1", evidence_refs=("evidence_class_asset_identity",),
    )
    response = {"memoryId": record.memory_id, "memoryType": record.memory_type, "userId": record.user_id,
                "entityIds": list(record.entity_ids), "statement": record.statement, "epistemicStatus": "candidate",
                "confidence": record.confidence, "sourceRequestId": record.source_request_id, "sourceConversationId": record.source_conversation_id,
                "evidenceRefs": [{"type": "canonical_ref", "id": record.evidence_refs[0]}], "provenanceCategory": record.provenance_category,
                "validFrom": record.valid_from, "validUntil": record.valid_until, "createdAt": record.created_at, "updatedAt": record.updated_at,
                "revision": 1, "status": "candidate", "indexStatus": record.index_status, "idempotencyFingerprint": record.idempotency_fingerprint,
                "logicalMemoryKey": record.logical_memory_key, "policyVersion": record.policy_version}
    fake = FakeProductClient(response)
    stored = ProductLongTermMemoryStore(ProductMemoryClient(fake)).put(memory=record)
    body = fake.calls[0][2]["json_body"]
    assert body["epistemicStatus"] == "candidate"
    assert body["entityIds"] == ["192.0.2.1"]
    assert body["evidenceRefs"] == [{"type": "canonical_ref", "id": "evidence_class_asset_identity"}]
    assert stored.idempotency_fingerprint == record.idempotency_fingerprint


def test_product_ltm_search_preserves_current_request_id():
    fake = FakeProductClient({"items": []})
    store = ProductLongTermMemoryStore(ProductMemoryClient(fake))

    records = store.list(
        user_id="owner",
        entity_ids=("192.0.2.1",),
        statuses=("active",),
        request_id="current-request-1",
        purpose="exact_active_retrieval",
    )

    assert records == ()
    method, path, kwargs = fake.calls[0]
    assert (method, path) == ("POST", "/configured/ltm/search")
    assert kwargs["request_id"] == "current-request-1"
    assert kwargs["operation"] == "memory_search"


def test_product_ltm_inventory_normalizes_snake_case_and_reference_aliases():
    record = _candidate()
    canonical = _canonical_wire(record)
    snake = {
        "memory_id": canonical["memoryId"],
        "memory_type": canonical["memoryType"],
        "user_id": canonical["userId"],
        "entity_ids": canonical["entityIds"],
        "statement": canonical["statement"],
        "epistemic_status": canonical["epistemicStatus"],
        "confidence": canonical["confidence"],
        "source_request_id": canonical["sourceRequestId"],
        "source_conversation_id": canonical["sourceConversationId"],
        "evidence_refs": [
            {"refType": "canonical", "refId": record.evidence_refs[0]}
        ],
        "provenance_category": canonical["provenanceCategory"],
        "valid_from": canonical["validFrom"],
        "valid_until": canonical["validUntil"],
        "created_at": canonical["createdAt"],
        "updated_at": canonical["updatedAt"],
        "revision": canonical["revision"],
        "status": canonical["status"],
        "index_status": canonical["indexStatus"],
        "idempotency_fingerprint": canonical["idempotencyFingerprint"],
        "logical_memory_key": canonical["logicalMemoryKey"],
        "policy_version": canonical["policyVersion"],
    }
    store = ProductLongTermMemoryStore(
        ProductMemoryClient(FakeProductClient({"records": [{"record": snake}]}))
    )

    records = store.list(user_id=record.user_id, statuses=("candidate",))

    assert records == (record,)


def test_product_ltm_get_preserves_current_request_id():
    record = __import__("src.core.memory.long_term", fromlist=["LongTermMemoryRecord"]).LongTermMemoryRecord.candidate(
        memory_type="validated_finding", user_id="owner", entity_ids=("192.0.2.1",), statement="{}",
        source_request_id="source-request", source_conversation_id="chat-1", evidence_refs=("evidence_class_asset_identity",),
    )
    payload = {
        "memoryId": record.memory_id, "memoryType": record.memory_type, "userId": record.user_id,
        "entityIds": list(record.entity_ids), "statement": record.statement, "epistemicStatus": record.epistemic_status,
        "confidence": record.confidence, "sourceRequestId": record.source_request_id, "sourceConversationId": record.source_conversation_id,
        "evidenceRefs": [{"type": "canonical_ref", "id": record.evidence_refs[0]}], "provenanceCategory": record.provenance_category,
        "validFrom": record.valid_from, "validUntil": record.valid_until, "createdAt": record.created_at, "updatedAt": record.updated_at,
        "revision": record.revision, "status": record.status, "indexStatus": record.index_status,
        "idempotencyFingerprint": record.idempotency_fingerprint, "logicalMemoryKey": record.logical_memory_key,
        "policyVersion": record.policy_version,
    }
    fake = FakeProductClient(payload)
    stored = ProductLongTermMemoryStore(ProductMemoryClient(fake)).get(
        user_id=record.user_id,
        memory_id=record.memory_id,
        request_id="current-request-2",
        purpose="semantic_canonical_hydration",
    )

    assert stored is not None
    assert stored.source_request_id == "source-request"
    method, path, kwargs = fake.calls[0]
    assert (method, path) == ("GET", f"/configured/ltm/{record.memory_id}")
    assert kwargs["request_id"] == "current-request-2"
    assert kwargs["operation"] == "memory_get"


def test_non_conflict_review_reuses_canonical_candidate_without_reads():
    record = __import__("src.core.memory.long_term", fromlist=["LongTermMemoryRecord"]).LongTermMemoryRecord.candidate(
        memory_type="validated_finding", user_id="owner", entity_ids=("192.0.2.1",), statement="{}",
        source_request_id="source-request", source_conversation_id="chat-1", evidence_refs=("evidence_class_asset_identity",),
    )
    fake = FakeProductClient({"items": []})
    decision = PromotionDecision(
        "requires_review",
        "evidence_class_requires_review",
        "review required",
        confidence=record.confidence,
        epistemic_status=record.epistemic_status,
        provenance_category=record.provenance_category,
    )

    result = ProductLongTermMemoryStore(ProductMemoryClient(fake)).apply_promotion(
        candidate=record,
        decision=decision,
        request_id="current-request-3",
    )

    assert result.memory is record
    assert result.memory.source_request_id == "source-request"
    assert fake.calls == []


def test_product_ltm_promotion_uses_one_atomic_transition_request():
    record = __import__("src.core.memory.long_term", fromlist=["LongTermMemoryRecord"]).LongTermMemoryRecord.candidate(
        memory_type="validated_finding", user_id="owner", entity_ids=("192.0.2.1",), statement="{}",
        source_request_id="request-1", source_conversation_id="chat-1", evidence_refs=("evidence_class_asset_identity",),
    )
    payload = {
        "memoryId": record.memory_id, "memoryType": record.memory_type, "userId": record.user_id,
        "entityIds": list(record.entity_ids), "statement": record.statement, "epistemicStatus": "source_validated",
        "confidence": 0.95, "sourceRequestId": record.source_request_id, "sourceConversationId": record.source_conversation_id,
        "evidenceRefs": [{"type": "canonical_ref", "id": record.evidence_refs[0]}], "provenanceCategory": "product",
        "validFrom": record.valid_from, "validUntil": record.valid_until, "createdAt": record.created_at, "updatedAt": record.updated_at,
        "revision": 2, "status": "active", "indexStatus": "pending", "idempotencyFingerprint": record.idempotency_fingerprint,
        "logicalMemoryKey": record.logical_memory_key, "policyVersion": record.policy_version,
    }
    fake = FakeProductClient(lambda method, path, kwargs: {"items": []} if path.endswith("/search") else payload)
    decision = PromotionDecision("auto_promote", "validated_product_fact", "eligible", confidence=0.95, epistemic_status="source_validated", provenance_category="product")
    result = ProductLongTermMemoryStore(ProductMemoryClient(fake)).apply_promotion(candidate=record, decision=decision, actor="copilot")
    assert result.memory.status == "active"
    transition_calls = [call for call in fake.calls if call[1].endswith("/transition")]
    assert len(transition_calls) == 1
    method, path, kwargs = transition_calls[0]
    assert (method, path) == ("POST", f"/configured/ltm/{record.memory_id}/transition")
    assert kwargs["json_body"] == {
        "expectedRevision": record.revision,
        "action": "promote",
        "reasonCode": "validated_product_fact",
        "actor": "copilot",
        "policyVersion": "ltm-promotion-v1",
        "resolution": "compatible_change",
    }
    assert kwargs["extra_headers"] == {"X-User-ID": "owner", "Content-Type": "application/json"}
    assert kwargs["operation"] == "memory_transition"


def test_product_ltm_local_test_owner_is_transport_only():
    record = __import__("src.core.memory.long_term", fromlist=["LongTermMemoryRecord"]).LongTermMemoryRecord.candidate(
        memory_type="validated_finding", user_id="local-user-123", entity_ids=("192.0.2.1",), statement="{}",
        source_request_id="request-1", source_conversation_id="chat-1", evidence_refs=("evidence_class_asset_identity",),
    )
    payload = {
        "memoryId": record.memory_id, "memoryType": record.memory_type, "userId": "product-test-user-abc",
        "entityIds": list(record.entity_ids), "statement": record.statement, "epistemicStatus": record.epistemic_status,
        "confidence": record.confidence, "sourceRequestId": record.source_request_id, "sourceConversationId": record.source_conversation_id,
        "evidenceRefs": [{"type": "canonical_ref", "id": record.evidence_refs[0]}], "provenanceCategory": record.provenance_category,
        "validFrom": record.valid_from, "validUntil": record.valid_until, "createdAt": record.created_at, "updatedAt": record.updated_at,
        "revision": record.revision, "status": record.status, "indexStatus": record.index_status,
        "idempotencyFingerprint": record.idempotency_fingerprint, "logicalMemoryKey": record.logical_memory_key, "policyVersion": record.policy_version,
    }
    fake = FakeProductClient(payload)
    stored = ProductLongTermMemoryStore(ProductMemoryClient(fake), local_test_user_id="product-test-user-abc", require_local_test_user=True).put(memory=record)
    assert fake.calls[0][2]["extra_headers"]["X-User-ID"] == "product-test-user-abc"
    assert stored.user_id == "local-user-123"
    assert stored.entity_ids == ("192.0.2.1",)


def _canonical_wire(record, *, status="candidate", revision=None, epistemic_status=None):
    return {
        "memoryId": record.memory_id, "memoryType": record.memory_type, "userId": record.user_id,
        "entityIds": list(record.entity_ids), "statement": record.statement,
        "epistemicStatus": epistemic_status or record.epistemic_status, "confidence": record.confidence,
        "sourceRequestId": record.source_request_id, "sourceConversationId": record.source_conversation_id,
        "evidenceRefs": [{"type": "canonical_ref", "id": item} for item in record.evidence_refs],
        "provenanceCategory": record.provenance_category, "validFrom": record.valid_from,
        "validUntil": record.valid_until, "createdAt": record.created_at, "updatedAt": record.updated_at,
        "revision": record.revision if revision is None else revision, "status": status,
        "indexStatus": record.index_status, "idempotencyFingerprint": record.idempotency_fingerprint,
        "logicalMemoryKey": record.logical_memory_key, "policyVersion": record.policy_version,
    }


def _candidate():
    return __import__("src.core.memory.long_term", fromlist=["LongTermMemoryRecord"]).LongTermMemoryRecord.candidate(
        memory_type="validated_finding", user_id="owner", entity_ids=("192.0.2.1",), statement="{\"value\":\"new\"}",
        source_request_id="request-1", source_conversation_id="chat-1",
        evidence_refs=("evidence_class_asset_identity",),
    )


@pytest.mark.parametrize(
    ("method_name", "action", "reason", "actor"),
    (
        ("invalidate", "invalidate", "explicit_invalidation", "copilot"),
        ("expire", "expire", "retention_or_validity_expired", "system"),
    ),
)
def test_product_ltm_terminal_transition_bodies_are_exact(method_name, action, reason, actor):
    record = _candidate()
    canonical = _canonical_wire(record, status="invalidated" if action == "invalidate" else "expired", revision=2)
    fake = FakeProductClient(canonical)
    store = ProductLongTermMemoryStore(ProductMemoryClient(fake))
    getattr(store, method_name)(user_id=record.user_id, memory_id=record.memory_id, expected_revision=1)
    body = fake.calls[0][2]["json_body"]
    assert body == {
        "action": action, "reasonCode": reason, "actor": actor,
        "policyVersion": "ltm-promotion-v1", "expectedRevision": 1,
    }


def test_product_ltm_reject_transition_body_is_exact():
    record = _candidate()
    canonical = _canonical_wire(record, status="rejected", revision=2)
    fake = FakeProductClient(lambda method, path, kwargs: {"items": []} if path.endswith("/search") else canonical)
    decision = PromotionDecision(
        "reject", "insufficient_authority", "reject", confidence=record.confidence,
        epistemic_status=record.epistemic_status, provenance_category=record.provenance_category,
    )
    ProductLongTermMemoryStore(ProductMemoryClient(fake)).apply_promotion(
        candidate=record, decision=decision, actor="copilot", request_id="request-current"
    )
    body = next(call[2]["json_body"] for call in fake.calls if call[1].endswith("/transition"))
    assert body["action"] == "reject"
    assert body["reasonCode"] == "insufficient_authority"
    assert body["expectedRevision"] == record.revision
    assert "sourceRequestId" not in body


def test_product_ltm_delete_reads_revision_then_sends_delete_transition():
    record = _candidate()
    fake = FakeProductClient(lambda method, path, kwargs: _canonical_wire(record) if method == "GET" else {"memoryId": record.memory_id})
    assert ProductLongTermMemoryStore(ProductMemoryClient(fake)).delete(
        user_id=record.user_id, memory_id=record.memory_id
    )
    body = fake.calls[1][2]["json_body"]
    assert body == {
        "action": "delete", "reasonCode": "quota_or_manual_cleanup", "actor": "copilot",
        "policyVersion": record.policy_version, "expectedRevision": record.revision,
    }


@pytest.mark.parametrize(
    ("same_statement", "expected_action", "expected_reason", "expected_resolution"),
    (
        (True, "confirm", "same_value_refresh", "same_value"),
        (False, "promote", "newer_compatible_value", "compatible_change"),
    ),
)
def test_product_ltm_existing_active_transition_body_is_exact(
    same_statement, expected_action, expected_reason, expected_resolution
):
    candidate = _candidate()
    active_base = candidate if same_statement else candidate.__class__.candidate(
        memory_type=candidate.memory_type, user_id=candidate.user_id, entity_ids=candidate.entity_ids,
        statement="{\"older\":true}", source_request_id="older-request",
        source_conversation_id=candidate.source_conversation_id, evidence_refs=candidate.evidence_refs,
    )
    active = replace(
        active_base, memory_id="active-memory", status="active", revision=5,
        epistemic_status="source_validated",
    )
    canonical = _canonical_wire(candidate, status="active", revision=2, epistemic_status="source_validated")
    fake = FakeProductClient(
        lambda method, path, kwargs: {"items": [_canonical_wire(active, status="active", revision=5)]}
        if path.endswith("/search") else canonical
    )
    decision = PromotionDecision(
        "auto_promote", "validated_product_fact", "eligible", confidence=0.95,
        epistemic_status="source_validated", provenance_category="product",
    )
    ProductLongTermMemoryStore(ProductMemoryClient(fake)).apply_promotion(
        candidate=candidate, decision=decision, actor="copilot", request_id="current-request"
    )
    body = next(call[2]["json_body"] for call in fake.calls if call[1].endswith("/transition"))
    assert body == {
        "action": expected_action,
        "reasonCode": expected_reason,
        "actor": "copilot",
        "policyVersion": candidate.policy_version,
        "expectedRevision": candidate.revision,
        "resolution": expected_resolution,
    }

def test_product_ltm_terse_create_hydrates_canonical_id_and_revision():
    candidate = _candidate()
    canonical = replace(candidate, memory_id="canonical-memory", revision=4)

    def payload(method, path, kwargs):
        if method == "POST":
            return {"memoryId": canonical.memory_id}
        return _canonical_wire(canonical)

    fake = FakeProductClient(payload)
    stored = ProductLongTermMemoryStore(ProductMemoryClient(fake)).put(memory=candidate)

    assert stored.memory_id == canonical.memory_id
    assert stored.revision == 4
    assert [(method, path) for method, path, _ in fake.calls] == [
        ("POST", "/configured/ltm"),
        ("GET", f"/configured/ltm/{canonical.memory_id}"),
    ]
    assert fake.calls[1][2]["request_id"] == candidate.source_request_id


def test_product_ltm_deduplicated_full_create_reuses_canonical_active_record():
    candidate = _candidate()
    canonical = replace(
        candidate,
        memory_id="existing-active-memory",
        revision=8,
        status="active",
        epistemic_status="source_validated",
        index_status="pending",
    )
    fake = FakeProductClient(
        _canonical_wire(
            canonical,
            status="active",
            revision=8,
            epistemic_status="source_validated",
        )
    )

    stored = ProductLongTermMemoryStore(ProductMemoryClient(fake)).put(memory=candidate)

    assert stored.memory_id == canonical.memory_id
    assert stored.revision == 8
    assert stored.status == "active"
    assert len(fake.calls) == 1


def test_product_ltm_transition_conflict_maps_stale_canonical_revision():
    candidate = _candidate()

    def payload(method, path, kwargs):
        if path.endswith("/search"):
            return {"items": []}
        if path.endswith("/transition"):
            raise ProductMemoryConflictError("stale")
        raise AssertionError(f"unexpected Product call: {method} {path}")

    fake = FakeProductClient(payload)
    decision = PromotionDecision(
        "auto_promote",
        "validated_product_fact",
        "eligible",
        confidence=0.95,
        epistemic_status="source_validated",
        provenance_category="product",
    )

    with pytest.raises(LocalPersistenceConflictError, match="revision is stale"):
        ProductLongTermMemoryStore(ProductMemoryClient(fake)).apply_promotion(
            candidate=candidate,
            decision=decision,
            actor="copilot",
            request_id="current-request",
        )

    transition = next(call for call in fake.calls if call[1].endswith("/transition"))
    assert transition[2]["json_body"]["expectedRevision"] == candidate.revision

def test_product_ltm_unresolved_conflict_stays_candidate_without_unsupported_transition():
    candidate = _candidate()
    active = replace(
        candidate,
        memory_id="active-memory",
        status="active",
        revision=5,
        epistemic_status="source_validated",
    )
    fake = FakeProductClient(
        lambda method, path, kwargs: {
            "items": [_canonical_wire(active, status="active", revision=5)]
        }
    )
    decision = PromotionDecision(
        "requires_review",
        "unresolved_material_conflict",
        "review required",
        confidence=candidate.confidence,
        epistemic_status=candidate.epistemic_status,
        provenance_category=candidate.provenance_category,
    )

    result = ProductLongTermMemoryStore(ProductMemoryClient(fake)).apply_promotion(
        candidate=candidate,
        decision=decision,
        request_id="current-request",
    )

    assert result.memory is candidate
    assert result.previous_memory == active
    assert result.conflict_count == 1
    assert [path for _, path, _ in fake.calls] == ["/configured/ltm/search"]

def test_product_candidate_is_not_indexed_before_successful_active_transition():
    entity = "192.0.2.10"
    statement = json.dumps(
        {
            "source_capability": "asset.get_profile",
            "entities": [entity],
            "evidence_classes": ["asset_role"],
            "selected_views": ["overview"],
            "schema_version": "product-view-v1",
            "completeness": "complete",
            "projection_complete": True,
            "evidence": {"views": {"overview": {"role": "server"}}},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    candidate = LongTermMemoryRecord.candidate(
        memory_type="validated_finding",
        user_id="owner",
        entity_ids=(entity,),
        statement=statement,
        source_request_id="request-index-order",
        source_conversation_id="chat-1",
        evidence_refs=("evidence_class_asset_role", "complete"),
        provenance_category="product",
    )

    def payload(method, path, kwargs):
        if path.endswith("/search"):
            return {"items": []}
        if path.endswith("/transition"):
            raise ProductMemoryConflictError("stale")
        if method == "POST":
            return _canonical_wire(candidate)
        raise AssertionError(f"unexpected Product call: {method} {path}")

    class RecordingIndex:
        def __init__(self):
            self.memory_ids = []

        def index(self, memory):
            self.memory_ids.append(memory.memory_id)
            return memory

    fake = FakeProductClient(payload)
    index = RecordingIndex()
    coordinator = LongTermMemoryCoordinator(
        ProductLongTermMemoryStore(ProductMemoryClient(fake)),
        index,
    )
    evidence = ToolResult(
        status="ok",
        entities=(entity,),
        source_capability="asset.get_profile",
        retrieved_at="2026-08-20T08:00:00+00:00",
        freshness="current",
        completeness="complete",
        selected_views=("overview",),
        view_payload={"views": {"overview": {"role": "server"}}},
        projection_schema_version="product-view-v1",
        source_payload_complete=True,
        projection_usable=True,
    )

    with pytest.raises(LocalPersistenceConflictError):
        coordinator.process_candidate(candidate, evidence)

    assert index.memory_ids == []
    transition = next(call for call in fake.calls if call[1].endswith("/transition"))
    assert transition[2]["json_body"] == {
        "expectedRevision": candidate.revision,
        "action": "promote",
        "reasonCode": "validated_product_fact",
        "actor": "system",
        "policyVersion": "ltm-promotion-v1",
        "resolution": "compatible_change",
    }
