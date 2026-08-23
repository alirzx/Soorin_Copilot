"""Offline Gate 6/7 tests for typed canonical and semantic memory."""

from __future__ import annotations

import math
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config.settings import get_settings
from src.core.memory.long_term import (
    LongTermMemoryRecord,
    MemoryPromotionPolicy,
    RetrievedLongTermMemory,
    retrieval_document,
    utc_now,
)
from src.core.agent.contracts import ToolResult
from src.core.memory.persistence import (
    LOCAL_SCHEMA_VERSION,
    LocalPersistenceConflictError,
    LocalPersistenceOwnershipError,
    LocalPersistenceQuotaError,
    MemoryStoragePolicy,
)
from src.core.memory.retrieval import (
    LazyCrossEncoderReranker,
    LongTermMemoryCoordinator,
    LongTermMemoryRetriever,
    MemorySemanticIndex,
)
from src.core.memory.sqlite import LocalSQLiteDatabase
from src.core.memory.sqlite_long_term import SQLiteLongTermMemoryStore
from src.core.memory.store import MemoryStore
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.context.models import EntityResolution, ResolvedEntity
from src.core.identity import RequestIdentity
from src.core.memory.retrieval import LongTermMemorySelection
from src.core.memory.routing_state import SessionRoutingState
from src.core.rag.embeddings import EmbeddingHealth
from src.core.rag.vector_store import (
    VectorCollectionInfo,
    VectorRecord,
    VectorSearchHit,
    VectorStoreHealth,
)


def candidate(
    *,
    user: str = "user-a",
    entity: str = "192.0.2.10",
    statement: str = "This asset is an approved vulnerability scanner.",
    memory_type: str = "validated_finding",
    evidence_refs: tuple[str, ...] = ("finding-7",),
) -> LongTermMemoryRecord:
    return LongTermMemoryRecord.candidate(
        memory_type=memory_type,
        user_id=user,
        entity_ids=(entity,),
        statement=statement,
        source_request_id="req-1",
        source_conversation_id="conv-1",
        evidence_refs=evidence_refs,
        confidence=0.4,
    )


def active(**kwargs) -> LongTermMemoryRecord:
    item = candidate(**kwargs)
    return MemoryPromotionPolicy.promote(
        item,
        epistemic_status="analyst_confirmed",
        confidence=0.95,
        provenance_category="analyst",
    )


def structured_candidate(
    *,
    user: str = "user-a",
    entity: str = "192.0.2.10",
    role: str = "server",
    provenance: str = "product",
    memory_type: str = "validated_finding",
) -> LongTermMemoryRecord:
    statement = json.dumps({
        "source_capability": "asset.get_profile",
        "entities": [entity],
        "evidence_classes": ["asset_role"],
        "selected_views": ["overview"],
        "schema_version": "product-view-v1",
        "completeness": "complete",
        "projection_complete": True,
        "evidence": {"views": {"overview": {"role": role}}},
    }, sort_keys=True, separators=(",", ":"))
    return LongTermMemoryRecord.candidate(
        memory_type=memory_type,
        user_id=user,
        entity_ids=(entity,),
        statement=statement,
        source_request_id=f"request-{role}",
        source_conversation_id="conversation-a",
        evidence_refs=("evidence_class_asset_role", "complete"),
        provenance_category=provenance,
    )


def product_evidence(
    *,
    entity: str = "192.0.2.10",
    role: str = "server",
    contradictions=(),
) -> ToolResult:
    return ToolResult(
        status="ok",
        entities=(entity,),
        source_capability="asset.get_profile",
        retrieved_at="2026-08-16T00:00:00+00:00",
        freshness="current",
        completeness="complete",
        contradictions=tuple(contradictions),
        selected_views=("overview",),
        view_payload={"views": {"overview": {"role": role}}},
        projection_schema_version="product-view-v1",
        source_payload_complete=True,
        projection_usable=True,
    )


class FakeEmbedder:
    model_name = "fake-bge"
    dimension = 3

    def __init__(self) -> None:
        self.query_calls = 0
        self.document_calls = 0

    def health(self) -> EmbeddingHealth:
        return EmbeddingHealth("ok", self.model_name, self.dimension, False)

    @staticmethod
    def _vector(text: str) -> list[float]:
        raw = [float(text.lower().count(word) + 1) for word in ("scanner", "domain", "false")]
        norm = math.sqrt(sum(value * value for value in raw))
        return [value / norm for value in raw]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._vector(text)

    def embed_documents(self, texts) -> list[list[float]]:
        self.document_calls += 1
        return [self._vector(text) for text in texts]


