"""Deterministic T16-T20 fault-injection checks; no network or live Product writes."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config.settings import get_settings
from src.core.agent.contracts import RequestConstraints
from src.core.agent.task_mapping import derive_turn_policy
from src.core.context.models import EntityResolution, ResolvedEntity
from src.core.context.router import DeterministicFallbackRouter
from src.core.copilot.service import CopilotService
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent
from src.core.memory.episodes import WorkingFact
from src.core.memory.long_term import LongTermMemoryRecord, MemoryPromotionPolicy, RetrievedLongTermMemory
from src.core.memory.persistence import LocalPersistenceError
from src.core.memory.retrieval import LongTermMemoryRetriever, LongTermMemorySelection, MemorySemanticIndex
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.sqlite import LocalSQLiteDatabase
from src.core.memory.sqlite_long_term import SQLiteLongTermMemoryStore
from src.core.memory.store import MemoryStore
from src.core.identity import RequestIdentity
from src.core.rag.embeddings import EmbeddingHealth
from src.core.rag.vector_store import VectorCollectionInfo, VectorRecord, VectorSearchHit, VectorStoreHealth
from src.tests.test_chat_streaming import FakeStreamingLLM, provider_done, service_settings


def _active(*, entity: str, statement: str, conversation: str = "conv-a") -> LongTermMemoryRecord:
    candidate = LongTermMemoryRecord.candidate(
        memory_type="validated_finding",
        user_id="user-a",
        entity_ids=(entity,),
        statement=statement,
        source_request_id="fault-request",
        source_conversation_id=conversation,
        evidence_refs=("evidence_class_asset_role", "complete"),
        confidence=0.5,
    )
    return MemoryPromotionPolicy.promote(
        candidate,
        epistemic_status="analyst_confirmed",
        confidence=0.95,
        provenance_category="analyst",
    )


class _BrokenInventoryStore:
    def list(self, **_kwargs):
        raise LocalPersistenceError("injected inventory failure")


def test_t16_exact_product_ltm_survives_local_persistence_error() -> None:
    memory = _active(entity="192.0.2.10", statement="The asset is an internal Linux server.")
    retrieved = RetrievedLongTermMemory(memory, 1.0, "exact_entity", memory.freshness())

    class Retriever:
        def retrieve(self, **_kwargs):
            return LongTermMemorySelection(status="ok", memories=(retrieved,), selected_count=1)

    service = CopilotService(
        service_settings(),
        FakeStreamingLLM([]),
        MemoryStore(0),
        long_term_memory_store=_BrokenInventoryStore(),  # type: ignore[arg-type]
        long_term_memory_retriever=Retriever(),  # type: ignore[arg-type]
    )
    identity = RequestIdentity.resolve(user_id="user-a", conversation_id="conv-a", session_id="s16", request_id="r16")
    selection = service.retrieve_long_term_memory(
        identity=identity,
        message="What is this server?",
        entity_ids=("192.0.2.10",),
    )
    assert selection.memories == (retrieved,)
    assert "long_term_memory_inventory_unavailable" in selection.limitations


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SQLiteLongTermMemoryStore:
    database = LocalSQLiteDatabase(tmp_path / "fault-memory.sqlite3")
    database.initialize()
    return SQLiteLongTermMemoryStore(database)


class _Embedder:
    model_name = "fault-fixture"
    dimension = 2

    def health(self):
        return EmbeddingHealth("ok", self.model_name, self.dimension, False)

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        value = 1.0 if "linux" in text.lower() else 0.1
        norm = math.sqrt(value * value + 1.0)
        return [value / norm, 1.0 / norm]


class _VectorStore:
    def __init__(self) -> None:
        self.records: dict[str, VectorRecord] = {}

    def health(self):
        return VectorStoreHealth("ok", "fault", "memory", True, True, 2, "cosine")

    def ensure_collection(self):
        return VectorCollectionInfo("fault", 2, "cosine", len(self.records))

    collection_info = ensure_collection

    def upsert(self, records):
        self.records.update({record.id: record for record in records})
        return len(records)

    def delete(self, ids):
        for identifier in ids:
            self.records.pop(identifier, None)
        return len(ids)

    def search(self, query_vector, *, top_k, filters=None):
        hits = []
        for record in self.records.values():
            if filters and any(record.payload.get(key) != value for key, value in filters.items()):
                continue
            score = sum(left * right for left, right in zip(query_vector, record.vector))
            hits.append(VectorSearchHit(record.id, score, record.payload))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]


def test_t17_entity_a_rejects_entity_b_semantic_memory(sqlite_store: SQLiteLongTermMemoryStore) -> None:
    asset_a = _active(entity="192.0.2.10", statement="Internal Linux server with high risk.")
    asset_b = _active(entity="192.0.2.11", statement="External Windows workstation.")
    sqlite_store.put(memory=asset_a)
    sqlite_store.put(memory=asset_b)
    index = MemorySemanticIndex(_Embedder(), _VectorStore())
    index.index(asset_a)
    index.index(asset_b)
    result = LongTermMemoryRetriever(sqlite_store, index, min_score=0.0).retrieve(
        query="Linux server", user_id="user-a", entity_ids=("192.0.2.10",), conversation_id="conv-a"
    )
    assert result.memories
    assert {item.memory.entity_ids for item in result.memories} == {("192.0.2.10",)}


def _keep_inputs() -> tuple[EntityResolution, SessionRoutingState, RequestConstraints]:
    entity = ResolvedEntity("ip", "192.0.2.10", "conversation")
    resolution = EntityResolution(status="resolved", entities=[entity], primary_entity=entity, entity_mode="single", valid_entity_count=1)
    state = SessionRoutingState(
        active_ip="192.0.2.10",
        active_entities=("192.0.2.10",),
        previous_intent="asset_investigation",
        previous_scope="node_summary",
        last_providers=("asset_profile", "detection"),
    )
    return resolution, state, RequestConstraints(require_current=True, reason_codes=("current",))


@pytest.mark.parametrize("fault", ["transport_timeout", "invalid_router_output"])
def test_t18_t19_router_faults_preserve_keep_state(fault: str) -> None:
    resolution, state, constraints = _keep_inputs()
    policy = derive_turn_policy("verify current state", constraints, resolution, state)
    assert policy.episode_transition == "keep"
    decision = DeterministicFallbackRouter().route(
        "verify current state",
        resolution,
        state,
        fallback_reason=fault,
        request_id=f"request-{fault}",
        constraints=constraints,
        turn_policy=policy,
    )
    assert tuple(item.value for item in decision.target_entities) == ("192.0.2.10",)
    assert decision.fallback_reason == fault


def test_t20_reasoning_only_length_uses_one_bounded_trace_preserving_fallback() -> None:
    llm = FakeStreamingLLM(
        [
            LLMStreamEvent("reasoning_delta", text="fixture reasoning"),
            LLMStreamEvent("done", data={"finish_reason": "length", "usage": {"completion_tokens": 2048}, "stream_terminated": True}),
        ],
        fallback_text="bounded recovery answer",
    )
    service = CopilotService(service_settings(llm_expose_reasoning=False), llm, MemoryStore(0))  # type: ignore[arg-type]
    metrics = {
        "streaming_requested": True, "streaming_used": False,
        "first_reasoning_chunk_latency_ms": None, "first_answer_chunk_latency_ms": None,
        "stream_chunk_count": 0, "reasoning_chunk_count": 0, "answer_chunk_count": 0,
        "stream_completed": False, "stream_error_type": "",
    }
    emitted: list[LLMStreamEvent] = []
    result = service._stream_final_model(
        [{"role": "user", "content": "fixture"}], request_id="t20-request", trace_id="t20-trace",
        max_tokens=4096, temperature=0.2, top_p=0.9, timeout_seconds=10,
        sink=emitted.append, metrics=metrics,
    )
    assert result.text == "bounded recovery answer"
    assert len(llm.chat_calls) == 1
    assert llm.chat_calls[0]["max_tokens"] == 2048
    assert llm.chat_calls[0]["trace_id"] == "t20-trace"
    assert metrics["fallback_reason"] == "provider_stream_reasoning_exhausted"
    assert [item.text for item in emitted] == ["bounded recovery answer"]


def test_t20_empty_recovery_is_safe_without_retry() -> None:
    class Empty(FakeStreamingLLM):
        def chat(self, messages, **kwargs):
            self.chat_calls.append({"messages": messages, **kwargs})
            return LLMProviderResult("", "fake", "fixture-chat-model", deployment="glm")

    llm = Empty([])
    service = CopilotService(service_settings(), llm, MemoryStore(0))  # type: ignore[arg-type]
    metrics = {
        "streaming_requested": True, "streaming_used": False,
        "first_reasoning_chunk_latency_ms": None, "first_answer_chunk_latency_ms": None,
        "stream_chunk_count": 0, "reasoning_chunk_count": 1, "answer_chunk_count": 0,
        "stream_completed": False, "stream_error_type": "provider_stream_reasoning_exhausted",
    }
    with pytest.raises(LLMError, match="recovery response was empty"):
        service._synthesis_non_stream_fallback(
            [{"role": "user", "content": "fixture"}], request_id="t20-empty", trace_id="t20-trace",
            max_tokens=4096, temperature=0.2, top_p=0.9, timeout_seconds=10,
            sink=lambda _event: None, metrics=metrics, reason="provider_stream_reasoning_exhausted",
        )
    assert len(llm.chat_calls) == 1
