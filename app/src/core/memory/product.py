"""Product-backed memory adapters; Product is the canonical authority in product mode."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from src.core.identity import RequestIdentity
from src.core.memory.long_term import LongTermMemoryRecord, MemoryLifecycleAuditEvent, MemoryLifecycleResult, PromotionDecision
from src.core.memory.persistence import LocalPersistenceConflictError, LocalPersistenceError, LocalPersistenceOwnershipError, ThreadMemoryState
from src.core.observability.metrics import get_metrics
from src.core.product_client.memory_client import ProductMemoryClient, ProductMemoryConflictError, ProductMemoryNotFoundError


logger = logging.getLogger(__name__)


def _object(value: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LocalPersistenceError("Product memory response was invalid.")
    item: Any = value.get("data", value)
    if not isinstance(item, dict):
        raise LocalPersistenceError("Product memory response was invalid.")
    for key in keys:
        if key in item:
            item = item[key]
            if not isinstance(item, dict):
                raise LocalPersistenceError("Product memory response was invalid.")
            break
    return item


def _refs_from_wire(refs: Any) -> tuple[str, ...]:
    if not isinstance(refs, list):
        raise LocalPersistenceError("Product memory evidence references were invalid.")
    values: list[str] = []
    for ref in refs:
        if isinstance(ref, dict):
            if ref.get("type") != "canonical_ref" or not isinstance(ref.get("id"), str):
                raise LocalPersistenceError("Product memory evidence reference is not lossless.")
            values.append(ref["id"])
        elif isinstance(ref, str):
            values.append(ref)
        else:
            raise LocalPersistenceError("Product memory evidence reference was invalid.")
    return tuple(values)


def _memory_from_wire(value: Any) -> LongTermMemoryRecord:
    item = _object(value, "memory", "record")
    required = ("memoryId", "memoryType", "userId", "statement", "epistemicStatus", "confidence", "sourceRequestId", "sourceConversationId", "evidenceRefs", "validFrom", "revision", "status", "indexStatus", "idempotencyFingerprint", "logicalMemoryKey")
    if any(key not in item for key in required):
        raise LocalPersistenceError("Product memory canonical response was incomplete.")
    try:
        return LongTermMemoryRecord(
            memory_id=item["memoryId"], memory_type=item["memoryType"], user_id=item["userId"],
            entity_ids=tuple(item.get("entityIds") or ()), statement=item["statement"], epistemic_status=item["epistemicStatus"],
            confidence=item["confidence"], source_request_id=item["sourceRequestId"], source_conversation_id=item["sourceConversationId"],
            evidence_refs=_refs_from_wire(item["evidenceRefs"]), provenance_category=item.get("provenanceCategory", "investigation"),
            valid_from=item["validFrom"], valid_until=item.get("validUntil"), created_at=item.get("createdAt", ""), updated_at=item.get("updatedAt", ""),
            revision=int(item["revision"]), status=item["status"], index_status=item["indexStatus"], supersedes_memory_id=item.get("supersedesMemoryId"),
            idempotency_fingerprint=item["idempotencyFingerprint"], logical_memory_key=item["logicalMemoryKey"],
            has_unresolved_conflict=bool(item.get("hasUnresolvedConflict", False)), policy_version=item.get("policyVersion", "ltm-promotion-v1"),
        )
    except (TypeError, ValueError) as exc:
        raise LocalPersistenceError("Product memory canonical response was invalid.") from exc


class ProductThreadStateStore:
    def __init__(self, client: ProductMemoryClient, *, local_test_user_id: str = "", require_local_test_user: bool = False) -> None:
        self.client, self.local_test_user_id, self.require_local_test_user = client, str(local_test_user_id or "").strip(), require_local_test_user
    def _owner(self, identity: RequestIdentity) -> tuple[str, str]:
        if not identity.user_id or not identity.conversation_id:
            raise LocalPersistenceOwnershipError("Product thread persistence requires user and conversation identity.")
        if self.require_local_test_user and not self.local_test_user_id:
            raise LocalPersistenceOwnershipError("SOORIN_LOCAL_PRODUCT_TEST_USER_ID is required for local Product-memory tests.")
        return self.local_test_user_id or identity.user_id, identity.conversation_id
    def load(self, *, identity: RequestIdentity) -> ThreadMemoryState | None:
        owner, conversation_id = self._owner(identity)
        try: item = _object(self.client.get_thread_state(user_id=owner, conversation_id=conversation_id, request_id=identity.request_id))
        except ProductMemoryNotFoundError: return None
        try:
            return ThreadMemoryState.from_payload(item["stateJson"], thread_key=identity.thread_key, user_id=identity.user_id, conversation_id=conversation_id, session_id=item.get("sessionId", identity.session_id), updated_at=item.get("updatedAt", ""), revision=int(item["revision"]), schema_version=int(item["schemaVersion"]))
        except (KeyError, TypeError, ValueError) as exc: raise LocalPersistenceError("Product thread state was invalid.") from exc
    def save(self, *, identity: RequestIdentity, state: ThreadMemoryState, expected_revision: int) -> ThreadMemoryState:
        owner, conversation_id = self._owner(identity)
        if expected_revision < 0 or state.schema_version < 1:
            raise LocalPersistenceConflictError("Product thread-state revision or schema version is invalid.")
        if state.thread_key != identity.thread_key or state.user_id != identity.user_id or state.conversation_id != conversation_id or state.session_id != identity.session_id:
            raise LocalPersistenceOwnershipError("Product thread-state identity does not match the request.")
        body = {"sessionId": identity.session_id, "expectedRevision": expected_revision, "schemaVersion": state.schema_version, "conversationId": conversation_id, "stateJson": state.to_payload()}
        try: item = _object(self.client.put_thread_state(user_id=owner, conversation_id=conversation_id, body=body, request_id=identity.request_id))
        except ProductMemoryConflictError as exc: raise LocalPersistenceConflictError("Thread state revision is stale.") from exc
        try: return replace(state, session_id=item.get("sessionId", identity.session_id), revision=int(item["revision"]), updated_at=item.get("updatedAt", state.updated_at))
        except (KeyError, TypeError, ValueError) as exc: raise LocalPersistenceError("Product thread-state save response was invalid.") from exc
    def delete(self, *, identity: RequestIdentity) -> bool: raise LocalPersistenceError("Product thread-state deletion is not implemented by the backend contract.")


class ProductLongTermMemoryStore:
    """Product/PostgreSQL canonical LTM adapter with one mutation per decision."""

    def __init__(
        self,
        client: ProductMemoryClient,
        *,
        local_test_user_id: str = "",
        require_local_test_user: bool = False,
    ) -> None:
        self.client = client
        self.local_test_user_id = str(local_test_user_id or "").strip()
        self.require_local_test_user = require_local_test_user

    def _owner(self, domain_user_id: str) -> str:
        if not domain_user_id:
            raise LocalPersistenceOwnershipError("Product long-term memory requires an owner.")
        if self.require_local_test_user and not self.local_test_user_id:
            raise LocalPersistenceOwnershipError("SOORIN_LOCAL_PRODUCT_TEST_USER_ID is required for local Product-memory tests.")
        return self.local_test_user_id or domain_user_id

    def _domain_record(self, value: Any, *, domain_user_id: str) -> LongTermMemoryRecord:
        item = _object(value, "memory", "record")
        if item.get("userId") != self._owner(domain_user_id):
            raise LocalPersistenceOwnershipError("Product long-term memory owner did not match the transport owner.")
        # Product's transport owner can be a local test account while the
        # fingerprint/logical key deliberately remain bound to the domain owner.
        return _memory_from_wire({**item, "userId": domain_user_id})

    @staticmethod
    def _refs(refs: tuple[str, ...]) -> list[dict[str, str]]: return [{"type": "canonical_ref", "id": ref} for ref in refs]
    @classmethod
    def _wire(cls, memory: LongTermMemoryRecord) -> dict[str, Any]:
        return {"memoryType": memory.memory_type, "statement": memory.statement, "epistemicStatus": memory.epistemic_status, "confidence": memory.confidence, "sourceRequestId": memory.source_request_id, "sourceConversationId": memory.source_conversation_id, "evidenceRefs": cls._refs(memory.evidence_refs), "provenanceCategory": memory.provenance_category, "validFrom": memory.valid_from, "validUntil": memory.valid_until, "entityIds": list(memory.entity_ids), "idempotencyFingerprint": memory.idempotency_fingerprint, "logicalMemoryKey": memory.logical_memory_key, "policyVersion": memory.policy_version}
    def get(self, *, user_id: str, memory_id: str) -> LongTermMemoryRecord | None:
        try:
            return self._domain_record(
                self.client.get_ltm(user_id=self._owner(user_id), memory_id=memory_id),
                domain_user_id=user_id,
            )
        except ProductMemoryNotFoundError: return None
    def put(self, *, memory: LongTermMemoryRecord) -> LongTermMemoryRecord:
        response = _object(self.client.create_ltm(user_id=self._owner(memory.user_id), body={**self._wire(memory), "actor": "copilot"}, request_id=memory.source_request_id))
        return self._domain_record(response, domain_user_id=memory.user_id) if "memoryType" in response else self._hydrate(memory.user_id, response.get("memoryId"))
    def _hydrate(self, user_id: str, memory_id: Any) -> LongTermMemoryRecord:
        if not isinstance(memory_id, str) or not memory_id:
            get_metrics().observe_memory_canonical_reload("product", "failure")
            raise LocalPersistenceError("Product memory response omitted memoryId.")
        try:
            memory = self.get(user_id=user_id, memory_id=memory_id)
        except Exception:
            get_metrics().observe_memory_canonical_reload("product", "failure")
            raise
        if memory is None:
            get_metrics().observe_memory_canonical_reload("product", "failure")
            raise LocalPersistenceError("Product memory canonical readback was unavailable.")
        get_metrics().observe_memory_canonical_reload("product", "success")
        return memory
    def list(self, *, user_id: str, entity_ids=(), memory_types=(), statuses=("active",), epistemic_statuses=(), limit: int = 100, logical_memory_key: str = ""):
        body = {"entityIds": list(entity_ids), "memoryTypes": list(memory_types), "statuses": list(statuses), "epistemicStatuses": list(epistemic_statuses), "limit": limit, "offset": 0}
        if logical_memory_key: body["logicalMemoryKey"] = logical_memory_key
        result = _object(self.client.search_ltm(user_id=self._owner(user_id), body=body))
        records = next((result[name] for name in ("records", "items", "memories") if name in result), None)
        if not isinstance(records, list): raise LocalPersistenceError("Product memory search response was invalid.")
        return tuple(self._domain_record(item, domain_user_id=user_id) for item in records)
    def list_audit_events(self, *, user_id: str, memory_id: str | None = None, limit: int = 100):
        if not memory_id: raise LocalPersistenceError("Product audit reads require a memory id.")
        result = _object(self.client.list_ltm_audit(user_id=self._owner(user_id), memory_id=memory_id))
        events = result.get("events", result.get("items"))
        if not isinstance(events, list): raise LocalPersistenceError("Product memory audit response was invalid.")
        try:
            return tuple(MemoryLifecycleAuditEvent(event_id=e["eventId"], memory_id=e["memoryId"], logical_memory_key=e["logicalMemoryKey"], idempotency_fingerprint=e["idempotencyFingerprint"], user_id=e["userId"], entity_ids=tuple(e.get("entityIds") or ()), source_request_id=e["sourceRequestId"], action=e["action"], from_status=e["fromStatus"], to_status=e["toStatus"], reason_code=e["reasonCode"], policy_version=e["policyVersion"], evidence_refs=_refs_from_wire(e["evidenceRefs"]), actor=e["actor"], created_at=e["createdAt"]) for e in events[:limit])
        except (KeyError, TypeError, ValueError) as exc: raise LocalPersistenceError("Product memory audit event was invalid.") from exc
    def _transition(
        self,
        *,
        user_id: str,
        memory_id: str,
        action: str,
        reason_code: str,
        actor: str = "copilot",
        expected_revision: int,
        policy_version: str,
        request_id: str = "",
        related_memory_id: str | None = None,
        related_expected_revision: int | None = None,
        hydrate: bool = True,
    ) -> tuple[LongTermMemoryRecord | None, dict[str, Any]]:
        body = {
            "action": action,
            "reasonCode": reason_code,
            "actor": actor,
            "policyVersion": policy_version,
            "sourceRequestId": request_id,
            "expectedRevision": expected_revision,
        }
        if related_memory_id:
            body["relatedMemoryId"] = related_memory_id
        if related_expected_revision is not None:
            body["relatedExpectedRevision"] = related_expected_revision
        try: response = _object(self.client.transition_ltm(user_id=self._owner(user_id), memory_id=memory_id, body=body, request_id=request_id))
        except ProductMemoryConflictError as exc: raise LocalPersistenceConflictError("Long-term memory revision is stale.") from exc
        if "memoryType" in response:
            return self._domain_record(response, domain_user_id=user_id), response
        return (self._hydrate(user_id, response.get("memoryId", memory_id)) if hydrate else None), response

    def _active_for(self, candidate: LongTermMemoryRecord) -> LongTermMemoryRecord | None:
        active = self.list(user_id=candidate.user_id, statuses=("active",), limit=2, logical_memory_key=candidate.logical_memory_key)
        return next((record for record in active if record.logical_memory_key == candidate.logical_memory_key), None)

    def apply_promotion(self, *, candidate: LongTermMemoryRecord, decision: PromotionDecision, actor: str = "system") -> MemoryLifecycleResult:
        active = self._active_for(candidate)
        if decision.action in {"keep_candidate", "requires_review"}:
            if decision.reason_code != "unresolved_material_conflict" or active is None:
                return MemoryLifecycleResult(self.get(user_id=candidate.user_id, memory_id=candidate.memory_id) or candidate, decision, previous_memory=active)
            action, reason_code = "conflict", decision.reason_code
        elif decision.action == "reject":
            action, reason_code = "reject", decision.reason_code
        elif active is None:
            action, reason_code = "promote", decision.reason_code
        elif active.statement == candidate.statement:
            action, reason_code = "confirm", "same_value_revalidated"
        else:
            action, reason_code = "supersede", "newer_compatible_value"
        current, result = self._transition(user_id=candidate.user_id, memory_id=candidate.memory_id, action=action, reason_code=reason_code, actor=actor or "copilot", expected_revision=candidate.revision, policy_version=decision.policy_version, request_id=candidate.source_request_id, related_memory_id=active.memory_id if active is not None else None, related_expected_revision=active.revision if active is not None else None)
        if current is None: raise LocalPersistenceError("Product promotion did not return a canonical memory.")
        previous = result.get("relatedRecord", result.get("related_record"))
        return MemoryLifecycleResult(current, decision, previous_memory=self._domain_record(previous, domain_user_id=candidate.user_id) if isinstance(previous, dict) else active, deduplicated=action == "confirm" or bool(result.get("deduplicated", False)), superseded_count=1 if action == "supersede" else int(result.get("supersededCount", 0)), conflict_count=1 if action == "conflict" else int(result.get("conflictCount", 0)))
    def update(self, *, memory: LongTermMemoryRecord, expected_revision: int) -> LongTermMemoryRecord:
        if memory.revision != expected_revision + 1: raise LocalPersistenceConflictError("Long-term memory revision is invalid.")
        logger.info("event=product_ltm_index_status_local_only memory_id_present=true")
        return memory
    def invalidate(self, *, user_id: str, memory_id: str, expected_revision: int) -> LongTermMemoryRecord:
        updated, _ = self._transition(user_id=user_id, memory_id=memory_id, action="invalidate", reason_code="explicit_invalidation", actor="copilot", expected_revision=expected_revision, policy_version="ltm-promotion-v1")
        if updated is None: raise LocalPersistenceError("Product invalidation did not return a canonical memory.")
        return updated
    def expire(self, *, user_id: str, memory_id: str, expected_revision: int) -> LongTermMemoryRecord:
        updated, _ = self._transition(user_id=user_id, memory_id=memory_id, action="expire", reason_code="retention_or_validity_expired", actor="system", expected_revision=expected_revision, policy_version="ltm-promotion-v1")
        if updated is None: raise LocalPersistenceError("Product expiration did not return a canonical memory.")
        return updated
    def supersede(self, *, user_id: str, memory_id: str, replacement: LongTermMemoryRecord, expected_revision: int) -> tuple[LongTermMemoryRecord, LongTermMemoryRecord]:
        current, result = self._transition(user_id=user_id, memory_id=replacement.memory_id, action="supersede", reason_code="newer_compatible_value", expected_revision=replacement.revision, policy_version=replacement.policy_version, request_id=replacement.source_request_id, related_memory_id=memory_id, related_expected_revision=expected_revision)
        if current is None: raise LocalPersistenceError("Product supersession did not return a canonical memory.")
        previous = result.get("relatedRecord", result.get("related_record"))
        if not isinstance(previous, dict):
            raise LocalPersistenceError("Product supersession response omitted the prior canonical memory.")
        return _memory_from_wire(previous), current
    def delete(self, *, user_id: str, memory_id: str) -> bool:
        memory = self.get(user_id=user_id, memory_id=memory_id)
        if memory is None: return False
        self._transition(user_id=user_id, memory_id=memory_id, action="delete", reason_code="quota_or_manual_cleanup", expected_revision=memory.revision, policy_version=memory.policy_version, hydrate=False)
        return True