class FakeVectorStore:
    backend = "fake"

    def __init__(self) -> None:
        self.records: dict[str, VectorRecord] = {}
        self.fail_upsert = False
        self.last_filters = None

    def health(self):
        return VectorStoreHealth("ok", "fake", "memory", True, True, 3, "cosine")

    def ensure_collection(self):
        return VectorCollectionInfo("memory", 3, "cosine", len(self.records))

    def collection_info(self):
        return self.ensure_collection()

    def upsert(self, records):
        if self.fail_upsert:
            raise RuntimeError("offline index unavailable")
        for record in records:
            self.records[record.id] = record
        return len(records)

    def delete(self, ids):
        for identifier in ids:
            self.records.pop(identifier, None)
        return len(ids)

    def search(self, query_vector, *, top_k, filters=None):
        self.last_filters = filters
        hits = []
        for identifier, record in self.records.items():
            if filters and any(record.payload.get(key) != value for key, value in filters.items()):
                continue
            score = sum(a * b for a, b in zip(query_vector, record.vector))
            hits.append(VectorSearchHit(f"point-{identifier}", score, record.payload))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]


class FakeReranker:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def rerank(self, query, documents):
        self.calls.append(tuple(documents))
        if self.fail:
            raise RuntimeError("reranker unavailable")
        return [float(index) for index, _ in enumerate(documents)]


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SQLiteLongTermMemoryStore:
    database = LocalSQLiteDatabase(tmp_path / "memory.sqlite3")
    database.initialize()
    return SQLiteLongTermMemoryStore(database)


def test_canonical_model_projection_and_promotion_policy() -> None:
    item = candidate()
    assert item.status == "candidate"
    assert not item.authoritative
    promoted = MemoryPromotionPolicy.promote(
        item,
        epistemic_status="analyst_confirmed",
        confidence=1.0,
        provenance_category="analyst",
    )
    assert promoted.authoritative
    assert promoted.revision == item.revision + 1
    assert promoted.evidence_refs == item.evidence_refs
    with pytest.raises(ValueError):
        MemoryPromotionPolicy.promote(
            item,
            epistemic_status="unconfirmed",
            confidence=0.5,
            provenance_category="investigation",
        )
    projection = retrieval_document(promoted)
    assert promoted.statement in projection.text
    assert promoted.memory_id not in projection.text
    assert promoted.user_id not in projection.text
    assert "req-1" not in projection.text
    assert len(projection.text) <= 2400


def test_canonical_model_rejects_unbounded_or_unvalidated_records() -> None:
    with pytest.raises(ValueError):
        candidate(statement="x" * 2001)
    with pytest.raises(ValueError):
        replace(candidate(), status="active", epistemic_status="unconfirmed")
    with pytest.raises(ValueError):
        candidate(statement="Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    correction = candidate(memory_type="analyst_correction")
    with pytest.raises(ValueError):
        MemoryPromotionPolicy.promote(
            correction,
            epistemic_status="source_validated",
            confidence=0.8,
            provenance_category="investigation",
        )


def test_settings_validate_disabled_local_and_separate_collection() -> None:
    base = get_settings()
    replace(
        base,
        long_term_memory_enabled=False,
        local_sqlite_path="",
    ).validate_long_term_memory_configuration()
    with pytest.raises(ValueError):
        replace(
            base,
            long_term_memory_enabled=True,
            long_term_memory_backend="sqlite",
            memory_vector_index_enabled=True,
            memory_qdrant_collection=base.rag_collection,
        ).validate_long_term_memory_configuration()
    with pytest.raises(ValueError):
        replace(
            base,
            long_term_memory_enabled=True,
            memory_rerank_enabled=True,
            memory_rerank_model="",
        ).validate_long_term_memory_configuration()


def test_sqlite_v4_migration_and_lifecycle(sqlite_store, tmp_path: Path) -> None:
    memory = candidate()
    sqlite_store.put(memory=memory)
    assert sqlite_store.get(user_id="user-a", memory_id=memory.memory_id) == memory
    with pytest.raises(LocalPersistenceOwnershipError):
        sqlite_store.get(user_id="user-b", memory_id=memory.memory_id)
    promoted = MemoryPromotionPolicy.promote(
        memory,
        epistemic_status="source_validated",
        confidence=0.9,
        provenance_category="product",
    )
    sqlite_store.update(memory=promoted, expected_revision=memory.revision)
    assert sqlite_store.list(user_id="user-a", entity_ids=("192.0.2.10",)) == (promoted,)
    with pytest.raises(LocalPersistenceConflictError):
        sqlite_store.update(memory=replace(promoted, revision=3), expected_revision=1)
    replacement = candidate(entity="192.0.2.10", statement="Replacement validated scanner fact.")
    superseded, replacement = sqlite_store.supersede(
        user_id="user-a",
        memory_id=memory.memory_id,
        replacement=replacement,
        expected_revision=promoted.revision,
    )
    assert superseded.status == "superseded"
    assert replacement.supersedes_memory_id == memory.memory_id
    invalid = sqlite_store.invalidate(
        user_id="user-a", memory_id=replacement.memory_id, expected_revision=replacement.revision
    )
    assert invalid.status == "invalidated"
    assert sqlite_store.list(user_id="user-a") == ()
    assert sqlite_store.delete(user_id="user-a", memory_id=memory.memory_id)

    # Reopening preserves data/schema; a synthetic v3 database migrates explicitly.
    path = tmp_path / "v3.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO schema_metadata VALUES ('local_schema_version', '3')")
    connection.commit()
    connection.close()
    migrated = LocalSQLiteDatabase(path)
    migrated.initialize()
    with migrated.connect() as connection:
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key='local_schema_version'"
        ).fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='local_long_term_memories'"
        ).fetchone()
    assert version == str(LOCAL_SCHEMA_VERSION)
    assert table is not None


