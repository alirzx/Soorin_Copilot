"""Lazy Qdrant adapter implementing the Soorin vector-store contract."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from src.core.rag.vector_store import (
    VectorCollectionInfo,
    VectorRecord,
    VectorSearchHit,
    VectorStoreHealth,
)


logger = logging.getLogger(__name__)

VALID_DISTANCES = {"cosine", "dot", "euclid"}
VALID_MODES = {"server", "local"}


class QdrantVectorStore:
    """Qdrant-backed implementation of the Soorin vector-store contract.

    Supported modes:

    - ``server``: Connect to a running Qdrant service using ``url``.
    - ``local``: Use the qdrant-client embedded local storage using ``path``.

    Client creation is lazy. Constructing this class does not connect to a
    server, create directories, inspect collections, or load any model.
    """

    backend = "qdrant"

    def __init__(
        self,
        *,
        url: str = "",
        collection: str,
        dimension: int,
        distance: str = "cosine",
        api_key: str = "",
        timeout_seconds: float = 10.0,
        batch_size: int = 64,
        mode: str = "server",
        path: str = "",
        embedding_model: str = "",
        client: Any | None = None,
    ) -> None:
        self.mode = mode.strip().lower()
        self.url = url.strip().rstrip("/")
        self.path = path.strip()
        self.embedding_model = embedding_model.strip()
        self.collection = collection.strip()
        self.dimension = int(dimension)
        self.distance = distance.strip().lower()
        self.api_key = api_key.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.batch_size = max(1, int(batch_size))
        self._client = client

        if self.mode not in VALID_MODES:
            raise ValueError(
                f"Unsupported Qdrant mode: {self.mode!r}. "
                f"Expected one of: {sorted(VALID_MODES)}."
            )

        if not self.collection:
            raise ValueError("Qdrant collection name must not be empty.")

        if self.dimension < 1:
            raise ValueError("Qdrant vector dimension must be positive.")

        if self.distance not in VALID_DISTANCES:
            raise ValueError(
                "Qdrant distance must be cosine, dot, or euclid."
            )

        if self.timeout_seconds <= 0:
            raise ValueError("Qdrant timeout must be positive.")

        if self.mode == "local" and not self.path and client is None:
            raise ValueError(
                "Qdrant local mode requires a persistent storage path."
            )

        if self.mode == "server" and not self.url and client is None:
            raise ValueError(
                "Qdrant server mode requires a Qdrant URL."
            )

    @property
    def configured(self) -> bool:
        """Return whether the selected backend mode has sufficient settings."""
        if not self.collection:
            return False

        if self._client is not None:
            return True

        if self.mode == "local":
            return bool(self.path)

        return bool(self.url)

    def _get_client(self) -> Any:
        """Create and cache the Qdrant client only when first used."""
        if self._client is not None:
            return self._client

        from qdrant_client import QdrantClient

        if self.mode == "local":
            storage_path = Path(self.path).expanduser().resolve()
            storage_path.mkdir(parents=True, exist_ok=True)

            self._client = QdrantClient(
                path=str(storage_path),
            )
        else:
            self._client = QdrantClient(
                url=self.url,
                api_key=self.api_key or None,
                timeout=self.timeout_seconds,
            )

        return self._client

    @staticmethod
    def _distance_name(value: Any) -> str:
        raw = getattr(value, "value", value)
        return str(raw or "").lower()

    @staticmethod
    def _is_collection_missing_error(exc: Exception) -> bool:
        """Recognize missing-collection errors across local and server clients."""
        error_name = type(exc).__name__

        if error_name in {
            "UnexpectedResponse",
            "NotFoundError",
            "ValueError",
        }:
            message = str(exc).lower()
            return any(
                phrase in message
                for phrase in (
                    "not found",
                    "doesn't exist",
                    "does not exist",
                    "collection",
                )
            )

        return False

    @staticmethod
    def safe_error_reason(exc: Exception) -> str:
        """Return a bounded diagnostic without paths, credentials, or payloads."""
        message = " ".join(str(exc).split())
        message = re.sub(
            r"(?i)\b(?:api[_-]?key|token|authorization|password)\s*[=:]\s*\S+",
            "[REDACTED]",
            message,
        )
        message = re.sub(r"(?<![\w.])(?:~|/)[^\s,;:]+", "<path>", message)
        return (message or type(exc).__name__)[:180]

    @property
    def storage_path_kind(self) -> str:
        if self.mode == "server":
            return "remote_url"
        if not self.path:
            return "not_configured"
        return "absolute_local" if Path(self.path).expanduser().is_absolute() else "relative_local"

    def collection_info(self) -> VectorCollectionInfo | None:
        """Return normalized collection metadata.

        Returns ``None`` only when the store is not configured. Missing
        collections are allowed to raise so callers can distinguish them from
        configuration absence.
        """
        if not self.configured:
            return None

        info = self._get_client().get_collection(self.collection)

        config = getattr(info, "config", None)
        params = getattr(config, "params", None)
        vectors = getattr(params, "vectors", None)

        # Named-vector collections return a mapping. The current Soorin
        # baseline supports one unnamed dense vector only.
        if isinstance(vectors, dict):
            if len(vectors) != 1:
                raise ValueError(
                    "Soorin RAG expects one dense Qdrant vector configuration."
                )
            vectors = next(iter(vectors.values()))

        dimension = int(getattr(vectors, "size", 0) or 0)
        distance = self._distance_name(
            getattr(vectors, "distance", "")
        )
        points_count = getattr(info, "points_count", None)

        return VectorCollectionInfo(
            name=self.collection,
            dimension=dimension,
            distance=distance,
            points_count=(
                int(points_count)
                if points_count is not None
                else None
            ),
        )

    def health(self) -> VectorStoreHealth:
        """Check dependency, collection availability, and vector compatibility."""
        if not self.configured:
            return VectorStoreHealth(
                status="not_configured",
                backend=self.backend,
                collection=self.collection,
                configured=False,
                available=False,
            )

        try:
            info = self.collection_info()
        except ModuleNotFoundError:
            return VectorStoreHealth(
                status="unavailable",
                backend=self.backend,
                collection=self.collection,
                configured=True,
                available=False,
                error_classification="dependency_missing",
            )
        except Exception as exc:
            classification = (
                "collection_missing"
                if self._is_collection_missing_error(exc)
                else type(exc).__name__
            )

            logger.warning(
                "event=rag_vector_store_unavailable "
                "backend=qdrant mode=%s collection=%s embedding_model=%s "
                "storage_path_kind=%s error_type=%s error_reason=%s",
                self.mode,
                self.collection,
                self.embedding_model or "not_configured",
                self.storage_path_kind,
                type(exc).__name__,
                self.safe_error_reason(exc),
            )

            return VectorStoreHealth(
                status="unavailable",
                backend=self.backend,
                collection=self.collection,
                configured=True,
                available=False,
                error_classification=classification,
                error_reason=self.safe_error_reason(exc),
            )

        if info is None:
            return VectorStoreHealth(
                status="unavailable",
                backend=self.backend,
                collection=self.collection,
                configured=True,
                available=False,
                error_classification="collection_missing",
            )

        if (
            info.dimension != self.dimension
            or info.distance != self.distance
        ):
            return VectorStoreHealth(
                status="invalid",
                backend=self.backend,
                collection=self.collection,
                configured=True,
                available=False,
                dimension=info.dimension,
                distance=info.distance,
                error_classification=(
                    "collection_vector_config_mismatch"
                ),
                error_reason=(
                    "Configured embedding dimension or distance does not match "
                    "the existing collection."
                ),
            )

        return VectorStoreHealth(
            status="ok",
            backend=self.backend,
            collection=self.collection,
            configured=True,
            available=True,
            dimension=info.dimension,
            distance=info.distance,
        )

    def ensure_collection(self) -> VectorCollectionInfo:
        """Create a missing collection without replacing incompatible data."""
        client = self._get_client()

        existing: VectorCollectionInfo | None = None

        try:
            # Recent qdrant-client versions expose collection_exists for both
            # local and remote operation.
            if hasattr(client, "collection_exists"):
                exists = bool(
                    client.collection_exists(
                        collection_name=self.collection
                    )
                )
                if exists:
                    existing = self.collection_info()
            else:
                existing = self.collection_info()
        except Exception as exc:
            if not self._is_collection_missing_error(exc):
                raise
            existing = None

        if existing is not None:
            if (
                existing.dimension != self.dimension
                or existing.distance != self.distance
            ):
                raise ValueError(
                    "Existing Qdrant collection vector configuration "
                    "is incompatible."
                )
            return existing

        from qdrant_client.http import models

        distances = {
            "cosine": models.Distance.COSINE,
            "dot": models.Distance.DOT,
            "euclid": models.Distance.EUCLID,
        }

        client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(
                size=self.dimension,
                distance=distances[self.distance],
            ),
        )

        return VectorCollectionInfo(
            name=self.collection,
            dimension=self.dimension,
            distance=self.distance,
            points_count=0,
        )

    @staticmethod
    def _query_filter(
        filters: dict[str, Any] | None,
    ) -> Any:
        """Convert simple equality filters into Qdrant filter objects."""
        if not filters:
            return None

        try:
            from qdrant_client.http import models
        except ModuleNotFoundError:
            # Useful for dependency-free test doubles.
            return filters

        return models.Filter(
            must=[
                models.FieldCondition(
                    key=key,
                    match=models.MatchValue(value=value),
                )
                for key, value in sorted(filters.items())
            ]
        )

    def search(
        self,
        query_vector: Sequence[float],
        *,
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]:
        """Search for the nearest vectors using an optional payload filter."""
        vector = [float(value) for value in query_vector]

        if len(vector) != self.dimension:
            raise ValueError(
                "Query vector dimension does not match "
                "Qdrant collection configuration."
            )

        client = self._get_client()
        query_filter = self._query_filter(filters)
        limit = max(1, int(top_k))

        if hasattr(client, "query_points"):
            response = client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            rows = getattr(response, "points", response)
        else:
            # Compatibility path for older clients and simple test doubles.
            rows = client.search(
                collection_name=self.collection,
                query_vector=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )

        return [
            VectorSearchHit(
                id=str(getattr(row, "id", "")),
                score=float(
                    getattr(row, "score", 0.0) or 0.0
                ),
                payload=dict(
                    getattr(row, "payload", {}) or {}
                ),
            )
            for row in rows
        ]

    @staticmethod
    def _normalize_point_id(value: str) -> int | str:
        """Convert arbitrary stable record IDs into Qdrant-compatible IDs.

        Qdrant point IDs must be unsigned integers or UUID strings. Stable
        chunk hashes and other arbitrary strings are deterministically mapped
        to UUIDv5 values.
        """
        raw = str(value).strip()

        if not raw:
            raise ValueError("Qdrant point ID must not be empty.")

        if raw.isdigit():
            numeric = int(raw)
            if numeric >= 0:
                return numeric

        try:
            return str(uuid.UUID(raw))
        except ValueError:
            return str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"soorin-rag:{raw}",
                )
            )

    @classmethod
    def _point(cls, record: VectorRecord) -> Any:
        point_id = cls._normalize_point_id(record.id)
        payload = dict(record.payload)

        # Preserve the original stable application ID when it was converted
        # to a Qdrant-compatible UUID.
        payload.setdefault("record_id", str(record.id))

        try:
            from qdrant_client.http import models
        except ModuleNotFoundError:
            return {
                "id": point_id,
                "vector": [
                    float(value)
                    for value in record.vector
                ],
                "payload": payload,
            }

        return models.PointStruct(
            id=point_id,
            vector=[
                float(value)
                for value in record.vector
            ],
            payload=payload,
        )

    def upsert(
        self,
        records: Sequence[VectorRecord],
    ) -> int:
        """Insert or update records in deterministic batches."""
        if not records:
            return 0

        client = self._get_client()
        total = 0

        for start in range(0, len(records), self.batch_size):
            batch = list(
                records[start : start + self.batch_size]
            )

            for record in batch:
                if len(record.vector) != self.dimension:
                    raise ValueError(
                        "Upsert vector dimension does not match "
                        "Qdrant collection configuration."
                    )

            client.upsert(
                collection_name=self.collection,
                points=[
                    self._point(record)
                    for record in batch
                ],
                wait=True,
            )
            total += len(batch)

        return total

    def delete(self, ids: Sequence[str]) -> int:
        """Delete records using their original application IDs."""
        if not ids:
            return 0

        client = self._get_client()
        normalized_ids = [
            self._normalize_point_id(value)
            for value in ids
        ]

        try:
            from qdrant_client.http import models
        except ModuleNotFoundError:
            selector: Any = normalized_ids
        else:
            selector = models.PointIdsList(
                points=normalized_ids
            )

        client.delete(
            collection_name=self.collection,
            points_selector=selector,
            wait=True,
        )

        return len(ids)

    def close(self) -> None:
        """Close the underlying client when supported."""
        if self._client is None:
            return

        close = getattr(self._client, "close", None)
        if callable(close):
            close()

        self._client = None
