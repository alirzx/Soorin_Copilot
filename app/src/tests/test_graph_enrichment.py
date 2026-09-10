"""Offline contracts for Product-to-Neo4j Asset enrichment."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.config.settings import get_settings
from src.core.graph.enrichment import AssetEnrichmentService
from src.core.graph.enrichment_models import (
    AssetEnrichmentMutation,
    EnrichmentAssetPage,
    EnrichmentWriteResult,
)
from src.core.graph.neo4j import Neo4jGraphRepository
from src.core.product_client import (
    ProductApiError,
    ProductAssetDetectionOverview,
    ProductAssetOverviewContractError,
    ProductAssetResponse,
    TopologyConnectionRecord,
)


NOW = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
SUMMARY = (
    "SOORIN-DC classified as Domain Controller from stored detection "
    "(model 99%, mapping 99%). 3 evidence item(s) support this prediction."
)
ROLES = [
    "Domain Controller",
    "Domain Joined Windows",
    "Domain Joined Server",
    "LDAP Server",
    "Global Catalog",
    "Database Server",
    "Web Server",
    "DNS Server",
]


def overview_payload(ip: str = "192.168.0.125") -> dict[str, Any]:
    return {
        "assetName": "SOORIN-DC",
        "ip": ip,
        "status": "CONFIRMED",
        "suggestedType": "Domain Controller",
        "modelConfidence": 0.99,
        "mappingConfidence": 0.99,
        "unknownScore": 0.01,
        "classificationSummary": SUMMARY,
        "vendor": "VMware",
        "product": "Active Directory",
        "role": "Domain Controller",
        "roles": list(ROLES),
        "tag": "services",
        "subTag": "Active Directory",
        "lastDetectionAt": "2026-09-09T13:04:41.835Z",
    }


def settings(**overrides: Any):
    values = {
        "graph_enrichment_enabled": True,
        "graph_enrichment_concurrency": 1,
        "graph_enrichment_batch_size": 2,
        "graph_enrichment_page_size": 5,
        "graph_enrichment_refresh_seconds": 259200,
        "graph_enrichment_retry_seconds": 3600,
    }
    values.update(overrides)
    return replace(get_settings(), **values)


def product_response(
    ip: str,
    payload: dict[str, Any] | None = None,
    *,
    found: bool = True,
) -> ProductAssetResponse:
    return ProductAssetResponse(
        target_ip=ip,
        raw_payload=payload if payload is not None else overview_payload(ip),
        endpoint_path=f"/asset-detection/{ip}/overview",
        status_code=200 if found else 404,
        elapsed_seconds=0.01,
        found=found,
    )


def test_exact_product_payload_preserves_all_fifteen_fields() -> None:
    overview = ProductAssetDetectionOverview.from_payload(overview_payload())
    properties = overview.property_values()

    assert properties == {
        "asset_name": "SOORIN-DC",
        "ip": "192.168.0.125",
        "status": "CONFIRMED",
        "suggested_type": "Domain Controller",
        "model_confidence": 0.99,
        "mapping_confidence": 0.99,
        "unknown_score": 0.01,
        "classification_summary": SUMMARY,
        "vendor": "VMware",
        "product": "Active Directory",
        "role": "Domain Controller",
        "roles": ROLES,
        "tag": "services",
        "sub_tag": "Active Directory",
        "last_detection_at": "2026-09-09T13:04:41.835000+00:00",
    }


def test_optional_nulls_missing_values_and_empty_roles_remain_unknown() -> None:
    nullable = {key: None for key in ProductAssetDetectionOverview._FIELDS}
    nullable["ip"] = "192.0.2.1"
    nullable["roles"] = []

    overview = ProductAssetDetectionOverview.from_payload(nullable)

    assert overview.roles == ()
    assert overview.property_values() == {"ip": "192.0.2.1", "roles": []}
    missing = ProductAssetDetectionOverview.from_payload({"ip": "192.0.2.2"})
    assert missing.property_values() == {"ip": "192.0.2.2"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("modelConfidence", "0.99"),
        ("mappingConfidence", 1.01),
        ("unknownScore", float("nan")),
        ("lastDetectionAt", "2026-09-09 13:04:41"),
    ],
)
def test_malformed_scores_and_timestamp_are_rejected(field: str, value: Any) -> None:
    payload = overview_payload()
    payload[field] = value
    with pytest.raises(ProductAssetOverviewContractError):
        ProductAssetDetectionOverview.from_payload(payload)


def test_unexpected_product_field_and_mismatched_ip_are_rejected() -> None:
    payload = overview_payload()
    payload["futureField"] = "unsupported"
    with pytest.raises(ProductAssetOverviewContractError, match="unsupported fields"):
        ProductAssetDetectionOverview.from_payload(payload)
    with pytest.raises(ProductAssetOverviewContractError, match="does not match"):
        ProductAssetDetectionOverview.from_payload(
            overview_payload(), expected_ip="192.168.0.126"
        )


def test_success_mutation_is_deterministic_and_tracks_72_hour_freshness() -> None:
    overview = ProductAssetDetectionOverview.from_payload(overview_payload())
    first = AssetEnrichmentMutation.success(
        overview, attempted_at=NOW, freshness_seconds=259200
    )
    second = AssetEnrichmentMutation.success(
        overview, attempted_at=NOW, freshness_seconds=259200
    )

    assert first == second
    assert first.next_due_at == (NOW + timedelta(hours=72)).isoformat()
    assert first.properties["roles"] == ROLES
    assert len(first.version or "") == 64


class RecordingProductClient:
    def __init__(
        self,
        responses: dict[str, ProductAssetResponse | Exception],
        *,
        delay: float = 0.0,
    ) -> None:
        self.responses = responses
        self.delay = delay
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self._counter_lock = threading.Lock()

    def get_asset_detection_overview(
        self, ip: str, *, request_id: str = ""
    ) -> ProductAssetResponse:
        del request_id
        with self._counter_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(ip)
        try:
            if self.delay:
                time.sleep(self.delay)
            response = self.responses[ip]
            if isinstance(response, Exception):
                raise response
            return response
        finally:
            with self._counter_lock:
                self.active -= 1


class RecordingWorkerRepository:
    def __init__(self, pages: dict[str | None, EnrichmentAssetPage]) -> None:
        self.pages = pages
        self.page_calls: list[dict[str, Any]] = []
        self.batches: list[list[AssetEnrichmentMutation]] = []
        self._write_lock = threading.Lock()

    def list_due_enrichment_assets(self, **kwargs: Any) -> EnrichmentAssetPage:
        self.page_calls.append(kwargs)
        return self.pages[kwargs["after_graph_key"]]

    def apply_enrichment_batch(
        self, mutations: list[AssetEnrichmentMutation]
    ) -> EnrichmentWriteResult:
        with self._write_lock:
            self.batches.append(list(mutations))
        return EnrichmentWriteResult(
            attempted=len(mutations), updated=len(mutations)
        )


def test_concurrent_sweep_and_on_demand_callers_share_one_product_lane() -> None:
    ips = ["192.0.2.1", "192.0.2.2", "192.0.2.3"]
    product = RecordingProductClient(
        {ip: product_response(ip) for ip in ips}, delay=0.015
    )
    repository = RecordingWorkerRepository(
        {
            None: EnrichmentAssetPage(
                graph_keys=(ips[0], ips[1]), next_cursor=ips[1], has_more=False
            )
        }
    )
    sweep_service = AssetEnrichmentService(
        settings(), product, repository, now=lambda: NOW  # type: ignore[arg-type]
    )
    on_demand_service = AssetEnrichmentService(
        settings(), product, repository, now=lambda: NOW  # type: ignore[arg-type]
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        sweep = executor.submit(sweep_service.run_page)
        on_demand = executor.submit(on_demand_service.enrich_asset, ips[2])
        sweep.result()
        on_demand.result()

    assert sorted(product.calls) == ips
    assert len(product.calls) == 3
    assert product.max_active == 1


def test_worker_continues_after_timeout_malformed_and_unavailable() -> None:
    ips = [f"192.0.2.{index}" for index in range(1, 6)]
    malformed = overview_payload(ips[2])
    malformed["modelConfidence"] = "high"
    product = RecordingProductClient(
        {
            ips[0]: product_response(ips[0]),
            ips[1]: ProductApiError("request timed out after bounded retries"),
            ips[2]: product_response(ips[2], malformed),
            ips[3]: ProductAssetResponse(
                target_ip=ips[3], raw_payload=None,
                endpoint_path=f"/asset-detection/{ips[3]}/overview",
                status_code=404, elapsed_seconds=0.01, found=False,
            ),
            ips[4]: product_response(ips[4]),
        }
    )
    repository = RecordingWorkerRepository(
        {None: EnrichmentAssetPage(tuple(ips), ips[-1], False)}
    )
    result = AssetEnrichmentService(
        settings(), product, repository, now=lambda: NOW  # type: ignore[arg-type]
    ).run_page()

    assert product.calls == ips
    assert (result.succeeded, result.failed, result.unavailable) == (2, 2, 1)
    assert [len(batch) for batch in repository.batches] == [2, 2, 1]
    mutations = [item for batch in repository.batches for item in batch]
    assert [item.status for item in mutations] == [
        "success", "error", "error", "unavailable", "success"
    ]
    assert all(
        item.properties == {}
        for item in mutations
        if item.status != "success"
    )
    assert mutations[1].next_due_at == (NOW + timedelta(hours=1)).isoformat()
    assert mutations[2].next_due_at == (NOW + timedelta(hours=72)).isoformat()
    assert mutations[3].next_due_at == (NOW + timedelta(hours=72)).isoformat()


def test_worker_exposes_stable_three_page_resume_contract() -> None:
    ips = [f"198.51.100.{index}" for index in range(1, 7)]
    repository = RecordingWorkerRepository(
        {
            None: EnrichmentAssetPage(tuple(ips[:2]), ips[1], True),
            ips[1]: EnrichmentAssetPage(tuple(ips[2:4]), ips[3], True),
            ips[3]: EnrichmentAssetPage(tuple(ips[4:]), ips[5], False),
        }
    )
    product = RecordingProductClient({ip: product_response(ip) for ip in ips})
    service = AssetEnrichmentService(
        settings(graph_enrichment_page_size=2),
        product,
        repository,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    page1 = service.run_page()
    page2 = service.run_page(after_graph_key=page1.next_cursor)
    page3 = service.run_page(after_graph_key=page2.next_cursor)

    assert [call["after_graph_key"] for call in repository.page_calls] == [
        None, ips[1], ips[3]
    ]
    assert [page1.has_more, page2.has_more, page3.has_more] == [True, True, False]
    assert product.calls == ips


def test_large_population_contract_processes_only_one_bounded_page() -> None:
    page_size = 100
    ips = [f"203.0.113.{index % 250 + 1}" for index in range(page_size)]
    repository = RecordingWorkerRepository(
        {None: EnrichmentAssetPage(tuple(ips), ips[-1], True)}
    )
    product = RecordingProductClient({ip: product_response(ip) for ip in set(ips)})

    result = AssetEnrichmentService(
        settings(graph_enrichment_page_size=page_size, graph_enrichment_batch_size=20),
        product,
        repository,  # type: ignore[arg-type]
        now=lambda: NOW,
    ).run_page()

    assert result.attempted == page_size
    assert len(product.calls) == page_size
    assert [len(batch) for batch in repository.batches] == [20] * 5
    assert result.has_more is True


class Result:
    def __init__(
        self,
        *,
        records: list[dict[str, Any]] | None = None,
        record: dict[str, Any] | None = None,
    ) -> None:
        self.records = records or []
        self.record = record

    def __iter__(self):
        return iter(self.records)

    def single(self):
        return self.record

    def consume(self) -> None:
        return None


class Transaction:
    def __init__(self, session: "RecordingSession") -> None:
        self.session = session

    def run(self, query: Any, **params: Any) -> Result:
        text = getattr(query, "text", query)
        self.session.write_calls.append((text, params))
        rows = params.get("rows", [])
        missing = [
            row["graph_key"] for row in rows
            if row.get("graph_key") in self.session.missing_keys
        ]
        return Result(
            record={"updated": len(rows) - len(missing), "missing_graph_keys": missing}
        )


class RecordingSession:
    def __init__(
        self,
        *,
        read_records: list[dict[str, Any]] | None = None,
        missing_keys: set[str] | None = None,
    ) -> None:
        self.read_records = read_records or []
        self.missing_keys = missing_keys or set()
        self.read_calls: list[tuple[str, dict[str, Any]]] = []
        self.write_calls: list[tuple[str, dict[str, Any]]] = []
        self.transactions = 0

    def run(self, query: Any, **params: Any) -> Result:
        text = getattr(query, "text", query)
        self.read_calls.append((text, params))
        return Result(records=self.read_records)

    def execute_write(self, callback):
        self.transactions += 1
        return callback(Transaction(self))


class RecordingDriver:
    def __init__(self, session: RecordingSession) -> None:
        self.session_value = session

    @contextmanager
    def session(self):
        yield self.session_value


def test_repository_uses_active_version_keyset_and_fetches_only_page_plus_one() -> None:
    session = RecordingSession(
        read_records=[{"graph_key": f"10.0.0.{index}"} for index in range(1, 5)]
    )
    repository = Neo4jGraphRepository(
        RecordingDriver(session), settings(graph_enrichment_page_size=3)  # type: ignore[arg-type]
    )

    page = repository.list_due_enrichment_assets(
        after_graph_key="10.0.0.0",
        limit=999,
        as_of=NOW,
        stale_before=NOW - timedelta(hours=72),
    )

    query, params = session.read_calls[0]
    assert page == EnrichmentAssetPage(
        ("10.0.0.1", "10.0.0.2", "10.0.0.3"), "10.0.0.3", True
    )
    assert "graph_version: m.active_graph_version" in query
    assert "a.graph_key > $after_graph_key" in query
    assert "ORDER BY a.graph_key ASC" in query
    assert "LIMIT $fetch_limit" in query
    assert "enrichment_status IS NULL" in query
    assert "enrichment_status = 'pending'" in query
    assert "enrichment_next_due_at <= $as_of" in query
    assert "enrichment_last_success_at <= $stale_before" in query
    assert params["fetch_limit"] == 4


def test_repository_batches_updates_and_reports_missing_without_creating_assets() -> None:
    session = RecordingSession(missing_keys={"10.0.0.4"})
    repository = Neo4jGraphRepository(
        RecordingDriver(session), settings(graph_enrichment_batch_size=2)  # type: ignore[arg-type]
    )
    overview = ProductAssetDetectionOverview.from_payload(overview_payload("10.0.0.1"))
    success = AssetEnrichmentMutation.success(
        overview, attempted_at=NOW, freshness_seconds=259200
    )
    mutations = [
        success,
        replace(success, graph_key="10.0.0.2"),
        replace(success, graph_key="10.0.0.3"),
        AssetEnrichmentMutation.failure(
            "10.0.0.4", attempted_at=NOW, retry_seconds=3600,
            unavailable=False, error="timeout",
        ),
        replace(success, graph_key="10.0.0.5"),
    ]

    result = repository.apply_enrichment_batch(mutations)

    assert session.transactions == 3
    assert [len(params["rows"]) for _, params in session.write_calls] == [2, 2, 1]
    assert result == EnrichmentWriteResult(5, 4, ("10.0.0.4",))
    query = session.write_calls[0][0]
    assert "UNWIND $rows AS row" in query
    assert "OPTIONAL MATCH (a:Asset" in query
    assert "graph_version: m.active_graph_version" in query
    assert "CREATE" not in query and "MERGE (a:Asset" not in query
    assert "SET a += row.properties" in query
    assert "a.enrichment_updated_at = row.succeeded_at" in query
    assert "WHEN a.enrichment_last_success_at IS NULL THEN row.status" in query


def test_schema_bootstrap_adds_only_the_enrichment_schedule_index() -> None:
    session = RecordingSession()
    repository = Neo4jGraphRepository(
        RecordingDriver(session), settings()  # type: ignore[arg-type]
    )

    repository.bootstrap_schema()

    statements = [query for query, _ in session.read_calls]
    assert len(statements) == 4
    assert sum("asset_enrichment_schedule" in query for query in statements) == 1
    assert "enrichment_next_due_at" in statements[-1]


def test_topology_publication_and_enrichment_write_share_mutation_boundary() -> None:
    staging_started = threading.Event()
    allow_staging_to_finish = threading.Event()
    session = RecordingSession()

    class BlockingSyncRepository(Neo4jGraphRepository):
        def _write_staging_nodes(self, nodes: list[str], version: str) -> None:
            del nodes, version
            staging_started.set()
            assert allow_staging_to_finish.wait(timeout=1.0)

        def _write_staging_edges(
            self, pairs: list[dict[str, Any]], version: str
        ) -> None:
            del pairs, version

        def _validate_staging(
            self, version: str, expected_nodes: int, expected_edges: int
        ) -> None:
            del version, expected_nodes, expected_edges

        def _publish(self, version: str) -> None:
            del version

        def _delete_inactive_versions(self, active_version: str) -> None:
            del active_version

    repository = BlockingSyncRepository(
        RecordingDriver(session), settings()  # type: ignore[arg-type]
    )
    overview = ProductAssetDetectionOverview.from_payload(
        overview_payload("10.0.0.1")
    )
    mutation = AssetEnrichmentMutation.success(
        overview, attempted_at=NOW, freshness_seconds=259200
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        sync = executor.submit(
            repository.sync_snapshot,
            [TopologyConnectionRecord("10.0.0.1", "10.0.0.2")],
            "v2",
        )
        assert staging_started.wait(timeout=1.0)
        enrichment = executor.submit(repository.apply_enrichment_batch, [mutation])
        time.sleep(0.02)
        assert session.transactions == 0
        allow_staging_to_finish.set()
        sync.result()
        enrichment.result()

    assert session.transactions == 1


def test_failure_mutation_is_metadata_only_and_bounded() -> None:
    mutation = AssetEnrichmentMutation.failure(
        "10.0.0.1",
        attempted_at=NOW,
        retry_seconds=3600,
        unavailable=False,
        error="x" * 1000,
    )
    assert mutation.properties == {}
    assert mutation.status == "error"
    assert len(mutation.error or "") == 160
    assert mutation.succeeded_at is None
    assert mutation.version is None


def test_topology_staging_carries_all_enrichment_and_marks_new_assets_pending() -> None:
    session = RecordingSession()
    repository = Neo4jGraphRepository(
        RecordingDriver(session), settings()  # type: ignore[arg-type]
    )

    repository._write_staging_nodes(["192.0.2.1", "192.0.2.2"], "v2")

    assert session.transactions == 1
    query, params = session.write_calls[0]
    for property_name in ProductAssetDetectionOverview.from_payload(
        overview_payload()
    ).property_values():
        if property_name != "ip":
            assert f"a.{property_name} = previous.{property_name}" in query
    for metadata_name in (
        "enrichment_status",
        "enrichment_updated_at",
        "enrichment_last_attempt_at",
        "enrichment_last_success_at",
        "enrichment_next_due_at",
        "enrichment_source",
        "enrichment_version",
        "enrichment_error",
    ):
        assert f"a.{metadata_name} = previous.{metadata_name}" in query
    assert "SET a.enrichment_status = coalesce(a.enrichment_status, 'pending')" in query
    assert "COMMUNICATES_WITH" not in query
    assert params["version"] == "v2"


def test_enrichment_settings_defaults_and_bounds() -> None:
    configured = get_settings()
    assert configured.graph_enrichment_concurrency == 1
    assert configured.graph_enrichment_refresh_seconds == 259200
    with pytest.raises(ValueError, match="must be 1"):
        replace(
            configured, graph_enrichment_concurrency=2
        ).validate_graph_enrichment_configuration()
    with pytest.raises(ValueError, match="must not exceed"):
        replace(
            configured, graph_enrichment_batch_size=6, graph_enrichment_page_size=5
        ).validate_graph_enrichment_configuration()


def test_disabled_worker_does_not_enumerate_or_call_product() -> None:
    product = RecordingProductClient({})
    repository = RecordingWorkerRepository({})
    result = AssetEnrichmentService(
        settings(graph_enrichment_enabled=False),
        product,
        repository,  # type: ignore[arg-type]
        now=lambda: NOW,
    ).run_page(after_graph_key="cursor")
    assert result.disabled is True
    assert result.next_cursor == "cursor"
    assert product.calls == []
    assert repository.page_calls == []
    assert repository.batches == []