def test_exact_candidate_writes_are_idempotent_and_scope_safe(sqlite_store) -> None:
    first = candidate()
    replay = candidate()

    stored_first = sqlite_store.put(memory=first)
    stored_replay = sqlite_store.put(memory=replay)

    assert stored_replay.memory_id == stored_first.memory_id
    assert stored_replay.idempotency_fingerprint == stored_first.idempotency_fingerprint
    assert len(sqlite_store.list(user_id="user-a", statuses=("candidate",))) == 1

    changed = sqlite_store.put(memory=candidate(statement="Changed validated evidence."))
    other_entity = sqlite_store.put(memory=candidate(entity="192.0.2.11"))
    other_user = sqlite_store.put(memory=candidate(user="user-b"))
    assert len({stored_first.memory_id, changed.memory_id, other_entity.memory_id}) == 3
    assert other_user.memory_id != stored_first.memory_id


def test_invalidated_candidate_does_not_block_a_later_equivalent_write(sqlite_store) -> None:
    first = sqlite_store.put(memory=candidate())
    sqlite_store.invalidate(
        user_id=first.user_id,
        memory_id=first.memory_id,
        expected_revision=first.revision,
    )

    replacement = sqlite_store.put(memory=candidate())

    assert replacement.memory_id != first.memory_id
    assert len(sqlite_store.list(user_id="user-a", statuses=("candidate",))) == 1


def test_long_term_quotas_evict_candidates_but_preserve_authoritative_records(
    tmp_path: Path,
) -> None:
    database = LocalSQLiteDatabase(tmp_path / "bounded-memory.sqlite3")
    database.initialize()
    store = SQLiteLongTermMemoryStore(
        database,
        MemoryStoragePolicy(
            max_candidate_long_term_per_user=1,
            max_active_long_term_per_user=1,
        ),
    )
    first = store.put(memory=candidate(statement="First candidate."))
    second = store.put(memory=candidate(statement="Second candidate."))
    assert store.get(user_id="user-a", memory_id=first.memory_id) is None
    assert store.get(user_id="user-a", memory_id=second.memory_id) == second

    promoted = MemoryPromotionPolicy.promote(
        second,
        epistemic_status="analyst_confirmed",
        confidence=1.0,
        provenance_category="analyst",
    )
    store.update(memory=promoted, expected_revision=second.revision)
    another = store.put(memory=candidate(statement="Third candidate."))
    another_promoted = MemoryPromotionPolicy.promote(
        another,
        epistemic_status="analyst_confirmed",
        confidence=1.0,
        provenance_category="analyst",
    )
    with pytest.raises(LocalPersistenceQuotaError):
        store.update(memory=another_promoted, expected_revision=another.revision)
    assert store.get(user_id="user-a", memory_id=promoted.memory_id) == promoted


