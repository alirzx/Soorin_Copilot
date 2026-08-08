"""Offline Gate 6/7 tests for typed canonical and semantic memory."""

from __future__ import annotations

import math
import sqlite3
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
)
from src.core.memory.persistence import (
    LOCAL_SCHEMA_VERSION,
    LocalPersistenceConflictError,
    LocalPersistenceOwnershipError,
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
) -> LongTermMemoryRecord:
    return LongTermMemoryRecord.candidate(
        memory_type=memory_type,
        user_id=user,
        entity_ids=(entity,),
        statement=statement,
        source_request_id="req-1",
        source_conversation_id="conv-1",
        evidence_refs=("finding-7",),
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
