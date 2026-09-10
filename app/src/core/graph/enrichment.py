"""Bounded serialized Product-to-Neo4j Asset enrichment worker."""

from __future__ import annotations

import threading
from _thread import LockType
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.config.settings import Settings
from src.core.graph.enrichment_models import (
    AssetEnrichmentMutation,
    AssetEnrichmentRunResult,
    EnrichmentWriteResult,
)
from src.core.graph.neo4j import Neo4jGraphRepository
from src.core.product_client import (
    ProductApiClient,
    ProductApiError,
    ProductAssetDetectionOverview,
    ProductAssetOverviewContractError,
)


_PRODUCT_OVERVIEW_REQUEST_LOCK: LockType = threading.Lock()


class AssetEnrichmentService:
    """Process one bounded keyset page with one serialized Product lane."""

    def __init__(
        self,
        settings: Settings,
        product_client: ProductApiClient,
        repository: Neo4jGraphRepository,
        *,
        product_request_lock: LockType | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if settings.graph_enrichment_concurrency != 1:
            raise ValueError(
                "SOORIN_GRAPH_ENRICHMENT_CONCURRENCY must be 1 for the current Product backend."
            )
        self.settings = settings
        self.product_client = product_client
        self.repository = repository
        self._product_request_lock = (
            product_request_lock or _PRODUCT_OVERVIEW_REQUEST_LOCK
        )
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run_page(
        self,
        *,
        after_graph_key: str | None = None,
        request_id: str = "graph-enrichment",
    ) -> AssetEnrichmentRunResult:
        """Process at most one configured page and return a stable resume cursor."""
        if not self.settings.graph_enrichment_enabled:
            return AssetEnrichmentRunResult(
                attempted=0,
                succeeded=0,
                failed=0,
                unavailable=0,
                updated=0,
                missing_topology_assets=0,
                batches=0,
                next_cursor=after_graph_key,
                has_more=False,
                disabled=True,
            )
        as_of = self._now()
        page = self.repository.list_due_enrichment_assets(
            after_graph_key=after_graph_key,
            limit=self.settings.graph_enrichment_page_size,
            as_of=as_of,
            stale_before=as_of
            - timedelta(seconds=self.settings.graph_enrichment_refresh_seconds),
        )
        buffer: list[AssetEnrichmentMutation] = []
        succeeded = failed = unavailable = updated = missing = batches = 0

        def flush() -> None:
            nonlocal updated, missing, batches
            if not buffer:
                return
            write = self.repository.apply_enrichment_batch(buffer)
            updated += write.updated
            missing += len(write.missing_graph_keys)
            batches += 1
            buffer.clear()

        for graph_key in page.graph_keys:
            mutation = self._fetch_mutation(
                graph_key,
                request_id=f"{request_id}:{graph_key}",
            )
            if mutation.status == "success":
                succeeded += 1
            elif mutation.status == "unavailable":
                unavailable += 1
            else:
                failed += 1
            buffer.append(mutation)
            if len(buffer) >= self.settings.graph_enrichment_batch_size:
                flush()
        flush()
        return AssetEnrichmentRunResult(
            attempted=len(page.graph_keys),
            succeeded=succeeded,
            failed=failed,
            unavailable=unavailable,
            updated=updated,
            missing_topology_assets=missing,
            batches=batches,
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    def enrich_asset(
        self,
        graph_key: str,
        *,
        request_id: str = "graph-enrichment-on-demand",
    ) -> EnrichmentWriteResult:
        """Reuse the serialized lane and mutation path for one future on-demand call."""
        mutation = self._fetch_mutation(graph_key, request_id=request_id)
        return self.repository.apply_enrichment_batch([mutation])

    def _fetch_mutation(
        self,
        graph_key: str,
        *,
        request_id: str,
    ) -> AssetEnrichmentMutation:
        try:
            with self._product_request_lock:
                attempted_at = self._now()
                response = self.product_client.get_asset_detection_overview(
                    graph_key,
                    request_id=request_id,
                )
            if response.found is False or response.raw_payload is None:
                return AssetEnrichmentMutation.failure(
                    graph_key,
                    attempted_at=attempted_at,
                    retry_seconds=self.settings.graph_enrichment_refresh_seconds,
                    unavailable=True,
                    error="Product detection overview was unavailable.",
                )
            overview = ProductAssetDetectionOverview.from_payload(
                response.raw_payload,
                expected_ip=graph_key,
            )
            return AssetEnrichmentMutation.success(
                overview,
                attempted_at=attempted_at,
                freshness_seconds=self.settings.graph_enrichment_refresh_seconds,
            )
        except ProductAssetOverviewContractError as exc:
            return AssetEnrichmentMutation.failure(
                graph_key,
                attempted_at=attempted_at,
                retry_seconds=self.settings.graph_enrichment_refresh_seconds,
                unavailable=False,
                error=f"contract_error:{exc}",
            )
        except ProductApiError as exc:
            return AssetEnrichmentMutation.failure(
                graph_key,
                attempted_at=attempted_at,
                retry_seconds=self.settings.graph_enrichment_retry_seconds,
                unavailable=False,
                error=f"{type(exc).__name__}:{exc}",
            )