def test_safe_structured_candidate_auto_promotes_and_audits(sqlite_store) -> None:
    coordinator = LongTermMemoryCoordinator(sqlite_store, None)
    result = coordinator.process_candidate(structured_candidate(), product_evidence())

    assert result.decision.action == "auto_promote"
    assert result.memory.status == "active"
    assert result.memory.authoritative
    assert result.memory.logical_memory_key
    actions = {
        event.action
        for event in sqlite_store.list_audit_events(
            user_id="user-a", memory_id=result.memory.memory_id
        )
    }
    assert {"candidate_created", "promotion_evaluated", "promoted"} <= actions


@pytest.mark.parametrize(
    ("memory", "expected_action"),
    (
        (structured_candidate(provenance="analyst"), "requires_review"),
        (structured_candidate(memory_type="hypothesis_resolution"), "requires_review"),
    ),
)
def test_unsafe_or_analyst_candidates_never_auto_promote(
    sqlite_store,
    memory: LongTermMemoryRecord,
    expected_action: str,
) -> None:
    result = LongTermMemoryCoordinator(sqlite_store, None).process_candidate(
        memory, product_evidence()
    )
    assert result.decision.action == expected_action
    assert result.memory.status == "candidate"
    assert not result.memory.authoritative


def test_newer_same_value_confirms_without_duplicate_active(sqlite_store) -> None:
    coordinator = LongTermMemoryCoordinator(sqlite_store, None)
    first = coordinator.process_candidate(structured_candidate(), product_evidence())
    replay_with_new_reference = replace(
        structured_candidate(),
        evidence_refs=("evidence_class_asset_role", "complete", "validation-2"),
        idempotency_fingerprint="",
    )
    second = coordinator.process_candidate(replay_with_new_reference, product_evidence())

    assert second.deduplicated
    assert second.memory.memory_id == first.memory.memory_id
    assert len(sqlite_store.list(user_id="user-a", statuses=("active",))) == 1


def test_newer_safe_changed_value_supersedes_active_atomically(sqlite_store) -> None:
    coordinator = LongTermMemoryCoordinator(sqlite_store, None)
    first = coordinator.process_candidate(structured_candidate(role="server"), product_evidence())
    second = coordinator.process_candidate(
        structured_candidate(role="domain_controller"),
        product_evidence(role="domain_controller"),
    )

    assert first.memory.logical_memory_key == second.memory.logical_memory_key
    assert second.memory.status == "active"
    assert second.memory.supersedes_memory_id == first.memory.memory_id
    assert second.superseded_count == 1
    assert sqlite_store.get(user_id="user-a", memory_id=first.memory.memory_id).status == "superseded"
    assert len(sqlite_store.list(user_id="user-a", statuses=("active",))) == 1


def test_unresolved_conflict_keeps_candidate_and_blocks_old_active(sqlite_store) -> None:
    coordinator = LongTermMemoryCoordinator(sqlite_store, None)
    active_result = coordinator.process_candidate(structured_candidate(role="server"), product_evidence())
    conflict = coordinator.process_candidate(
        structured_candidate(role="domain_controller"),
        product_evidence(role="domain_controller", contradictions=("role_conflict",)),
    )

    assert conflict.decision.reason_code == "unresolved_material_conflict"
    assert conflict.memory.status == "candidate"
    assert conflict.conflict_count == 1
    old = sqlite_store.get(user_id="user-a", memory_id=active_result.memory.memory_id)
    assert old is not None and old.has_unresolved_conflict
    selection = LongTermMemoryRetriever(
        sqlite_store, None, candidate_k=5, top_k=5, min_score=0.0
    ).retrieve(query="role", user_id="user-a", entity_ids=("192.0.2.10",))
    assert selection.memories == ()


def test_promotion_replay_is_transactionally_idempotent(sqlite_store) -> None:
    memory = sqlite_store.put(memory=structured_candidate())
    decision = MemoryPromotionPolicy.evaluate_candidate_for_promotion(
        memory, product_evidence()
    )
    first = sqlite_store.apply_promotion(candidate=memory, decision=decision)
    second = sqlite_store.apply_promotion(candidate=memory, decision=decision)
    assert first.memory.memory_id == second.memory.memory_id
    assert second.deduplicated
    assert len(sqlite_store.list(user_id="user-a", statuses=("active",))) == 1


