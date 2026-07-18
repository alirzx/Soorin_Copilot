"""Typed contracts at the Copilot knowledge-retrieval boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


KnowledgeStatus = Literal["ok", "empty", "not_configured", "unavailable", "invalid", "partial"]


@dataclass(frozen=True)
class KnowledgeCitation:
    chunk_id: str
    document_id: str
    title: str
    relative_path: str
    section: str
    score: float


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    text: str
    score: float
    source_path: str = ""
    relative_path: str = ""
    section: str = ""
    category: str = ""
    title: str = ""
    content_hash: str = ""
    indexed_at: str = ""
    source_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def citation(self) -> KnowledgeCitation:
        return KnowledgeCitation(
            chunk_id=self.chunk_id,
            document_id=self.document_id,
            title=self.title,
            relative_path=self.relative_path,
            section=self.section,
            score=self.score,
        )


@dataclass(frozen=True)
class KnowledgeSearchResult:
    status: KnowledgeStatus
    query: str
    backend: str
    retrieved_at: str
    freshness: str
    chunks: tuple[KnowledgeChunk, ...] = ()
    citations: tuple[KnowledgeCitation, ...] = ()
    limitations: tuple[str, ...] = ()
    total_candidates: int | None = None
    included_count: int = 0
    truncated: bool = False
    error_classification: str | None = None

    @property
    def documents(self) -> tuple[KnowledgeChunk, ...]:
        return self.chunks

    def to_context_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "backend": self.backend,
            "retrieved_at": self.retrieved_at,
            "freshness": self.freshness,
            "chunks": [asdict(item) for item in self.chunks],
            "citations": [asdict(item) for item in self.citations],
            "limitations": list(self.limitations),
            "total_candidates": self.total_candidates,
            "included_count": self.included_count,
            "truncated": self.truncated,
            "error_classification": self.error_classification,
        }
