"""Hybrid exact/vector long-term memory retrieval and index coordination."""

from __future__ import annotations

import importlib.util
import logging
from queue import Queue
from threading import Thread
import time
from dataclasses import dataclass, replace
from typing import Protocol, Sequence

from src.core.memory.long_term import (
    AUTHORITATIVE_EPISTEMIC,
    LongTermMemoryRecord,
    MemoryPromotionPolicy,
    RetrievedLongTermMemory,
    retrieval_document,
    utc_now,
)
from src.core.memory.ports import LongTermMemoryStore
from src.core.rag.embeddings import Embedder
from src.core.rag.vector_store import VectorRecord, VectorSearchHit, VectorStore


logger = logging.getLogger(__name__)


class Reranker(Protocol):
    def rerank(
        self,
        query: str,
        documents: Sequence[str],
    ) -> list[float]: ...


class LazyCrossEncoderReranker:
    """Optional local-only CrossEncoder loaded on its first bounded rerank."""

    def __init__(
        self,
        model_name: str,
        *,
        batch_size: int = 16,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.model_name = str(model_name).strip()
        self.batch_size = max(1, min(64, int(batch_size)))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._model = None
        self._timed_out = False

    @property
    def configured(self) -> bool:
        return bool(self.model_name and importlib.util.find_spec("sentence_transformers"))

    def _ensure_loaded(self):
        if self._model is not None:
            return self._model
        if not self.configured:
            raise RuntimeError("memory_reranker_not_configured")
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(self.model_name, local_files_only=True)
        return self._model

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        if self._timed_out:
            raise TimeoutError("memory_reranker_previously_timed_out")
        model = self._ensure_loaded()
        result: Queue[tuple[str, object]] = Queue(maxsize=1)

        def predict() -> None:
            try:
                scores = model.predict(
                    [(query, document) for document in documents],
                    batch_size=self.batch_size,
                    show_progress_bar=False,
                )
                result.put(("ok", scores))
            except Exception as exc:
                result.put(("error", exc))

        worker = Thread(target=predict, name="memory-reranker", daemon=True)
        worker.start()
        worker.join(self.timeout_seconds)
        if worker.is_alive():
            self._timed_out = True
            raise TimeoutError("memory_reranker_timeout")
        status, value = result.get_nowait()
        if status == "error":
            raise value  # type: ignore[misc]
        return [float(score) for score in value]  # type: ignore[union-attr]


class MemorySemanticIndex:
    """Atomic memory projection over the existing Embedder/VectorStore ports."""

    def __init__(self, embedder: Embedder, vector_store: VectorStore) -> None:
        self.embedder = embedder
        self.vector_store = vector_store

    def index(self, memory: LongTermMemoryRecord) -> None:
        document = retrieval_document(memory)
        vector = self.embedder.embed_documents([document.text])[0]
        self.vector_store.ensure_collection()
        self.vector_store.upsert(
            [
                VectorRecord(
                    id=memory.memory_id,
                    vector=vector,
                    payload={
                        "memory_id": memory.memory_id,
                        "memory_type": memory.memory_type,
                        "user_id": memory.user_id,
                        "entity_ids": list(memory.entity_ids),
                        "epistemic_status": memory.epistemic_status,
                        "status": memory.status,
                        "confidence": memory.confidence,
                        "valid_from": memory.valid_from,
                        "valid_until": memory.valid_until,
                        "source_request_id": memory.source_request_id,
                        "source_conversation_id": memory.source_conversation_id,
                        "revision": memory.revision,
                        "content_hash": document.content_hash,
                        "retrieval_text": document.text,
                    },
                )
            ]
        )

    def delete(self, memory_id: str) -> None:
        self.vector_store.delete([memory_id])

    def search(self, query: str, *, user_id: str, limit: int) -> list[VectorSearchHit]:
        vector = self.embedder.embed_query(query)
        return self.vector_store.search(
            vector,
            top_k=max(1, int(limit)),
            filters={"user_id": user_id, "status": "active"},
        )


@dataclass(frozen=True)
class LongTermMemorySelection:
    status: str
    memories: tuple[RetrievedLongTermMemory, ...] = ()
    exact_candidate_count: int = 0
    semantic_candidate_count: int = 0
    reranked_count: int = 0
    estimated_tokens: int = 0
    limitations: tuple[str, ...] = ()


class LongTermMemoryRetriever:
    """Owner-scoped exact+dense retrieval with policy filtering and bounded reranking."""

    def __init__(
        self,
        store: LongTermMemoryStore,
        semantic_index: MemorySemanticIndex | None,
        *,
        candidate_k: int = 20,
        top_k: int = 5,
        min_score: float = 0.35,
        context_token_budget: int = 500,
        reranker: Reranker | None = None,
    ) -> None:
        self.store = store
        self.semantic_index = semantic_index
        self.candidate_k = max(1, min(100, int(candidate_k)))
        self.top_k = max(1, min(20, int(top_k)))
        self.min_score = min(1.0, max(0.0, float(min_score)))
        self.context_token_budget = max(0, int(context_token_budget))
        self.reranker = reranker

    @staticmethod
    def _eligible(memory: LongTermMemoryRecord) -> bool:
        return (
            memory.status == "active"
            and memory.epistemic_status in AUTHORITATIVE_EPISTEMIC | {"historical"}
            and memory.freshness() not in {"expired", "inactive"}
        )

    def retrieve(
        self,
        *,
        query: str,
        user_id: str,
        entity_ids: tuple[str, ...] = (),
        request_id: str = "",
    ) -> LongTermMemorySelection:
        started = time.perf_counter()
        exact = self.store.list(
            user_id=user_id,
            entity_ids=entity_ids,
            statuses=("active",),
            limit=self.candidate_k,
        ) if entity_ids else ()
        logger.info(
            "event=memory_exact_search_completed request_id=%s status=ok candidate_count=%s",
            request_id,
            len(exact),
        )
        candidates: dict[str, RetrievedLongTermMemory] = {}
        for memory in exact:
            if self._eligible(memory):
                candidates[memory.memory_id] = RetrievedLongTermMemory(
                    memory=memory,
                    relevance_score=1.0,
                    retrieval_reason="exact_entity",
                    freshness=memory.freshness(),
                )

        semantic_hits: list[VectorSearchHit] = []
        limitations: list[str] = []
        if self.semantic_index is not None:
            try:
                semantic_hits = self.semantic_index.search(
                    query,
                    user_id=user_id,
                    limit=self.candidate_k,
                )
                for hit in semantic_hits:
                    memory_id = str(hit.payload.get("memory_id") or hit.id)
                    if hit.score < self.min_score or memory_id in candidates:
                        continue
                    memory = self.store.get(user_id=user_id, memory_id=memory_id)
                    if memory is None or not self._eligible(memory):
                        continue
                    if entity_ids and memory.entity_ids and not set(entity_ids).intersection(memory.entity_ids):
                        continue
                    candidates[memory.memory_id] = RetrievedLongTermMemory(
                        memory=memory,
                        relevance_score=float(hit.score),
                        retrieval_reason="semantic",
                        freshness=memory.freshness(),
                    )
                logger.info(
                    "event=memory_semantic_search_completed request_id=%s status=ok candidate_count=%s",
                    request_id,
                    len(semantic_hits),
                )
            except Exception as exc:
                limitations.append("semantic_memory_unavailable")
                logger.warning(
                    "event=memory_semantic_search_completed request_id=%s status=unavailable error_type=%s",
                    request_id,
                    type(exc).__name__,
                )

        ranked = sorted(
            candidates.values(),
            key=lambda item: (
                item.retrieval_reason == "exact_entity",
                item.relevance_score,
                item.memory.confidence,
                item.memory.updated_at,
            ),
            reverse=True,
        )[: self.candidate_k]
        reranked_count = 0
        if self.reranker is not None and ranked:
            try:
                scores = self.reranker.rerank(
                    query,
                    [retrieval_document(item.memory).text for item in ranked],
                )
                if len(scores) != len(ranked):
                    raise ValueError("memory_reranker_score_count_mismatch")
                ranked = [
                    item
                    for _, item in sorted(
                        zip(scores, ranked),
                        key=lambda pair: pair[0],
                        reverse=True,
                    )
                ]
                reranked_count = len(ranked)
                logger.info(
                    "event=memory_rerank_completed request_id=%s status=ok candidate_count=%s",
                    request_id,
                    reranked_count,
                )
            except Exception as exc:
                limitations.append("memory_reranker_unavailable")
                logger.warning(
                    "event=memory_rerank_completed request_id=%s status=fallback error_type=%s",
                    request_id,
                    type(exc).__name__,
                )

        selected: list[RetrievedLongTermMemory] = []
        used_tokens = 0
        for item in ranked:
            if len(selected) >= self.top_k:
                break
            if used_tokens + item.estimated_tokens > self.context_token_budget:
                limitations.append("long_term_memory_context_truncated")
                continue
            selected.append(item)
            used_tokens += item.estimated_tokens

        logger.info(
            "event=memory_retrieval_completed request_id=%s status=%s exact_candidate_count=%s semantic_candidate_count=%s reranked_count=%s selected_count=%s estimated_tokens=%s latency_ms=%s memory_types=%s",
            request_id,
            "ok" if selected else "empty",
            len(exact),
            len(semantic_hits),
            reranked_count,
            len(selected),
            used_tokens,
            int((time.perf_counter() - started) * 1000),
            ",".join(sorted({item.memory.memory_type for item in selected})) or "none",
        )
        return LongTermMemorySelection(
            status="ok" if selected else "empty",
            memories=tuple(selected),
            exact_candidate_count=len(exact),
            semantic_candidate_count=len(semantic_hits),
            reranked_count=reranked_count,
            estimated_tokens=used_tokens,
            limitations=tuple(dict.fromkeys(limitations)),
        )


class LongTermMemoryCoordinator:
    """Canonical-first lifecycle operations with best-effort index consistency."""

    def __init__(
        self,
        store: LongTermMemoryStore,
        semantic_index: MemorySemanticIndex | None,
    ) -> None:
        self.store = store
        self.semantic_index = semantic_index

    def create_candidate(self, memory: LongTermMemoryRecord) -> LongTermMemoryRecord:
        if memory.status != "candidate" or memory.epistemic_status != "candidate":
            raise ValueError("Only explicit candidate records can use create_candidate")
        stored = self.store.put(memory=memory)
        logger.info(
            "event=long_term_memory_candidate_created memory_type=%s status=candidate",
            memory.memory_type,
        )
        return stored

    def promote(
        self,
        candidate: LongTermMemoryRecord,
        *,
        epistemic_status: str,
        confidence: float,
        provenance_category: str,
    ) -> LongTermMemoryRecord:
        promoted = MemoryPromotionPolicy.promote(
            candidate,
            epistemic_status=epistemic_status,
            confidence=confidence,
            provenance_category=provenance_category,
        )
        canonical = self.store.update(memory=promoted, expected_revision=candidate.revision)
        result = self._index_canonical(canonical)
        logger.info(
            "event=long_term_memory_promoted memory_type=%s epistemic_status=%s index_status=%s",
            result.memory_type,
            result.epistemic_status,
            result.index_status,
        )
        return result

    def _index_canonical(self, memory: LongTermMemoryRecord) -> LongTermMemoryRecord:
        if self.semantic_index is None:
            return memory
        try:
            if memory.status == "active":
                self.semantic_index.index(memory)
            else:
                self.semantic_index.delete(memory.memory_id)
            index_status = "synced"
            event = "memory_index_write_completed"
        except Exception as exc:
            index_status = "failed"
            event = "memory_index_write_failed"
            logger.warning(
                "event=%s memory_type=%s error_type=%s",
                event,
                memory.memory_type,
                type(exc).__name__,
            )
        updated = replace(
            memory,
            index_status=index_status,
            revision=memory.revision + 1,
            updated_at=utc_now(),
        )
        updated = self.store.update(memory=updated, expected_revision=memory.revision)
        if index_status == "synced":
            logger.info("event=%s memory_type=%s status=synced", event, memory.memory_type)
        return updated

    def invalidate(
        self,
        *,
        user_id: str,
        memory_id: str,
        expected_revision: int,
    ) -> LongTermMemoryRecord:
        memory = self.store.invalidate(
            user_id=user_id,
            memory_id=memory_id,
            expected_revision=expected_revision,
        )
        result = self._index_canonical(memory)
        logger.info("event=long_term_memory_invalidated status=%s", result.status)
        return result

    def supersede(
        self,
        *,
        user_id: str,
        memory_id: str,
        replacement: LongTermMemoryRecord,
        expected_revision: int,
    ) -> tuple[LongTermMemoryRecord, LongTermMemoryRecord]:
        old, new = self.store.supersede(
            user_id=user_id,
            memory_id=memory_id,
            replacement=replacement,
            expected_revision=expected_revision,
        )
        old = self._index_canonical(old)
        if new.status == "active":
            new = self._index_canonical(new)
        return old, new

    def delete(self, *, user_id: str, memory_id: str) -> bool:
        deleted = self.store.delete(user_id=user_id, memory_id=memory_id)
        if deleted and self.semantic_index is not None:
            try:
                self.semantic_index.delete(memory_id)
            except Exception as exc:
                logger.warning(
                    "event=memory_index_write_failed operation=delete error_type=%s",
                    type(exc).__name__,
                )
        return deleted

    def reconcile(self, *, user_id: str, limit: int = 500) -> dict[str, int]:
        records = self.store.list(
            user_id=user_id,
            statuses=("active", "invalidated", "superseded"),
            limit=limit,
        )
        synced = failed = 0
        for memory in records:
            result = self._index_canonical(memory)
            if result.index_status == "synced":
                synced += 1
            else:
                failed += 1
        logger.info(
            "event=memory_index_reconciliation_completed candidate_count=%s synced_count=%s failed_count=%s",
            len(records),
            synced,
            failed,
        )
        return {"candidate_count": len(records), "synced_count": synced, "failed_count": failed}