def test_concurrent_equivalent_promotion_cannot_create_duplicate_active(sqlite_store) -> None:
    memory = sqlite_store.put(memory=structured_candidate())
    decision = MemoryPromotionPolicy.evaluate_candidate_for_promotion(
        memory, product_evidence()
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(
            lambda _: sqlite_store.apply_promotion(candidate=memory, decision=decision),
            range(2),
        ))
    assert {item.memory.memory_id for item in results} == {memory.memory_id}
    assert len(sqlite_store.list(user_id="user-a", statuses=("active",))) == 1


def test_rejected_candidate_has_compact_audit_event(sqlite_store) -> None:
    memory = LongTermMemoryRecord.candidate(
        memory_type="validated_finding",
        user_id="user-a",
        entity_ids=("192.0.2.10",),
        statement="unstructured assistant interpretation",
        source_request_id="request-reject",
        source_conversation_id="conversation-a",
        provenance_category="investigation",
    )
    stored = sqlite_store.put(memory=memory)
    decision = MemoryPromotionPolicy.evaluate_candidate_for_promotion(
        stored, product_evidence()
    )
    result = sqlite_store.apply_promotion(candidate=stored, decision=decision)
    assert result.memory.status == "rejected"
    events = sqlite_store.list_audit_events(user_id="user-a", memory_id=memory.memory_id)
    assert any(event.action == "rejected" for event in events)
    assert all(event.evidence_refs == () for event in events)


