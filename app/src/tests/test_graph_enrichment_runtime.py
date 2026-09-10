"""Deterministic contracts for graph enrichment runtime scheduling and leases."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from prometheus_client import CollectorRegistry

from src.api.main import create_app
from src.config.settings import get_settings
from src.core.graph.enrichment import AssetEnrichmentService
from src.core.graph.enrichment_models import (
    AssetEnrichmentEligibility,
    AssetEnrichmentRunResult,
    EnrichmentWriteResult,
)
from src.core.graph.enrichment_runtime import GraphEnrichmentRuntimeService
from src.core.graph.neo4j import GraphProjectionStatus, Neo4jGraphRepository
from src.core.graph.refresh import GraphRefreshService
from src.core.observability.metrics import SoorinMetrics
from src.core.product_client import ProductAssetResponse, ProductTopologyResponse, TopologyConnectionRecord


NOW = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


def runtime_settings(**overrides: Any):
    values = {
        "graph_enrichment_enabled": True,
        "graph_enrichment_concurrency": 1,
        "graph_enrichment_batch_size": 2,
        "graph_enrichment_page_size": 2,
        "graph_enrichment_refresh_seconds": 259200,
        "graph_enrichment_retry_seconds": 3600,
        "graph_enrichment_poll_interval_seconds": 1,
        "graph_enrichment_startup_delay_seconds": 0,
        "graph_enrichment_max_pages_per_cycle": 3,
        "graph_enrichment_lease_ttl_seconds": 3,
        "graph_enrichment_shutdown_timeout_seconds": 2,
    }
    values.update(overrides)
    return replace(get_settings(), **values)


class SharedLeaseBackend:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.leases: dict[str, tuple[str, float]] = {}

    def acquire(self, name: str, owner: str, ttl: int) -> bool:
        with self.lock:
            current = self.leases.get(name)
            if current is not None and current[0] != owner and current[1] > time.monotonic():
                return False
            self.leases[name] = (owner, time.monotonic() + ttl)
            return True

    def renew(self, name: str, owner: str, ttl: int) -> bool:
        with self.lock:
            current = self.leases.get(name)
            if current is None or current[0] != owner:
                return False
            self.leases[name] = (owner, time.monotonic() + ttl)
            return True

    def release(self, name: str, owner: str) -> bool:
        with self.lock:
            current = self.leases.get(name)
            if current is None or current[0] != owner:
                return False
            del self.leases[name]
            return True


class RuntimeRepository:
    def __init__(self, backend: SharedLeaseBackend | None = None) -> None:
        self.backend = backend or SharedLeaseBackend()
        self.eligibility = AssetEnrichmentEligibility(
            "192.0.2.1", True, True, "pending"
        )
        self.writes = 0
        self.schema_calls = 0
        self.bootstrap_entered = threading.Event()
        self.bootstrap_release: threading.Event | None = None

    def bootstrap_schema(self) -> None:
        self.schema_calls += 1
        self.bootstrap_entered.set()
        if self.bootstrap_release is not None:
            assert self.bootstrap_release.wait(timeout=2)

    def try_acquire_lease(self, name: str, owner: str, *, ttl_seconds: int, **_kwargs: Any) -> bool:
        return self.backend.acquire(name, owner, ttl_seconds)

    def renew_lease(self, name: str, owner: str, *, ttl_seconds: int, **_kwargs: Any) -> bool:
        return self.backend.renew(name, owner, ttl_seconds)

    def release_lease(self, name: str, owner: str) -> bool:
        return self.backend.release(name, owner)

    def enrichment_backlog_counts(self, **_kwargs: Any) -> dict[str, int]:
        return {"pending": 1, "stale": 2, "error": 3, "unavailable": 4, "backlog": 10}

    def get_asset_enrichment_eligibility(self, graph_key: str, **_kwargs: Any) -> AssetEnrichmentEligibility:
        return replace(self.eligibility, graph_key=graph_key)

    def apply_enrichment_batch(self, mutations: list[Any]) -> EnrichmentWriteResult:
        self.writes += len(mutations)
        return EnrichmentWriteResult(len(mutations), len(mutations))


def page_result(*, attempted: int, has_more: bool) -> AssetEnrichmentRunResult:
    return AssetEnrichmentRunResult(
        attempted=attempted,
        succeeded=attempted,
        failed=0,
        unavailable=0,
        updated=attempted,
        missing_topology_assets=0,
        batches=1 if attempted else 0,
        next_cursor=None,
        has_more=has_more,
    )


class RecordingWorker:
    def __init__(self, results: list[AssetEnrichmentRunResult | Exception]) -> None:
        self.results = list(results)
        self.calls = 0
        self.call_arguments: list[dict[str, Any]] = []
        self.called = threading.Event()
        self.lock = threading.Lock()

    def run_page(self, **kwargs: Any) -> AssetEnrichmentRunResult:
        with self.lock:
            self.calls += 1
            self.call_arguments.append(kwargs)
            self.called.set()
            result = self.results.pop(0) if self.results else page_result(attempted=0, has_more=False)
        if isinstance(result, Exception):
            raise result
        return result


def build_runtime(settings, worker, repository) -> GraphEnrichmentRuntimeService:
    return GraphEnrichmentRuntimeService(
        settings,
        worker,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        metrics=SoorinMetrics(enabled=True, registry=CollectorRegistry()),
        now=lambda: NOW,
    )


def test_disabled_scheduler_does_not_start() -> None:
    runtime = build_runtime(
        runtime_settings(graph_enrichment_enabled=False),
        RecordingWorker([]),
        RuntimeRepository(),
    )
    assert runtime.start() is False
    assert runtime.status()["started"] is False
    assert runtime.stop() is True


def test_enabled_start_is_non_blocking_and_shutdown_is_clean() -> None:
    repository = RuntimeRepository()
    repository.bootstrap_release = threading.Event()
    runtime = build_runtime(runtime_settings(), RecordingWorker([]), repository)

    assert runtime.start() is True
    assert repository.bootstrap_entered.wait(timeout=1)
    assert runtime.status()["started"] is True
    repository.bootstrap_release.set()
    assert runtime.stop() is True
    assert runtime.status()["started"] is False


def test_wake_interrupts_idle_wait_without_sleeping_for_poll_interval() -> None:
    worker = RecordingWorker([page_result(attempted=0, has_more=False)] * 2)
    runtime = build_runtime(runtime_settings(graph_enrichment_poll_interval_seconds=30), worker, RuntimeRepository())
    assert runtime.start() is True
    assert worker.called.wait(timeout=1)
    worker.called.clear()

    assert runtime.wake(reason="manual") is True
    assert worker.called.wait(timeout=1)
    assert runtime.stop() is True
    assert worker.calls >= 2


def test_scheduler_error_is_recorded_and_next_wake_still_runs() -> None:
    worker = RecordingWorker(
        [RuntimeError("boom"), page_result(attempted=0, has_more=False)]
    )
    runtime = build_runtime(runtime_settings(graph_enrichment_poll_interval_seconds=30), worker, RuntimeRepository())
    assert runtime.start() is True
    assert worker.called.wait(timeout=1)
    worker.called.clear()
    assert runtime.status()["consecutive_failures"] == 1

    runtime.wake(reason="manual")
    assert worker.called.wait(timeout=1)
    assert runtime.stop() is True
    assert worker.calls >= 2


def test_many_due_pages_stop_at_exact_cycle_bound() -> None:
    worker = RecordingWorker([page_result(attempted=2, has_more=True)] * 8)
    runtime = build_runtime(
        runtime_settings(graph_enrichment_max_pages_per_cycle=3),
        worker,
        RuntimeRepository(),
    )

    result = runtime.run_bounded_cycle(trigger="manual")

    assert result.status == "completed"
    assert result.pages == 3
    assert result.attempted == 6
    assert result.has_more is True
    assert worker.calls == 3
    assert [call["prefer_stale"] for call in worker.call_arguments] == [
        False,
        False,
        True,
    ]


class RecordingProductClient:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.lock = threading.Lock()

    def get_asset_detection_overview(self, ip: str, *, request_id: str = "") -> ProductAssetResponse:
        del request_id
        with self.lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            return ProductAssetResponse(
                target_ip=ip,
                raw_payload={"ip": ip},
                endpoint_path=f"/asset-detection/{ip}/overview",
                status_code=200,
                elapsed_seconds=0.02,
                found=True,
            )
        finally:
            with self.lock:
                self.active -= 1


def test_two_logical_process_owners_share_distributed_product_lane() -> None:
    backend = SharedLeaseBackend()
    repositories = [RuntimeRepository(backend), RuntimeRepository(backend)]
    product = RecordingProductClient()
    services = [
        AssetEnrichmentService(
            runtime_settings(),
            product,  # type: ignore[arg-type]
            repository,  # type: ignore[arg-type]
            product_request_lock=threading.Lock(),
            now=lambda: NOW,
            owner_id=f"process-{index}",
        )
        for index, repository in enumerate(repositories)
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                service.enrich_asset,
                f"192.0.2.{index + 1}",
                mode="force_refresh",
            )
            for index, service in enumerate(services)
        ]
        results = [future.result() for future in futures]

    assert all(result.refreshed for result in results)
    assert product.calls == 2
    assert product.max_active == 1


@pytest.mark.parametrize(
    ("status", "needs_refresh", "mode", "expected_calls", "expected_refreshed"),
    [
        ("success", False, "refresh_if_stale", 0, False),
        ("stale", True, "refresh_if_stale", 1, True),
        (None, True, "refresh_if_stale", 1, True),
        ("success", False, "force_refresh", 1, True),
    ],
    ids=["fresh-skip", "stale-refresh", "missing-enrichment-refresh", "force-fresh"],
)
def test_single_asset_refresh_modes(
    status: str | None,
    needs_refresh: bool,
    mode: str,
    expected_calls: int,
    expected_refreshed: bool,
) -> None:
    repository = RuntimeRepository()
    repository.eligibility = AssetEnrichmentEligibility(
        "192.0.2.1",
        True,
        needs_refresh,
        status,  # type: ignore[arg-type]
        last_success_at=NOW.isoformat() if not needs_refresh else None,
        next_due_at=(NOW + timedelta(hours=72)).isoformat() if not needs_refresh else None,
    )
    product = RecordingProductClient()
    service = AssetEnrichmentService(
        runtime_settings(),
        product,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = service.enrich_asset(
        "192.0.2.1",
        mode=mode,  # type: ignore[arg-type]
    )

    assert product.calls == expected_calls
    assert result.refreshed is expected_refreshed


def test_nonexistent_active_asset_never_calls_product() -> None:
    repository = RuntimeRepository()
    repository.eligibility = AssetEnrichmentEligibility("192.0.2.99", False, False)
    product = RecordingProductClient()
    service = AssetEnrichmentService(
        runtime_settings(), product, repository, now=lambda: NOW  # type: ignore[arg-type]
    )

    result = service.enrich_asset("192.0.2.99", mode="force_refresh")

    assert result.found is False
    assert result.message == "asset_not_found"
    assert product.calls == 0


def test_runtime_recreation_rediscovers_durable_due_work() -> None:
    repository = RuntimeRepository()
    first_worker = RecordingWorker([page_result(attempted=0, has_more=False)])
    second_worker = RecordingWorker([page_result(attempted=2, has_more=False)])

    first = build_runtime(runtime_settings(), first_worker, repository)
    second = build_runtime(runtime_settings(), second_worker, repository)
    assert first.run_bounded_cycle(trigger="manual").attempted == 0
    assert second.run_bounded_cycle(trigger="manual").attempted == 2
    assert first_worker.calls == 1
    assert second_worker.calls == 1


def test_topology_publication_wakes_runtime_only_after_publish(tmp_path) -> None:
    published = threading.Event()
    callback_observed_publish = threading.Event()
    records = [
        TopologyConnectionRecord("192.0.2.1", "192.0.2.2"),
        TopologyConnectionRecord("192.0.2.2", "192.0.2.3"),
    ]

    class Product:
        def fetch_topology_unique_ip_pairs(self) -> ProductTopologyResponse:
            return ProductTopologyResponse(
                raw_payload=[
                    {"src_ip": record.src_ip, "dst_ip": record.dst_ip}
                    for record in records
                ],
                records=records,
                endpoint_path="/topology",
                status_code=200,
                elapsed_seconds=0.01,
            )

    class Repository:
        _normalize = staticmethod(Neo4jGraphRepository._normalize)

        def status(self) -> GraphProjectionStatus:
            return GraphProjectionStatus("v1", 2, 1, NOW.isoformat())

        def sync_snapshot(self, _records, version: str) -> GraphProjectionStatus:
            published.set()
            return GraphProjectionStatus(version, 3, 2, NOW.isoformat(), 1)

    def wake(_count: int, _version: str) -> None:
        if published.is_set():
            callback_observed_publish.set()

    configured = runtime_settings(
        graph_auto_refresh_enabled=False,
        graph_raw_path=str(tmp_path / "topology.json"),
        graph_refresh_keep_raw_snapshots=0,
    )
    service = GraphRefreshService(
        configured,
        Product(),  # type: ignore[arg-type]
        Repository(),  # type: ignore[arg-type]
        on_new_pending_assets=wake,
    )

    result = service.refresh_once(force=True)

    assert result.activated is True
    assert callback_observed_publish.is_set()


def test_fastapi_startup_and_shutdown_manage_enrichment_runtime() -> None:
    calls: list[str] = []

    class RefreshRuntime:
        def load_last_known_good(self) -> bool:
            calls.append("refresh_load")
            return True

        def start_background(self) -> None:
            calls.append("refresh_start")

        def stop_background(self) -> None:
            calls.append("refresh_stop")

    class EnrichmentRuntime:
        def start(self) -> bool:
            calls.append("enrichment_start")
            return True

        def stop(self) -> bool:
            calls.append("enrichment_stop")
            return True

    repository = SimpleNamespace(
        driver=SimpleNamespace(close=lambda: calls.append("repository_close"))
    )
    configured = runtime_settings()
    with (
        patch("src.api.main.get_settings", return_value=configured),
        patch("src.api.main.get_api_graph_refresh_service", return_value=RefreshRuntime()),
        patch("src.api.main.get_graph_enrichment_runtime_service", return_value=EnrichmentRuntime()),
        patch("src.api.main.get_graph_repository", return_value=repository),
        patch("src.api.main.copilot_service.close", side_effect=lambda: calls.append("copilot_close")),
    ):
        app = create_app()
        for handler in app.router.on_startup:
            handler()
        for handler in app.router.on_shutdown:
            handler()

    assert calls == [
        "refresh_load",
        "enrichment_start",
        "refresh_start",
        "refresh_stop",
        "enrichment_stop",
        "repository_close",
        "copilot_close",
    ]
