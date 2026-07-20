"""Explicit manual corpus validation and Qdrant indexing entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from src.config.settings import Settings, get_settings
from src.core.rag.chunker import SourceChunk, chunk_document
from src.core.rag.embeddings import Embedder, HuggingFaceTextEmbedder
from src.core.rag.qdrant_store import QdrantVectorStore
from src.core.rag.sources import discover_sources, load_document, validate_source_root
from src.core.rag.vector_store import VectorRecord, VectorStore


def validate_corpus(settings: Settings) -> dict[str, Any]:
    root = validate_source_root(settings.rag_source_root)
    sources = discover_sources(root)
    return {
        "status": "ok",
        "source_root": str(root),
        "source_count": len(sources),
        "supported_suffixes": sorted({path.suffix.lower() for path in sources}),
        "would_generate_embeddings": False,
        "would_write_index": False,
    }


def build_index(
    settings: Settings,
    *,
    embedder: Embedder | None = None,
    vector_store: VectorStore | None = None,
) -> dict[str, Any]:
    """Build only when this function is explicitly invoked by an operator."""
    if not settings.rag_enabled:
        raise ValueError("SOORIN_RAG_ENABLED must be true for indexing.")
    settings.validate_rag_embedding_configuration()
    settings.validate_rag_qdrant_configuration()
    sources = discover_sources(settings.rag_source_root)
    documents = [load_document(path, settings.rag_source_root) for path in sources]
    chunks: list[SourceChunk] = []
    for document in documents:
        chunks.extend(
            chunk_document(
                document,
                chunk_size=settings.rag_chunk_size_chars,
                overlap=settings.rag_chunk_overlap_chars,
            )
        )
    active_embedder = embedder or HuggingFaceTextEmbedder(
        settings.rag_embedding_model,
        settings.rag_embedding_dimension,
        local_files_only=settings.rag_embedding_local_files_only,
        cache_dir=settings.rag_embedding_cache_dir,
        revision=settings.rag_embedding_revision,
    )
    store = vector_store or QdrantVectorStore(
        mode=settings.rag_qdrant_mode,
        path=settings.rag_qdrant_path,
        url=settings.rag_qdrant_url,
        collection=settings.rag_collection,
        dimension=settings.rag_embedding_dimension,
        distance=settings.rag_distance,
        api_key=settings.rag_qdrant_api_key,
        timeout_seconds=settings.rag_qdrant_timeout_seconds,
        batch_size=settings.rag_upsert_batch_size,
    )
    store.ensure_collection()
    vectors = active_embedder.embed_documents([chunk.text for chunk in chunks])
    indexed_at = datetime.now(timezone.utc).isoformat()
    records = [
        VectorRecord(
            id=chunk.chunk_id,
            vector=vector,
            payload={
                "document_id": chunk.document_id,
                "chunk_id": chunk.chunk_id,
                "source_path": chunk.source_path,
                "relative_path": chunk.relative_path,
                "section": chunk.section,
                "category": chunk.category,
                "title": chunk.title,
                "text": chunk.text,
                "content_hash": chunk.content_hash,
                "indexed_at": indexed_at,
                "source_version": chunk.source_version,
            },
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    count = store.upsert(records)
    source_version = hashlib.sha256(
        "".join(document.content_hash for document in documents).encode("utf-8")
    ).hexdigest()
    return {
        "status": "ok",
        "backend": store.backend,
        "collection": settings.rag_collection,
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "upserted_count": count,
        "embedding_model": active_embedder.model_name,
        "embedding_dimension": active_embedder.dimension,
        "indexed_at": indexed_at,
        "source_version": source_version,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate or explicitly index the Soorin SOC corpus.")
    parser.add_argument("--build", action="store_true", help="Generate embeddings and upsert Qdrant records.")
    args = parser.parse_args()
    settings = get_settings()
    result = build_index(settings) if args.build else validate_corpus(settings)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
