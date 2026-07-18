"""Soorin-owned vector-store contracts, independent of vendor clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Sequence


VectorStoreStatus = Literal["ok", "not_configured", "unavailable", "invalid"]


@dataclass(frozen=True)
class VectorStoreHealth:
    status: VectorStoreStatus
    backend: str
    collection: str
    configured: bool
    available: bool
    dimension: int | None = None
    distance: str | None = None
    error_classification: str | None = None


@dataclass(frozen=True)
class VectorCollectionInfo:
    name: str
    dimension: int
    distance: str
    points_count: int | None = None


@dataclass(frozen=True)
class VectorRecord:
    id: str
    vector: Sequence[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VectorSearchHit:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
    backend: str

    def health(self) -> VectorStoreHealth: ...

    def search(
        self,
        query_vector: Sequence[float],
        *,
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]: ...

    def upsert(self, records: Sequence[VectorRecord]) -> int: ...

    def delete(self, ids: Sequence[str]) -> int: ...

    def collection_info(self) -> VectorCollectionInfo | None: ...

    def ensure_collection(self) -> VectorCollectionInfo: ...
