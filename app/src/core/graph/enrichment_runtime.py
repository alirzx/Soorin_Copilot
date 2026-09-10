"""Managed bounded runtime scheduler for Product-backed graph enrichment."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.config.settings import Settings
from src.core.graph.enrichment import AssetEnrichmentService
from src.core.graph.enrichment_models import AssetEnrichmentRunResult, EnrichmentTrigger
from src.core.graph.leases import RenewingNeo4jLease
from src.core.graph.neo4j import Neo4jGraphRepository
from src.core.observability.metrics import SoorinMetrics, get_metrics


logger = logging.getLogger(__name__)
_SCHEDULER_LEASE = "graph_enrichment_scheduler"


@dataclass(frozen=True)
class GraphEnrichmentCycleResult:
    status: str
    trigger: EnrichmentTrigger
    pages: int
    attempted: int
    succeeded: int
    failed: int
    unavailable: int
    updated: int
    skipped: int
    has_more: bool
    message: str = ""


@dataclass
class GraphEnrichmentRuntimeStatus:
    enabled: bool
    started: bool = False
    running: bool = False
    owns_lease: bool = False
    poll_interval_seconds: int = 0
    max_pages_per_cycle: int = 0
    last_wake_at: str | None = None
    last_wake_reason: str | None = None
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_error_type: str | None = None
    consecutive_failures: int = 0
    last_pages: int = 0
    last_attempted: int = 0
    last_updated: int = 0
    backlog: dict[str, int] | None = None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["backlog"] = dict(self.backlog or {})
        return value


class GraphEnrichmentRuntimeService:
    """Own scheduler lifecycle and repeatedly orchestrate the existing worker."""

    def __init__(
        self,
        settings: Settings,
        worker: AssetEnrichmentService,
        repository: Neo4jGraphRepository,
        *,
        metrics: SoorinMetrics | None = None,
        now: Callable[[], datetime] | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.worker = worker
        self.repository = repository
        self.metrics = metrics or get_metrics()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._owner_id = owner_id or f"scheduler-{uuid.uuid4()}"
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._status_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._active_lease: RenewingNeo4jLease | None = None
        self._schema_ready = False
        self._status = GraphEnrichmentRuntimeStatus(
            enabled=settings.graph_enrichment_enabled,
            poll_interval_seconds=settings.graph_enrichment_poll_interval_seconds,
            max_pages_per_cycle=settings.graph_enrichment_max_pages_per_cycle,
            backlog={
                "pending": 0,
                "stale": 0,
                "error": 0,
                "unavailable": 0,
                "backlog": 0,
            },
        )

    def start(self) -> bool:
        if not self.settings.graph_enrichment_enabled:
            logger.info("event=enrichment_scheduler_disabled")
            return False
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="soorin-graph-enrichment",
            daemon=True,
        )
        with self._status_lock:
            self._status.started = True
        self._thread.start()
        logger.info(
            "event=enrichment_scheduler_started poll_interval_seconds=%s startup_delay_seconds=%s max_pages_per_cycle=%s lease_ttl_seconds=%s",
            self.settings.graph_enrichment_poll_interval_seconds,
            self.settings.graph_enrichment_startup_delay_seconds,
            self.settings.graph_enrichment_max_pages_per_cycle,
            self.settings.graph_enrichment_lease_ttl_seconds,
        )
        return True

    def stop(self, timeout_seconds: float | None = None) -> bool:
        self._stop_event.set()
        self._wake_event.set()
        timeout = (
            self.settings.graph_enrichment_shutdown_timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        stopped = self._thread is None or not self._thread.is_alive()
        if stopped:
            with self._status_lock:
                self._status.started = False
                self._status.running = False
        else:
            logger.warning(
                "event=enrichment_scheduler_shutdown_timeout timeout_seconds=%s",
                timeout,
            )
        logger.info("event=enrichment_scheduler_stopped clean=%s", stopped)
        return stopped

    def wake(self, *, reason: str = "manual") -> bool:
        if not self.settings.graph_enrichment_enabled:
            return False
        now = self._now().isoformat()
        with self._status_lock:
            self._status.last_wake_at = now
            self._status.last_wake_reason = reason[:80]
        self._wake_event.set()
        if reason == "new_assets":
            logger.info("event=enrichment_wakeup_new_assets")
        return True

    def status(self) -> dict[str, object]:
        with self._status_lock:
            return self._status.to_dict()

    def run_bounded_cycle(
        self,
        *,
        trigger: EnrichmentTrigger = "manual",
    ) -> GraphEnrichmentCycleResult:
        """Run one bounded cycle if this process can acquire scheduler ownership."""
        if not self.settings.graph_enrichment_enabled:
            return self._empty_result("disabled", trigger, "enrichment_disabled")
        lease = RenewingNeo4jLease(
            self.repository,
            lease_name=_SCHEDULER_LEASE,
            owner_id=f"{self._owner_id}:manual",
            ttl_seconds=self.settings.graph_enrichment_lease_ttl_seconds,
        )
        if not lease.acquire(wait=False):
            return self._empty_result("skipped", trigger, "scheduler_lease_unavailable")
        self._set_lease_owned(True)
        try:
            return self._run_cycle(trigger, lease=lease)
        finally:
            lease.release()
            self._set_lease_owned(False)

    def _run_loop(self) -> None:
        if self._stop_event.wait(self.settings.graph_enrichment_startup_delay_seconds):
            return
        while not self._stop_event.is_set():
            if not self._schema_ready:
                try:
                    self.repository.bootstrap_schema()
                    self._schema_ready = True
                except Exception:
                    logger.exception("event=enrichment_scheduler_schema_unavailable")
                    self._record_runtime_failure(RuntimeError("schema_unavailable"))
                    self._wait_for_next_poll()
                    continue
            lease = RenewingNeo4jLease(
                self.repository,
                lease_name=_SCHEDULER_LEASE,
                owner_id=self._owner_id,
                ttl_seconds=self.settings.graph_enrichment_lease_ttl_seconds,
            )
            self._active_lease = lease
            try:
                if not lease.acquire(wait=False):
                    self._wait_for_next_poll()
                    continue
                self._set_lease_owned(True)
                logger.info("event=enrichment_scheduler_lease_acquired")
                while not self._stop_event.is_set() and not lease.lost:
                    self._run_cycle("scheduled", lease=lease)
                    self._wait_for_next_poll()
                if lease.lost:
                    logger.warning("event=enrichment_scheduler_lease_lost")
            except Exception:
                logger.exception("event=enrichment_scheduler_boundary_failed")
                self._record_runtime_failure(RuntimeError("scheduler_boundary_failed"))
                self._wait_for_next_poll()
            finally:
                lease.release()
                self._active_lease = None
                self._set_lease_owned(False)

    def _wait_for_next_poll(self) -> None:
        self._wake_event.wait(self.settings.graph_enrichment_poll_interval_seconds)
        self._wake_event.clear()

    def _run_cycle(
        self,
        trigger: EnrichmentTrigger,
        *,
        lease: RenewingNeo4jLease | None = None,
    ) -> GraphEnrichmentCycleResult:
        if self._stop_event.is_set():
            return self._empty_result("skipped", trigger, "runtime_stopping")
        if not self._cycle_lock.acquire(blocking=False):
            return self._empty_result("skipped", trigger, "cycle_already_running")
        started = time.perf_counter()
        started_at = self._now()
        self.metrics.graph_enrichment_cycle_started(trigger)
        with self._status_lock:
            self._status.running = True
            self._status.last_run_at = started_at.isoformat()
        logger.info(
            "event=enrichment_cycle_started trigger=%s max_pages=%s",
            trigger,
            self.settings.graph_enrichment_max_pages_per_cycle,
        )
        pages = attempted = succeeded = failed = unavailable = updated = skipped = 0
        has_more = False
        try:
            while (
                pages < self.settings.graph_enrichment_max_pages_per_cycle
                and not self._stop_event.is_set()
                and not (lease is not None and lease.lost)
            ):
                page: AssetEnrichmentRunResult = self.worker.run_page(
                    after_graph_key=None,
                    request_id=f"graph-enrichment-{trigger}",
                    trigger=trigger,
                    cancel_event=self._stop_event,
                    prefer_stale=(
                        self.settings.graph_enrichment_max_pages_per_cycle > 1
                        and pages
                        == self.settings.graph_enrichment_max_pages_per_cycle - 1
                    ),
                )
                if page.disabled or page.attempted == 0:
                    has_more = False
                    break
                pages += 1
                attempted += page.attempted
                succeeded += page.succeeded
                failed += page.failed
                unavailable += page.unavailable
                updated += page.updated
                skipped += page.missing_topology_assets
                has_more = page.has_more
                if not page.has_more:
                    break

            backlog = self._refresh_backlog()
            completed_at = self._now()
            result = GraphEnrichmentCycleResult(
                "completed",
                trigger,
                pages,
                attempted,
                succeeded,
                failed,
                unavailable,
                updated,
                skipped,
                has_more,
            )
            with self._status_lock:
                self._status.running = False
                self._status.last_success_at = completed_at.isoformat()
                self._status.last_error_type = None
                self._status.consecutive_failures = 0
                self._status.last_pages = pages
                self._status.last_attempted = attempted
                self._status.last_updated = updated
                self._status.backlog = backlog
            self.metrics.observe_graph_enrichment_cycle(
                trigger,
                outcome="completed",
                duration_seconds=time.perf_counter() - started,
                timestamp=completed_at.timestamp(),
            )
            logger.info(
                "event=enrichment_cycle_completed trigger=%s pages=%s attempted=%s updated=%s has_more=%s",
                trigger,
                pages,
                attempted,
                updated,
                has_more,
            )
            return result
        except Exception as exc:
            self._record_runtime_failure(exc)
            self.metrics.observe_graph_enrichment_cycle(
                trigger,
                outcome="failed",
                duration_seconds=time.perf_counter() - started,
                timestamp=self._now().timestamp(),
            )
            logger.exception(
                "event=enrichment_cycle_failed trigger=%s error_type=%s",
                trigger,
                type(exc).__name__,
            )
            return GraphEnrichmentCycleResult(
                "failed",
                trigger,
                pages,
                attempted,
                succeeded,
                failed,
                unavailable,
                updated,
                skipped,
                has_more,
                type(exc).__name__,
            )
        finally:
            with self._status_lock:
                self._status.running = False
            self._cycle_lock.release()

    def _refresh_backlog(self) -> dict[str, int]:
        as_of = self._now()
        counts = self.repository.enrichment_backlog_counts(
            as_of=as_of,
            stale_before=as_of
            - timedelta(seconds=self.settings.graph_enrichment_refresh_seconds),
        )
        self.metrics.set_graph_enrichment_backlog(counts)
        return counts

    def _record_runtime_failure(self, exc: Exception) -> None:
        with self._status_lock:
            self._status.running = False
            self._status.last_failure_at = self._now().isoformat()
            self._status.last_error_type = type(exc).__name__
            self._status.consecutive_failures += 1

    def _set_lease_owned(self, owned: bool) -> None:
        with self._status_lock:
            self._status.owns_lease = owned
        self.metrics.set_graph_enrichment_lease_owned(owned)

    @staticmethod
    def _empty_result(
        status: str,
        trigger: EnrichmentTrigger,
        message: str,
    ) -> GraphEnrichmentCycleResult:
        return GraphEnrichmentCycleResult(
            status, trigger, 0, 0, 0, 0, 0, 0, 0, False, message
        )
