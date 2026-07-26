#!/usr/bin/env python3
"""Run one read-only application-level Knowledge retrieval diagnostic."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from src.config.settings import get_settings
from src.core.rag.service import KnowledgeSearchService


def main() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    service = KnowledgeSearchService(settings)
    try:
        health = service.vector_store.health() if service.vector_store else None
        print(f"backend={settings.rag_backend}")
        print(f"mode={settings.rag_qdrant_mode}")
        print(f"collection={settings.rag_collection}")
        print(f"model={settings.rag_embedding_model}")
        print(f"embedding_dimension={settings.rag_embedding_dimension}")
        if health is not None and not health.available:
            print(f"status={health.status}")
            print(f"error_classification={health.error_classification or 'unknown'}")
            if health.error_reason:
                print(f"error_reason={health.error_reason}")
            return 1

        collection_info = service.vector_store.collection_info()
        if collection_info is not None:
            print(f"point_count={collection_info.points_count}")
        result = service.search("What is Kerberos?", request_id="rag-diagnostic")
        print(f"status={result.status}")
        print(f"result_count={result.included_count}")
        for chunk in result.chunks:
            print(
                "result "
                f"score={chunk.score:.4f} "
                f"title={chunk.title or 'untitled'} "
                f"source={chunk.relative_path or 'unavailable'}"
            )
        return 0 if result.status in {"ok", "partial"} and result.included_count else 1
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
