from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.core.identity import RequestIdentity
from src.core.memory.long_term import PromotionDecision
from src.core.memory.persistence import ThreadMemoryState
from src.core.memory.product import ProductLongTermMemoryStore, ProductThreadStateStore
from src.core.product_client.memory_client import ProductMemoryClient, ProductMemoryNotFoundError


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
    state = ThreadMemoryState.from_routing_state(identity, __import__("src.core.memory.routing_state", fromlist=["SessionRoutingState"]).SessionRoutingState())
    saved = store.save(identity=identity, state=state, expected_revision=0)
    assert saved.revision == 1
    body = fake.calls[0][2]["json_body"]
    assert body["conversationId"] == "chat-1" and body["stateJson"] == state.to_payload()


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
    assert kwargs["json_body"]["action"] == "promote"
    assert kwargs["json_body"]["reasonCode"] == "validated_product_fact"
    assert kwargs["json_body"]["expectedRevision"] == record.revision
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
