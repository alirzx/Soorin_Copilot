"""Focused contracts for the organizational RFC1918 endpoint projection."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

from src.config.settings import get_settings
from src.core.graph.neo4j import GraphProjectionStatus
from src.core.graph.organizational_neo4j import (
    ORGANIZATIONAL_PROJECTION_SCHEMA_VERSION,
    OrganizationalNeo4jGraphRepository,
)
from src.core.graph.organizational_refresh import GraphRefreshService
from src.core.graph.refresh import GraphRefreshService as BaseGraphRefreshService
from src.core.product_client.schemas import (
    ProductTopologyResponse,
    TopologyConnectionRecord,
)


def _pairs(rows: list[dict[str, object]]) -> set[tuple[str, str, int]]:
    return {
        (str(row["source"]), str(row["target"]), int(row["weight"]))
        for row in rows
    }


def test_destination_only_rfc1918_endpoint_is_an_asset_node() -> None:
    nodes, edges = OrganizationalNeo4jGraphRepository._projection(
        [TopologyConnectionRecord("10.0.0.1", "10.0.0.2")]
    )

    assert nodes == ["10.0.0.1", "10.0.0.2"]
    assert _pairs(edges) == {("10.0.0.1", "10.0.0.2", 1)}


def test_multiple_destination_only_endpoints_and_all_private_pairs_are_retained() -> None:
    records = [
        TopologyConnectionRecord("10.0.0.1", "10.0.0.2"),
        TopologyConnectionRecord("10.0.0.1", "172.16.0.3"),
        TopologyConnectionRecord("10.0.0.1", "192.168.0.4"),
    ]

    nodes, edges = OrganizationalNeo4jGraphRepository._projection(records)

    assert set(nodes) == {
        "10.0.0.1",
        "10.0.0.2",
        "172.16.0.3",
        "192.168.0.4",
    }
    assert len(edges) == len(records)


def test_public_peers_are_excluded_without_losing_private_endpoints() -> None:
    records = [
        TopologyConnectionRecord("10.0.0.1", "8.8.8.8"),
        TopologyConnectionRecord("1.1.1.1", "10.0.0.3"),
    ]

    nodes, edges = OrganizationalNeo4jGraphRepository._projection(records)

    assert nodes == ["10.0.0.1", "10.0.0.3"]
    assert edges == []


def test_duplicate_pairs_preserve_normalized_weights() -> None:
    records = [
        TopologyConnectionRecord("10.0.0.1", "10.0.0.2", weight=2),
        TopologyConnectionRecord("10.0.0.1", "10.0.0.2", weight=3),
        TopologyConnectionRecord("10.0.0.2", "10.0.0.1"),
    ]

    nodes, edges = OrganizationalNeo4jGraphRepository._projection(records)

    assert nodes == ["10.0.0.1", "10.0.0.2"]
    assert _pairs(edges) == {
        ("10.0.0.1", "10.0.0.2", 5),
        ("10.0.0.2", "10.0.0.1", 1),
    }


class _RecordingRepository(OrganizationalNeo4jGraphRepository):
    def __init__(self) -> None:
        self.staged_nodes: list[str] = []
        self.staged_edges: list[dict[str, object]] = []
        self.validated: tuple[int, int] | None = None

    def _write_staging_nodes(self, nodes: list[str], version: str) -> int:
        del version
        self.staged_nodes = nodes
        return len(nodes)

    def _write_staging_edges(
        self, pairs: list[dict[str, object]], version: str
    ) -> None:
        del version
        self.staged_edges = pairs

    def _validate_staging(
        self, version: str, expected_nodes: int, expected_edges: int
    ) -> None:
        del version
        self.validated = (expected_nodes, expected_edges)

    def _publish(self, version: str) -> None:
        del version

    def _delete_inactive_versions(self, active_version: str) -> None:
        del active_version


def test_snapshot_publishes_private_node_with_only_public_peer() -> None:
    repository = _RecordingRepository()

    status = repository.sync_snapshot(
        [TopologyConnectionRecord("10.0.0.1", "8.8.8.8")], "graph-v3"
    )

    assert repository.staged_nodes == ["10.0.0.1"]
    assert repository.staged_edges == []
    assert repository.validated == (1, 0)
    assert status.nodes == 1
    assert status.edges == 0


class _RefreshRepository(OrganizationalNeo4jGraphRepository):
    def __init__(self) -> None:
        self.synced_records: list[TopologyConnectionRecord] = []

    def status(self) -> GraphProjectionStatus:
        return GraphProjectionStatus(None, 0, 0, None)

    def sync_snapshot(
        self, records: list[TopologyConnectionRecord], version: str
    ) -> GraphProjectionStatus:
        self.synced_records = records
        nodes, edges = self._projection(records)
        return GraphProjectionStatus(version, len(nodes), len(edges), None)


class _ProductClient:
    def __init__(self, records: list[TopologyConnectionRecord]) -> None:
        self.records = records

    def fetch_topology_unique_ip_pairs(self) -> ProductTopologyResponse:
        return ProductTopologyResponse(
            raw_payload=[
                {"src_ip": record.src_ip, "dst_ip": record.dst_ip}
                for record in self.records
            ],
            records=self.records,
            endpoint_path="/topology",
            status_code=200,
            elapsed_seconds=0.01,
        )


def test_refresh_prevalidation_uses_projection_node_candidates(tmp_path) -> None:
    records = [
        TopologyConnectionRecord("10.0.0.1", "8.8.8.8"),
        TopologyConnectionRecord("10.0.0.2", "10.0.0.3"),
    ]
    repository = _RefreshRepository()
    settings = replace(
        get_settings(),
        graph_auto_refresh_enabled=False,
        graph_refresh_min_nodes=3,
        graph_refresh_min_edges=1,
        graph_refresh_lock_timeout_seconds=1,
        graph_raw_path=str(tmp_path / "topology.json"),
        graph_refresh_keep_raw_snapshots=0,
    )
    service = BaseGraphRefreshService(
        settings,
        _ProductClient(records),  # type: ignore[arg-type]
        repository,
    )

    result = service.refresh_once(force=True)

    assert result.status == "ok"
    assert result.nodes == 3
    assert result.edges == 1
    assert repository.synced_records == records


def test_schema_v2_projection_forces_startup_refresh_to_schema_v3() -> None:
    repository = _RecordingRepository()
    repository.projection_schema_version = lambda: 2  # type: ignore[method-assign]
    service = object.__new__(GraphRefreshService)
    service.repository = repository
    sentinel = object()

    with patch.object(
        BaseGraphRefreshService,
        "refresh_once",
        return_value=sentinel,
    ) as refresh_once:
        result = GraphRefreshService.refresh_once(service, reason="startup")

    assert ORGANIZATIONAL_PROJECTION_SCHEMA_VERSION == 3
    assert result is sentinel
    refresh_once.assert_called_once_with(
        force=True,
        reason="organizational_projection_schema_migration",
    )
