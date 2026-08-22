"""Product-backed memory adapters; LTM lifecycle writes intentionally remain unavailable."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.core.identity import RequestIdentity
from src.core.memory.long_term import LongTermMemoryRecord, MemoryLifecycleAuditEvent, PromotionDecision
from src.core.memory.persistence import (
    LocalPersistenceConflictError, LocalPersistenceError, LocalPersistenceOwnershipError,
    ThreadMemoryState,
)
from src.core.product_client.memory_client import (
    ProductMemoryClient, ProductMemoryConflictError, ProductMemoryNotFoundError,
)


def _payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LocalPersistenceError("Product memory response was invalid.")
    return value.get("data", value) if isinstance(value.get("data", value), dict) else value


def _memory_from_payload(value: Any) -> LongTermMemoryRecord:
    item = _payload(value)
    refs = item.get("evidenceRefs", ())
    normalized_refs = tuple(
        str(ref.get("id")) if isinstance(ref, dict) and ref.get("type") == "canonical_ref" else str(ref)
        for ref in refs
    )
    return LongTermMemoryRecord(
        memory_id=item["memoryId"], memory_type=item["memoryType"], user_id=item["userId"],
        entity_ids=tuple(item.get("entityIds") or ()), statement=item["statement"],
        epistemic_status=item["epistemicStatus"], confidence=item["confidence"],
        source_request_id=item["sourceRequestId"], source_conversation_id=item["sourceConversationId"],
        evidence_refs=normalized_refs, provenance_category=item.get("provenanceCategory", "investigation"),
        valid_from=item["validFrom"], valid_until=item.get("validUntil"),
        created_at=item.get("createdAt", ""), updated_at=item.get("updatedAt", ""),
        revision=item.get("revision", 1), status=item.get("status", "candidate"),
        index_status=item.get("indexStatus", "not_indexed"), supersedes_memory_id=item.get("supersedesMemoryId"),
        idempotency_fingerprint=item.get("idempotencyFingerprint", ""),
        logical_memory_key=item.get("logicalMemoryKey", ""),
        has_unresolved_conflict=item.get("hasUnresolvedConflict", False), policy_version=item.get("policyVersion", "ltm-promotion-v1"),
    )


class ProductThreadStateStore:
    def __init__(self, client: ProductMemoryClient, *, local_test_user_id: str = "", require_local_test_user: bool = False) -> None:
        self.client = client
        self.local_test_user_id = str(local_test_user_id or "").strip()
        self.require_local_test_user = require_local_test_user

    @staticmethod
    def _require(identity: RequestIdentity) -> tuple[str, str]:
        if not identity.user_id or not identity.conversation_id:
            raise LocalPersistenceOwnershipError("Product thread persistence requires user and conversation identity.")
        return identity.user_id, identity.conversation_id

    def _owner(self, identity: RequestIdentity) -> tuple[str, str]:
        user_id, conversation_id = self._require(identity)
        if self.require_local_test_user and not self.local_test_user_id:
            raise LocalPersistenceOwnershipError("SOORIN_LOCAL_PRODUCT_TEST_USER_ID is required for local Product-memory tests.")
        return self.local_test_user_id or user_id, conversation_id

    def load(self, *, identity: RequestIdentity) -> ThreadMemoryState | None:
        user_id, conversation_id = self._owner(identity)
        try:
            item = _payload(self.client.get_thread_state(user_id=user_id, conversation_id=conversation_id, request_id=identity.request_id))
        except ProductMemoryNotFoundError:
            return None
        try:
            return ThreadMemoryState.from_payload(
                item["stateJson"], thread_key=identity.thread_key, user_id=identity.user_id,
                conversation_id=conversation_id, session_id=item.get("sessionId", identity.session_id),
                updated_at=item.get("updatedAt", ""), revision=int(item["revision"]),
                schema_version=int(item["schemaVersion"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalPersistenceError("Product thread state was invalid.") from exc

    def save(self, *, identity: RequestIdentity, state: ThreadMemoryState, expected_revision: int) -> ThreadMemoryState:
        user_id, conversation_id = self._owner(identity)
        if state.thread_key != identity.thread_key or state.conversation_id != conversation_id:
            raise LocalPersistenceOwnershipError("Product thread-state identity does not match the request.")
        body = {"sessionId": identity.session_id, "expectedRevision": expected_revision,
                "schemaVersion": state.schema_version, "conversationId": conversation_id, "stateJson": state.to_payload()}
        try:
            item = _payload(self.client.put_thread_state(user_id=user_id, conversation_id=conversation_id, body=body, request_id=identity.request_id))
        except ProductMemoryConflictError as exc:
            raise LocalPersistenceConflictError("Thread state revision is stale.") from exc
        try:
            return replace(state, session_id=item.get("sessionId", identity.session_id), revision=int(item["revision"]), updated_at=item.get("updatedAt", state.updated_at))
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalPersistenceError("Product thread-state save response was invalid.") from exc

    def delete(self, *, identity: RequestIdentity) -> bool:
        raise LocalPersistenceError("Product thread-state deletion is not implemented by the backend contract.")


class ProductLongTermMemoryStore:
    """Foundation adapter. It is deliberately not wired as a full runtime backend."""
    def __init__(self, client: ProductMemoryClient) -> None: self.client = client
    @staticmethod
    def _refs(refs: tuple[str, ...]) -> list[dict[str, str]]:
        return [{"type": "canonical_ref", "id": value} for value in refs]
    def get(self, *, user_id: str, memory_id: str) -> LongTermMemoryRecord | None:
        try: return _memory_from_payload(self.client.get_ltm(user_id=user_id, memory_id=memory_id))
        except ProductMemoryNotFoundError: return None
    def put(self, *, memory: LongTermMemoryRecord) -> LongTermMemoryRecord:
        body = {"memoryType": memory.memory_type, "statement": memory.statement, "epistemicStatus": memory.epistemic_status,
                "confidence": memory.confidence, "sourceRequestId": memory.source_request_id, "sourceConversationId": memory.source_conversation_id,
                "evidenceRefs": self._refs(memory.evidence_refs), "provenanceCategory": memory.provenance_category,
                "validFrom": memory.valid_from, "validUntil": memory.valid_until, "entityIds": list(memory.entity_ids),
                "idempotencyFingerprint": memory.idempotency_fingerprint, "logicalMemoryKey": memory.logical_memory_key,
                "policyVersion": memory.policy_version, "actor": "copilot"}
        response = _payload(self.client.create_ltm(user_id=memory.user_id, body=body, request_id=memory.source_request_id))
        if "memoryType" not in response:
            memory_id = response.get("memoryId")
            if not memory_id: raise LocalPersistenceError("Product LTM create response was invalid.")
            created = self.get(user_id=memory.user_id, memory_id=str(memory_id))
            if created is None: raise LocalPersistenceError("Product LTM create could not hydrate canonical record.")
            return created
        return _memory_from_payload(response)
    def list(self, *, user_id: str, entity_ids=(), memory_types=(), statuses=("active",), epistemic_statuses=(), limit: int = 100):
        body = {"entityIds": list(entity_ids), "memoryTypes": list(memory_types), "statuses": list(statuses),
                "epistemicStatuses": list(epistemic_statuses), "limit": limit, "offset": 0}
        result = _payload(self.client.search_ltm(user_id=user_id, body=body))
        items = result.get("items", result.get("memories", []))
        return tuple(_memory_from_payload(item) for item in items)
    def list_audit_events(self, *, user_id: str, memory_id: str | None = None, limit: int = 100):
        if not memory_id: raise LocalPersistenceError("Product audit reads require a memory id.")
        result = _payload(self.client.list_ltm_audit(user_id=user_id, memory_id=memory_id))
        return tuple(MemoryLifecycleAuditEvent(event_id=item["eventId"], memory_id=item["memoryId"], logical_memory_key=item["logicalMemoryKey"], idempotency_fingerprint=item["idempotencyFingerprint"], user_id=item["userId"], entity_ids=tuple(item.get("entityIds") or ()), source_request_id=item["sourceRequestId"], action=item["action"], from_status=item["fromStatus"], to_status=item["toStatus"], reason_code=item["reasonCode"], policy_version=item["policyVersion"], evidence_refs=tuple(item.get("evidenceRefs") or ()), actor=item["actor"], created_at=item["createdAt"]) for item in result.get("items", result.get("events", []))[:limit])
    def _unsupported(self, *args, **kwargs): raise LocalPersistenceError("Product LTM lifecycle transitions are not implemented.")
    update = apply_promotion = invalidate = expire = supersede = delete = _unsupported