def test_v6_to_v7_migration_preserves_legacy_ltm_rows(tmp_path: Path) -> None:
    path = tmp_path / "v6-memory.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_metadata VALUES ('local_schema_version', '6');
        CREATE TABLE local_users(
            user_id TEXT PRIMARY KEY, username TEXT, password_hash TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE local_long_term_memories(
            memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, memory_type TEXT NOT NULL,
            statement TEXT NOT NULL, epistemic_status TEXT NOT NULL, confidence REAL NOT NULL,
            source_request_id TEXT NOT NULL, source_conversation_id TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL, provenance_category TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_until TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
            index_status TEXT NOT NULL, supersedes_memory_id TEXT,
            idempotency_fingerprint TEXT
        );
        CREATE TABLE local_long_term_memory_entities(
            memory_id TEXT NOT NULL, entity_id TEXT NOT NULL,
            PRIMARY KEY(memory_id, entity_id)
        );
        """
    )
    legacy = structured_candidate()
    connection.execute(
        "INSERT INTO local_users VALUES (?,NULL,NULL,?)",
        (legacy.user_id, legacy.created_at),
    )
    connection.execute(
        "INSERT INTO local_long_term_memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            legacy.memory_id, legacy.user_id, legacy.memory_type, legacy.statement,
            legacy.epistemic_status, legacy.confidence, legacy.source_request_id,
            legacy.source_conversation_id, json.dumps(legacy.evidence_refs),
            legacy.provenance_category, legacy.valid_from, legacy.valid_until,
            legacy.created_at, legacy.updated_at, legacy.revision, legacy.status,
            legacy.index_status, legacy.supersedes_memory_id, legacy.idempotency_fingerprint,
        ),
    )
    connection.execute(
        "INSERT INTO local_long_term_memory_entities VALUES (?,?)",
        (legacy.memory_id, legacy.entity_ids[0]),
    )
    connection.commit()
    connection.close()

    database = LocalSQLiteDatabase(path)
    database.initialize()
    restored = SQLiteLongTermMemoryStore(database).get(
        user_id=legacy.user_id, memory_id=legacy.memory_id
    )
    assert restored is not None
    assert restored.logical_memory_key == legacy.logical_memory_key
    with database.connect() as migrated:
        assert migrated.execute(
            "SELECT value FROM schema_metadata WHERE key='local_schema_version'"
        ).fetchone()[0] == str(LOCAL_SCHEMA_VERSION)
        assert migrated.execute(
            "SELECT name FROM sqlite_master WHERE name='local_long_term_memory_audit'"
        ).fetchone() is not None


def test_active_ltm_survives_restart_crosses_conversations_but_not_users(
    tmp_path: Path,
) -> None:
    database = LocalSQLiteDatabase(tmp_path / "restart-ltm.sqlite3")
    database.initialize()
    first_store = SQLiteLongTermMemoryStore(database)
    promoted = LongTermMemoryCoordinator(
        first_store, None, active_validity_seconds=7 * 24 * 60 * 60
    ).process_candidate(
        structured_candidate(), replace(product_evidence(), retrieved_at=utc_now())
    ).memory

    reopened = LocalSQLiteDatabase(database.path)
    reopened.initialize()
    retriever = LongTermMemoryRetriever(
        SQLiteLongTermMemoryStore(reopened), None, candidate_k=5, top_k=5, min_score=0.0
    )
    same_owner_new_conversation = retriever.retrieve(
        query="validated role in a new conversation",
        user_id="user-a",
        entity_ids=("192.0.2.10",),
    )
    different_owner = retriever.retrieve(
        query="validated role",
        user_id="user-b",
        entity_ids=("192.0.2.10",),
    )
    assert [item.memory.memory_id for item in same_owner_new_conversation.memories] == [
        promoted.memory_id
    ]
    assert different_owner.memories == ()


def test_auto_promotion_at_active_quota_keeps_candidate_and_audits_reason(
    tmp_path: Path,
) -> None:
    database = LocalSQLiteDatabase(tmp_path / "active-quota.sqlite3")
    database.initialize()
    store = SQLiteLongTermMemoryStore(
        database,
        MemoryStoragePolicy(max_active_long_term_per_user=1),
    )
    coordinator = LongTermMemoryCoordinator(store, None)
    first = coordinator.process_candidate(structured_candidate(), product_evidence())
    second = coordinator.process_candidate(
        structured_candidate(entity="192.0.2.11"),
        product_evidence(entity="192.0.2.11"),
    )
    assert first.memory.status == "active"
    assert second.memory.status == "candidate"
    assert second.decision.reason_code == "active_memory_quota_full"
    assert any(
        event.reason_code == "active_memory_quota_full"
        for event in store.list_audit_events(
            user_id="user-a", memory_id=second.memory.memory_id
        )
    )


def test_expiration_is_audited_and_removes_operational_authority(sqlite_store) -> None:
    active_memory = LongTermMemoryCoordinator(sqlite_store, None).process_candidate(
        structured_candidate(), product_evidence()
    ).memory
    expired = sqlite_store.expire(
        user_id=active_memory.user_id,
        memory_id=active_memory.memory_id,
        expected_revision=active_memory.revision,
    )
    assert expired.status == "expired"
    assert not expired.authoritative
    assert sqlite_store.list(user_id="user-a", statuses=("active",)) == ()
    assert any(
        event.action == "expired"
        for event in sqlite_store.list_audit_events(
            user_id="user-a", memory_id=expired.memory_id
        )
    )


def test_expired_exact_replay_creates_a_fresh_candidate(sqlite_store) -> None:
    stale = replace(
        active(),
        valid_from="2025-01-01T00:00:00+00:00",
        valid_until="2025-01-02T00:00:00+00:00",
    )
    sqlite_store.put(memory=stale)

    replay = sqlite_store.put(memory=candidate())

    assert replay.memory_id != stale.memory_id
    assert replay.status == "candidate"
    assert sqlite_store.get(user_id="user-a", memory_id=stale.memory_id).status == "expired"
    assert any(
        event.reason_code == "validity_elapsed_on_replay"
        for event in sqlite_store.list_audit_events(
            user_id="user-a", memory_id=stale.memory_id
        )
    )


def test_atomic_index_and_canonical_survives_index_failure(sqlite_store) -> None:
    memory = candidate()
    sqlite_store.put(memory=memory)
    vectors = FakeVectorStore()
    index = MemorySemanticIndex(FakeEmbedder(), vectors)
    coordinator = LongTermMemoryCoordinator(sqlite_store, index)
    promoted = coordinator.promote(
        memory,
        epistemic_status="analyst_confirmed",
        confidence=0.9,
        provenance_category="analyst",
    )
    assert promoted.index_status == "synced"
    assert tuple(vectors.records) == (memory.memory_id,)
    payload = vectors.records[memory.memory_id].payload
    assert payload["user_id"] == "user-a"
    assert payload["memory_id"] == memory.memory_id
    assert "statement" not in payload

    second = candidate(entity="192.0.2.11", statement="A known Domain Controller role was validated.")
    sqlite_store.put(memory=second)
    vectors.fail_upsert = True
    failed = coordinator.promote(
        second,
        epistemic_status="source_validated",
        confidence=0.8,
        provenance_category="product",
    )
    assert failed.index_status == "failed"
    assert sqlite_store.get(user_id="user-a", memory_id=second.memory_id) is not None


def test_exact_semantic_dedup_user_scope_and_cross_conversation(sqlite_store) -> None:
    embedder = FakeEmbedder()
    vectors = FakeVectorStore()
    index = MemorySemanticIndex(embedder, vectors)
    records = (
        active(statement="This host is an approved scanner."),
        active(entity="192.0.2.20", statement="This host has a known Domain Controller role."),
        active(user="user-b", statement="Another tenant scanner is approved."),
    )
    for record in records:
        sqlite_store.put(memory=record)
        index.index(record)
    retriever = LongTermMemoryRetriever(
        sqlite_store, index, candidate_k=20, top_k=5, min_score=0.0, context_token_budget=500
    )
    result = retriever.retrieve(
        query="What do we know about this approved scanner?",
        user_id="user-a",
        entity_ids=("192.0.2.10",),
        request_id="req-search",
    )
    assert result.status == "ok"
    assert [item.memory.memory_id for item in result.memories] == [records[0].memory_id]
    assert result.memories[0].retrieval_reason == "exact_entity"
    assert vectors.last_filters == {"user_id": "user-a", "status": "active"}
    assert all(item.memory.user_id == "user-a" for item in result.memories)
    assert embedder.query_calls == 1


def test_exact_historical_retrieval_preserves_complementary_required_classes(sqlite_store) -> None:
    profile = active(
        statement="Previously validated identity is server.",
        evidence_refs=("evidence_class_asset_identity", "complete"),
    )
    detection = active(
        statement="Previously validated classification is server.",
        evidence_refs=("evidence_class_detection_classification", "complete"),
    )
    sqlite_store.put(memory=profile)
    sqlite_store.put(memory=detection)
    selection = LongTermMemoryRetriever(
        sqlite_store, None, candidate_k=5, top_k=2, min_score=0.0, context_token_budget=1000
    ).retrieve(
        query="What was the last validated identity and classification?",
        user_id="user-a",
        entity_ids=("192.0.2.10",),
        required_evidence_classes=("asset_identity", "detection_classification"),
    )

    assert {item.memory.memory_id for item in selection.memories} == {
        profile.memory_id,
        detection.memory_id,
    }


def test_reranker_is_bounded_and_failure_falls_back(sqlite_store) -> None:
    vectors = FakeVectorStore()
    index = MemorySemanticIndex(FakeEmbedder(), vectors)
    records = [
        active(entity=f"192.0.2.{number}", statement=f"Validated scanner behavior {number}.")
        for number in range(1, 7)
    ]
    for record in records:
        sqlite_store.put(memory=record)
        index.index(record)
    baseline = LongTermMemoryRetriever(
        sqlite_store, index, candidate_k=3, top_k=2, min_score=0.0
    ).retrieve(query="scanner", user_id="user-a")
    reranker = FakeReranker()
    retriever = LongTermMemoryRetriever(
        sqlite_store, index, candidate_k=3, top_k=2, min_score=0.0, reranker=reranker
    )
    result = retriever.retrieve(query="scanner", user_id="user-a")
    assert len(reranker.calls[0]) == 3
    assert len(result.memories) == 2
    assert [item.memory.memory_id for item in result.memories] != [
        item.memory.memory_id for item in baseline.memories
    ]
    assert all(item.memory.confidence == 0.95 for item in result.memories)

    fallback = LongTermMemoryRetriever(
        sqlite_store,
        index,
        candidate_k=3,
        top_k=2,
        min_score=0.0,
        reranker=FakeReranker(fail=True),
    ).retrieve(query="scanner", user_id="user-a")
    assert "memory_reranker_unavailable" in fallback.limitations
    disabled = LongTermMemoryRetriever(
        sqlite_store, index, candidate_k=3, top_k=2, min_score=0.0
    ).retrieve(query="scanner", user_id="user-a")
    assert disabled.reranked_count == 0


def test_lazy_cross_encoder_timeout_is_non_loading_and_sticky() -> None:
    import time

    class SlowModel:
        @staticmethod
        def predict(*args, **kwargs):
            time.sleep(0.2)
            return [1.0]

    reranker = LazyCrossEncoderReranker("not-loaded", timeout_seconds=0.01)
    assert reranker._model is None
    reranker._model = SlowModel()
    with pytest.raises(TimeoutError, match="memory_reranker_timeout"):
        reranker.rerank("query", ["document"])
    with pytest.raises(TimeoutError, match="previously_timed_out"):
        reranker.rerank("query", ["document"])


def test_memory_context_budget_dedup_and_grounding_metadata() -> None:
    memory = active(statement="This asset is an approved scanner.")
    retrieved = RetrievedLongTermMemory(memory, 1.0, "exact_entity", memory.freshness())
    settings = SimpleNamespace(
        conversation_summary_enabled=False,
        memory_relevant_turn_limit=0,
        memory_relevant_turn_token_budget=0,
        memory_episode_context_limit=0,
        memory_episode_context_token_budget=0,
        memory_context_token_budget=100,
        memory_context_long_term_token_budget=100,
    )
    package = MemoryStore(10).compose_memory_context(
        "session",
        settings,
        context_key=None,
        long_term_memories=(retrieved, retrieved),
    )
    assert len(package.long_term_memories) == 1
    text = "\n".join(item["content"] for item in package.model_messages())
    assert "epistemic=analyst_confirmed" in text
    assert "freshness=current" in text
    assert "provenance=analyst" in text
    assert "Qdrant" not in text and "SQLite" not in text and "CrossEncoder" not in text


def test_workflow_retrieves_after_resolution_without_changing_entity_state() -> None:
    events: list[str] = []
    resolution = EntityResolution(
        status="resolved",
        entities=[ResolvedEntity("ip", "192.0.2.10", "message")],
        primary_entity=ResolvedEntity("ip", "192.0.2.10", "message"),
        entity_mode="single",
        candidate_count=1,
        explicit_candidate_count=1,
        valid_entity_count=1,
    )

    class Resolver:
        def resolve(self, *args, **kwargs):
            events.append("resolve")
            return resolution

    class RetrieverService:
        settings = SimpleNamespace(chat_store_history=True, conversation_recent_raw_messages=2)
        routing_state_store = SimpleNamespace(get=lambda _session: SessionRoutingState())
        memory_store = SimpleNamespace(recent_for_routing=lambda _session, _limit: [])
        entity_resolver = Resolver()

        @staticmethod
        def retrieve_long_term_memory(**kwargs):
            events.append("retrieve")
            assert kwargs["entity_ids"] == ("192.0.2.10",)
            return LongTermMemorySelection(status="empty")

    identity = RequestIdentity.resolve(
        user_id="user-a",
        conversation_id="conv-a",
        session_id="session-a",
        request_id="request-a",
    )
    update = CopilotWorkflowNodes(RetrieverService()).resolve_entities(
        {
            "session_id": identity.session_id,
            "request_id": identity.request_id,
            "request_identity": identity,
            "message": "Investigate 192.0.2.10",
        }
    )
    assert events == ["resolve", "retrieve"]
    assert update["resolved_entities"] is resolution
    assert update["long_term_memory_selection"].status == "empty"
    assert update["next_edge"] == "route"


def test_offline_retrieval_quality_fixture_metrics(sqlite_store) -> None:
    vectors = FakeVectorStore()
    index = MemorySemanticIndex(FakeEmbedder(), vectors)
    fixtures = [
        ("approved scanner", active(statement="Approved scanner behavior is known.")),
        ("domain controller", active(entity="192.0.2.20", statement="Validated Domain Controller role.")),
        ("false positive correction", active(entity="192.0.2.30", statement="Analyst corrected this false positive.")),
    ]
    for _, memory in fixtures:
        sqlite_store.put(memory=memory)
        index.index(memory)
    retriever = LongTermMemoryRetriever(
        sqlite_store, index, candidate_k=3, top_k=3, min_score=0.0, context_token_budget=1000
    )
    reciprocal_ranks = []
    recalls = []
    ndcgs = []
    for query, expected in fixtures:
        result = retriever.retrieve(query=query, user_id="user-a")
        ids = [item.memory.memory_id for item in result.memories]
        rank = ids.index(expected.memory_id) + 1 if expected.memory_id in ids else 0
        recalls.append(float(rank > 0))
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        ndcgs.append(1.0 / math.log2(rank + 1) if rank else 0.0)
    assert sum(recalls) / len(recalls) == 1.0
    assert sum(reciprocal_ranks) / len(reciprocal_ranks) >= 0.5
    assert sum(ndcgs) / len(ndcgs) >= 0.6
