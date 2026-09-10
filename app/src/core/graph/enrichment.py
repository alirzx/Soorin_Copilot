"""Bounded serialized Product-to-Neo4j Asset enrichment worker."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from _thread import LockType
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.config.settings import Settings
from src.core.graph.enrichment_models import (
    AssetEnrichmentActionResult,
    AssetEnrichmentMutation,
    AssetEnrichmentRunResult,
    EnrichmentRefreshMode,
    EnrichmentTrigger,
    EnrichmentWriteResult,
)
from src.core.graph.leases import RenewingNeo4jLease
from src.core.graph.neo4j import Neo4jGraphRepository
from src.core.observability.metrics import SoorinMetrics, get_metrics
from src.core.product_client import (
    ProductApiClient,
    ProductApiError,
    ProductAssetDetectionOverview,
    ProductAssetOverviewContractError,
)


_PRODUCT_OVERVIEW_REQUEST_LOCK: LockType = threading.Lock()
_PRODUCT_OVERVIEW_LEASE = "graph_enrichment_product_request"
logger = logging.getLogger(__name__)


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
        metrics: SoorinMetrics | None = None,
        owner_id: str | None = None,
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
        self._metrics = metrics or get_metrics()
        self._owner_id = owner_id or f"enrichment-{uuid.uuid4()}"

    def run_page(
        self,
        *,
        after_graph_key: str | None = None,
        request_id: str = "graph-enrichment",
        trigger: EnrichmentTrigger = "scheduled",
        cancel_event: threading.Event | None = None,
        prefer_stale: bool = False,
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
            prefer_stale=prefer_stale,
        )
        buffer: list[AssetEnrichmentMutation] = []
        attempted = succeeded = failed = unavailable = updated = missing = batches = 0

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
            if cancel_event is not None and cancel_event.is_set():
                break
            mutation = self._fetch_mutation(
                graph_key,
                request_id=f"{request_id}:{graph_key}",
                trigger=trigger,
                cancel_event=cancel_event,
            )
            if mutation is None:
                break
            attempted += 1
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
        result = AssetEnrichmentRunResult(
            attempted=attempted,
            succeeded=succeeded,
            failed=failed,
            unavailable=unavailable,
            updated=updated,
            missing_topology_assets=missing,
            batches=batches,
            next_cursor=page.next_cursor,
            has_more=page.has_more or attempted < len(page.graph_keys),
        )
        self._metrics.observe_graph_enrichment_assets(
            trigger,
            attempted=result.attempted,
            succeeded=result.succeeded,
            failed=result.failed,
            unavailable=result.unavailable,
            updated=result.updated,
            skipped=result.missing_topology_assets,
        )
        return result

    def enrich_asset(
        self,
        graph_key: str,
        *,
        request_id: str = "graph-enrichment-on-demand",
        mode: EnrichmentRefreshMode = "refresh_if_stale",
        trigger: EnrichmentTrigger = "on_demand",
    ) -> AssetEnrichmentActionResult:
        """Refresh one active Asset through the canonical freshness-aware path."""
        if not self.settings.graph_enrichment_enabled:
            self._metrics.observe_graph_enrichment_assets(trigger, skipped=1)
            return AssetEnrichmentActionResult(
                graph_key, mode, False, False, True, message="enrichment_disabled"
            )
        if mode not in {"refresh_if_stale", "force_refresh"}:
            raise ValueError(f"Unsupported enrichment refresh mode: {mode}")
        as_of = self._now()
        eligibility = self.repository.get_asset_enrichment_eligibility(
            graph_key,
            as_of=as_of,
            stale_before=as_of
            - timedelta(seconds=self.settings.graph_enrichment_refresh_seconds),
        )
        if not eligibility.found:
            self._metrics.observe_graph_enrichment_assets(trigger, skipped=1)
            return AssetEnrichmentActionResult(
                graph_key, mode, False, False, True, message="asset_not_found"
            )
        if mode == "refresh_if_stale" and not eligibility.needs_refresh:
            self._metrics.observe_graph_enrichment_assets(trigger, skipped=1)
            return AssetEnrichmentActionResult(
                graph_key,
                mode,
                True,
                False,
                True,
                status=eligibility.status,
                message="asset_enrichment_fresh",
            )

        logger.info(
            "event=enrichment_on_demand_started trigger=%s mode=%s",
            trigger,
            mode,
        )
        mutation = self._fetch_mutation(
            graph_key,
            request_id=request_id,
            trigger=trigger,
        )
        write = self.repository.apply_enrichment_batch([mutation])
        self._metrics.observe_graph_enrichment_assets(
            trigger,
            attempted=1,
            succeeded=1 if mutation.status == "success" else 0,
            failed=1 if mutation.status == "error" else 0,
            unavailable=1 if mutation.status == "unavailable" else 0,
            updated=write.updated,
            skipped=len(write.missing_graph_keys),
        )
        logger.info(
            "event=enrichment_on_demand_completed trigger=%s mode=%s outcome=%s updated=%s",
            trigger,
            mode,
            mutation.status,
            write.updated,
        )
        return AssetEnrichmentActionResult(
            graph_key=graph_key,
            mode=mode,
            found=not bool(write.missing_graph_keys),
            refreshed=True,
            skipped=False,
            status=mutation.status,
            updated=write.updated,
            message="updated" if write.updated else "active_asset_changed",
        )

    def _fetch_mutation(
        self,
        graph_key: str,
        *,
        request_id: str,
        trigger: EnrichmentTrigger,
        cancel_event: threading.Event | None = None,
    ) -> AssetEnrichmentMutation | None:
        while not self._product_request_lock.acquire(timeout=0.1):
            if cancel_event is not None and cancel_event.is_set():
                return None
        try:
            lease = RenewingNeo4jLease(
                self.repository,
                lease_name=_PRODUCT_OVERVIEW_LEASE,
                owner_id=f"{self._owner_id}:{uuid.uuid4()}",
                ttl_seconds=self.settings.graph_enrichment_lease_ttl_seconds,
            )
            if not lease.acquire(wait=True, cancel_event=cancel_event):
                return None
            attempted_at = self._now()
            started = time.perf_counter()
            outcome = "error"
            try:
                response = self.product_client.get_asset_detection_overview(
                    graph_key,
                    request_id=request_id,
                )
                if response.found is False or response.raw_payload is None:
                    outcome = "unavailable"
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
                outcome = "success"
                return AssetEnrichmentMutation.success(
                    overview,
                    attempted_at=attempted_at,
                    freshness_seconds=self.settings.graph_enrichment_refresh_seconds,
                )
            except ProductAssetOverviewContractError as exc:
                logger.warning(
                    "event=enrichment_product_failure trigger=%s error_type=%s",
                    trigger,
                    type(exc).__name__,
                )
                return AssetEnrichmentMutation.failure(
                    graph_key,
                    attempted_at=attempted_at,
                    retry_seconds=self.settings.graph_enrichment_refresh_seconds,
                    unavailable=False,
                    error=f"contract_error:{exc}",
                )
            except ProductApiError as exc:
                logger.warning(
                    "event=enrichment_product_failure trigger=%s error_type=%s",
                    trigger,
                    type(exc).__name__,
                )
                return AssetEnrichmentMutation.failure(
                    graph_key,
                    attempted_at=attempted_at,
                    retry_seconds=self.settings.graph_enrichment_retry_seconds,
                    unavailable=False,
                    error=f"{type(exc).__name__}:{exc}",
                )
            finally:
                self._metrics.observe_graph_enrichment_product(
                    trigger,
                    outcome=outcome,
                    duration_seconds=time.perf_counter() - started,
                )
                lease.release()
        finally:
            self._product_request_lock.release()
