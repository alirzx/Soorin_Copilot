"""Phase 4A.1 hardening contracts that do not require a live Neo4j server."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.config.settings import get_settings
from src.core.graph.neo4j import GraphProjectionStatus
from src.core.graph.refresh import GraphRefreshService
from src.core.graph.service import GraphService
from src.core.graph.storage import atomic_write_json
from src.core.graph.structured import AssetSearchFilters, AssetSearchRequest, AssetSearchResult


class _StructuredPolicy:
    @staticmethod
    def structured_limit(requested: int | None) -> int:
        return 50 if requested is None else requested


class _StructuredRepository:
    def __init__(self, *, graph_version: str = "graph-v2") -> None:
        self.policy = _StructuredPolicy()
        self.graph_version = graph_version
        self.requests: list[AssetSearchRequest] = []

    def search_assets(self, request: AssetSearchRequest) -> AssetSearchResult:
        self.requests.append(request)
        return AssetSearchResult(
            active_graph_version=self.graph_version,
            filters=request.filters,
            rows=(),
            returned_count=0,
            matched_total=0,
            truncated=True,
            next_cursor="repository-position",
            sort=request.sort,
            direction=request.direction,
            retrieved_at="2026-09-10T00:00:00+00:00",
        )


def _graph_service(repository: _StructuredRepository) -> GraphService:
    service = GraphService(replace(get_settings(), neo4j_password="unused"))
    service.repository = repository  # type: ignore[assignment]
    return service


def test_service_cursor_is_bound_to_filters_sort_direction_and_graph_version() -> None:
    repository = _StructuredRepository(graph_version="graph-v2")
    service = _graph_service(repository)
    first_request = AssetSearchRequest(
        filters=AssetSearchFilters(role="Domain Controller"),
        sort="asset_name",
        direction="asc",
        limit=10,
    )
    first = service.search_assets(first_request)
    assert first.next_cursor
    assert repository.requests[-1].cursor is None

    second = service.search_assets(first_request.model_copy(update={"cursor": first.next_cursor}))
    assert second.active_graph_version == "graph-v2"
    assert repository.requests[-1].cursor == "repository-position"

    with pytest.raises(ValueError, match="does not match"):
        service.search_assets(
            AssetSearchRequest(
                filters=AssetSearchFilters(role="Web Server"),
                sort="asset_name",
                direction="asc",
                limit=10,
                cursor=first.next_cursor,
            )
        )
    with pytest.raises(ValueError, match="does not match"):
        service.search_assets(
            AssetSearchRequest(
                filters=first_request.filters,
                sort="graph_key",
                direction="asc",
                limit=10,
                cursor=first.next_cursor,
            )
        )
    with pytest.raises(ValueError, match="does not match"):
        service.search_assets(
            AssetSearchRequest(
                filters=first_request.filters,
                sort="asset_name",
                direction="desc",
                limit=10,
                cursor=first.next_cursor,
            )
        )

    repository.graph_version = "graph-v3"
    with pytest.raises(ValueError, match="different active graph version"):
        service.search_assets(first_request.model_copy(update={"cursor": first.next_cursor}))


def test_service_cursor_allows_page_size_change_without_changing_query_identity() -> None:
    repository = _StructuredRepository()
    service = _graph_service(repository)
    request = AssetSearchRequest(filters=AssetSearchFilters(status="CONFIRMED"), limit=10)
    first = service.search_assets(request)
    service.search_assets(request.model_copy(update={"limit": 20, "cursor": first.next_cursor}))
    assert repository.requests[-1].limit == 20
    assert repository.requests[-1].cursor == "repository-position"


class _RefreshRepository:
    def __init__(self) -> None:
        self.status_calls = 0

    def status(self) -> GraphProjectionStatus:
        self.status_calls += 1
        if self.status_calls <= 2:
            return GraphProjectionStatus("graph-v1", 2, 1, "2026-09-10T00:00:00+00:00")
        raise RuntimeError("neo4j unavailable while reporting failure")


class _FailingProductClient:
    @staticmethod
    def fetch_topology_unique_ip_pairs():
        raise RuntimeError("product unavailable")


def test_refresh_failure_reporting_does_not_mask_original_failure() -> None:
    settings = replace(
        get_settings(),
        graph_refresh_lock_timeout_seconds=1,
        graph_refresh_interval_seconds=3600,
    )
    service = GraphRefreshService(
        settings,
        _FailingProductClient(),  # type: ignore[arg-type]
        _RefreshRepository(),  # type: ignore[arg-type]
    )
    result = service.refresh_once(force=True)
    assert result.status == "error"
    assert result.message == "RuntimeError"


def test_atomic_json_write_removes_temporary_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "snapshot.json"
    original_replace = Path.replace

    def fail_replace(path: Path, target: Path):
        if path.name.endswith(".tmp"):
            raise OSError("replace failed")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_json({"ok": True}, output)
    assert not output.exists()
    assert list(tmp_path.glob("*.tmp")) == []
