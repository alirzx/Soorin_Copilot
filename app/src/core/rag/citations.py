"""Citation helpers that keep stable source and chunk provenance."""

from __future__ import annotations

from src.core.rag.models import KnowledgeChunk, KnowledgeCitation


def citations_from_chunks(chunks: list[KnowledgeChunk]) -> tuple[KnowledgeCitation, ...]:
    return tuple(chunk.citation() for chunk in chunks)
