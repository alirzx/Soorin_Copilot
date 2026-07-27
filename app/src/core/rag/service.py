"""Semantic knowledge.search capability orchestration."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.settings import Settings
from src.core.rag.citations import citations_from_chunks
from src.core.rag.embeddings import Embedder, HuggingFaceTextEmbedder
from src.core.rag.models import KnowledgeChunk, KnowledgeSearchResult
from src.core.rag.qdrant_store import QdrantVectorStore
from src.core.rag.safety import prompt_injection_flags
from src.core.rag.vector_store import VectorSearchHit, VectorStore


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeSearchService:
    capability_name = "knowledge.search"

    def __init__(
        self,
        settings: Settings,
        *,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.settings = settings
        logger.info(
            "event=rag_collection_selected enabled=%s backend=%s mode=%s collection=%s embedding_model=%s dimension=%s",
            settings.rag_enabled,
            settings.rag_backend,
            settings.rag_qdrant_mode,
            settings.rag_collection,
            settings.rag_embedding_model,
            settings.rag_embedding_dimension,
        )
        self.embedder = embedder or HuggingFaceTextEmbedder(
            settings.rag_embedding_model,
            settings.rag_embedding_dimension,
            local_files_only=settings.rag_embedding_local_files_only,
            cache_dir=settings.rag_embedding_cache_dir,
            revision=settings.rag_embedding_revision,
        )
        self.vector_store = vector_store
        if self.vector_store is None and settings.rag_enabled and settings.rag_backend == "qdrant":
            settings.validate_rag_embedding_configuration()
            settings.validate_rag_qdrant_configuration()
            self.vector_store = QdrantVectorStore(
                mode=settings.rag_qdrant_mode,
                path=settings.rag_qdrant_path,
                url=settings.rag_qdrant_url,
                collection=settings.rag_collection,
                dimension=settings.rag_embedding_dimension,
                distance=settings.rag_distance,
                api_key=settings.rag_qdrant_api_key,
                timeout_seconds=settings.rag_qdrant_timeout_seconds,
                batch_size=settings.rag_upsert_batch_size,
                embedding_model=settings.rag_embedding_model,
            )

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> KnowledgeSearchResult:
        started_at = _now()
        normalized_query = query.strip()
        if not normalized_query:
            return KnowledgeSearchResult(
                status="invalid",
                query="",
                backend=self.settings.rag_backend,
                retrieved_at=started_at,
                freshness="unknown",
                limitations=("Knowledge search requires a non-empty query.",),
                error_classification="invalid_query",
            )
        if not self.settings.rag_enabled:
            return KnowledgeSearchResult(
                status="not_configured",
                query=normalized_query,
                backend=self.settings.rag_backend,
                retrieved_at=started_at,
                freshness="unknown",
                limitations=("SOC knowledge retrieval is disabled.",),
                error_classification="rag_disabled",
            )
        if self.settings.rag_backend != "qdrant":
            return KnowledgeSearchResult(
                status="invalid",
                query=normalized_query,
                backend=self.settings.rag_backend,
                retrieved_at=started_at,
                freshness="unknown",
                limitations=("Configured knowledge backend is unsupported.",),
                error_classification="unsupported_backend",
            )
        if self.vector_store is None:
            return KnowledgeSearchResult(
                status="not_configured",
                query=normalized_query,
                backend=self.settings.rag_backend,
                retrieved_at=started_at,
                freshness="unknown",
                limitations=("SOC knowledge vector store is not configured.",),
                error_classification="vector_store_missing",
            )

        health = self.vector_store.health()
        if not health.available:
            return KnowledgeSearchResult(
                status="invalid" if health.status == "invalid" else "unavailable",
                query=normalized_query,
                backend=health.backend,
                retrieved_at=started_at,
                freshness="unknown",
                limitations=(
                    "Knowledge vector store is unavailable"
                    + (f": {health.error_reason}" if health.error_reason else "."),
                ),
                error_classification=health.error_classification or health.status,
            )
        embedder_health = self.embedder.health()
        if embedder_health.status != "ok":
            return KnowledgeSearchResult(
                status="unavailable",
                query=normalized_query,
                backend=health.backend,
                retrieved_at=started_at,
                freshness="unknown",
                limitations=("Knowledge embedding model is unavailable.",),
                error_classification=embedder_health.error_classification,
            )

        requested_k = max(1, int(top_k or self.settings.rag_top_k))
        try:
            query_vector = self.embedder.embed_query(normalized_query)
            hits = self.vector_store.search(
                query_vector,
                top_k=requested_k,
                filters=filters,
            )
        except Exception as exc:
            error_code = getattr(exc, "code", type(exc).__name__)
            logger.warning(
                "event=knowledge_search_failed request_id=%s backend=%s mode=%s "
                "collection=%s embedding_model=%s storage_path_kind=%s "
                "error_type=%s error_reason=%s",
                request_id,
                self.settings.rag_backend,
                self.settings.rag_qdrant_mode,
                self.settings.rag_collection,
                self.settings.rag_embedding_model,
                (
                    "remote_url"
                    if self.settings.rag_qdrant_mode == "server"
                    else "absolute_local"
                    if self.settings.rag_qdrant_path
                    and Path(self.settings.rag_qdrant_path).expanduser().is_absolute()
                    else "relative_local"
                    if self.settings.rag_qdrant_path
                    else "not_configured"
                ),
                error_code,
                QdrantVectorStore.safe_error_reason(exc),
            )
            return KnowledgeSearchResult(
                status="unavailable",
                query=normalized_query,
                backend=self.settings.rag_backend,
                retrieved_at=_now(),
                freshness="unknown",
                limitations=("Knowledge retrieval failed.",),
                error_classification=error_code,
            )

        accepted: list[KnowledgeChunk] = []
        blocked = 0
        below_threshold = 0
        for hit in hits:
            if hit.score < self.settings.rag_score_threshold:
                below_threshold += 1
                continue
            chunk = self._chunk_from_hit(hit)
            if prompt_injection_flags(chunk.text):
                blocked += 1
                continue
            accepted.append(chunk)
        limitations: list[str] = []
        if blocked:
            limitations.append(f"Excluded {blocked} unsafe retrieved chunk(s).")
        if below_threshold:
            limitations.append(f"Excluded {below_threshold} chunk(s) below the score threshold.")
        status = "ok" if accepted else "empty"
        if accepted and blocked:
            status = "partial"
        freshness_values = {chunk.indexed_at for chunk in accepted if chunk.indexed_at}
        freshness = "indexed" if freshness_values else "unknown"
        logger.info(
            "event=knowledge_search_complete request_id=%s backend=%s status=%s candidates=%s included=%s unsafe_excluded=%s threshold_excluded=%s",
            request_id,
            self.settings.rag_backend,
            status,
            len(hits),
            len(accepted),
            blocked,
            below_threshold,
        )
        return KnowledgeSearchResult(
            status=status,  # type: ignore[arg-type]
            query=normalized_query,
            backend=self.settings.rag_backend,
            retrieved_at=_now(),
            freshness=freshness,
            chunks=tuple(accepted),
            citations=citations_from_chunks(accepted),
            limitations=tuple(limitations),
            total_candidates=len(hits),
            included_count=len(accepted),
            truncated=len(hits) >= requested_k,
        )

    def close(self) -> None:
        close = getattr(self.vector_store, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _chunk_from_hit(hit: VectorSearchHit) -> KnowledgeChunk:
        payload = hit.payload
        return KnowledgeChunk(
            chunk_id=str(payload.get("chunk_id") or hit.id),
            document_id=str(payload.get("document_id") or ""),
            text=str(payload.get("text") or ""),
            score=hit.score,
            source_path=str(payload.get("source_path") or ""),
            relative_path=str(payload.get("relative_path") or ""),
            section=str(payload.get("section") or ""),
            category=str(payload.get("category") or ""),
            title=str(payload.get("title") or ""),
            content_hash=str(payload.get("content_hash") or ""),
            indexed_at=str(payload.get("indexed_at") or ""),
            source_version=str(payload.get("source_version") or ""),
            metadata={
                key: value
                for key, value in payload.items()
                if key not in {"text", "source_path"}
            },
        )
