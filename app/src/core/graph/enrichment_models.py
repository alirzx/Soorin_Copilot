"""Typed contracts shared by graph enrichment workers and persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from src.core.product_client import ProductAssetDetectionOverview


EnrichmentStatus = Literal["pending", "success", "stale", "error", "unavailable"]


@dataclass(frozen=True)
class EnrichmentAssetPage:
    """One stable keyset page from the active topology projection."""

    graph_keys: tuple[str, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class AssetEnrichmentMutation:
    """One success/failure mutation consumed by a Neo4j UNWIND batch."""

    graph_key: str
    status: EnrichmentStatus
    attempted_at: str
    next_due_at: str
    properties: dict[str, Any]
    source: str
    version: str | None = None
    succeeded_at: str | None = None
    error: str | None = None

    @classmethod
    def success(
        cls,
        overview: ProductAssetDetectionOverview,
        *,
        attempted_at: datetime,
        freshness_seconds: int,
    ) -> "AssetEnrichmentMutation":
        properties = overview.property_values()
        canonical = json.dumps(
            properties,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        timestamp = attempted_at.isoformat()
        return cls(
            graph_key=overview.ip,
            status="success",
            attempted_at=timestamp,
            succeeded_at=timestamp,
            next_due_at=(
                attempted_at + timedelta(seconds=freshness_seconds)
            ).isoformat(),
            properties=properties,
            source="product_asset_detection_overview",
            version=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def failure(
        cls,
        graph_key: str,
        *,
        attempted_at: datetime,
        retry_seconds: int,
        unavailable: bool,
        error: str,
    ) -> "AssetEnrichmentMutation":
        return cls(
            graph_key=graph_key,
            status="unavailable" if unavailable else "error",
            attempted_at=attempted_at.isoformat(),
            next_due_at=(
                attempted_at + timedelta(seconds=retry_seconds)
            ).isoformat(),
            properties={},
            source="product_asset_detection_overview",
            error=(error.strip() or "enrichment_failed")[:160],
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "graph_key": self.graph_key,
            "status": self.status,
            "attempted_at": self.attempted_at,
            "succeeded_at": self.succeeded_at,
            "next_due_at": self.next_due_at,
            "properties": dict(self.properties),
            "source": self.source,
            "version": self.version,
            "error": self.error,
        }


@dataclass(frozen=True)
class EnrichmentWriteResult:
    attempted: int
    updated: int
    missing_graph_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssetEnrichmentRunResult:
    """Bounded worker-page result and restart cursor."""

    attempted: int
    succeeded: int
    failed: int
    unavailable: int
    updated: int
    missing_topology_assets: int
    batches: int
    next_cursor: str | None
    has_more: bool
    disabled: bool = False
