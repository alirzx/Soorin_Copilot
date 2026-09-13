"""Offline contracts for Phase 4A structured graph retrieval."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from src.config.settings import get_settings
from src.core.graph.neo4j import Neo4jGraphRepository
from src.core.graph.service import GraphService
from src.core.graph.structured import (
    AssetAggregateGroup,
    AssetAggregateRequest,
    AssetSearchFilters,
    AssetSearchRequest,
)


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def single(self):
        return self.rows[0] if self.rows else None


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: Any, **params: object) -> _Result:
        cypher = getattr(query, "text", str(query))
        self.calls.append((cypher, params))
        if "RETURN m.active_graph_version AS version" in cypher:
            return _Result([{"version": "active-v2"}])
        if "matched_total" in cypher:
            return _Result([{"matched_total": 1}])
        if "RETURN count(a) AS count" in cypher:
            return _Result([{"count": 1}])
        if "WITH a.status AS value" in cypher:
            return _Result([
                {
                    "value": "CONFIRMED",
                    "count": 2,
                    "member_ips": ["192.0.2.10", "192.0.2.11"],
                }
            ])
        return _Result(
            [
                {
                    "graph_key": "192.0.2.10",
                    "graph_version": "active-v2",
                    "ip": "192.0.2.10",
                    "asset_name": "DC-01",
                    "status": "CONFIRMED",
                    "suggested_type": "Domain Controller",
                    "model_confidence": 0.65,
                    "mapping_confidence": 0.8,
                    "unknown_score": 0.2,
                    "classification_summary": "fixture",
                    "vendor": "VMware",
                    "product": "Active Directory",
                    "role": "Domain Controller",
                    "roles": ["Domain Controller", "LDAP Server"],
                    "tag": "identity",
                    "sub_tag": "directory",
                    "last_detection_at": "2026-09-10T08:00:00+00:00",
                    "enrichment_status": "success",
                    "enrichment_updated_at": "2026-09-10T08:00:00+00:00",
                    "enrichment_last_attempt_at": "2026-09-10T08:00:00+00:00",
                    "enrichment_last_success_at": "2026-09-10T08:00:00+00:00",
                    "enrichment_next_due_at": "2026-09-13T08:00:00+00:00",
                    "enrichment_source": "product_asset_detection_overview",
                    "enrichment_version": "hash",
                    "_sort_null": 0,
                    "_sort_value": "192.0.2.10",
                }
            ]
        )


class _Driver:
    def __init__(self) -> None:
        self.session_instance = _Session()

    @contextmanager
    def session(self):
        yield self.session_instance


def _repository(*, default_limit: int = 2, max_limit: int = 3) -> Neo4jGraphRepository:
    settings = replace(
        get_settings(),
        graph_asset_search_default_limit=default_limit,
        graph_asset_search_max_limit=max_limit,
    )
    return Neo4jGraphRepository(_Driver(), settings)  # type: ignore[arg-type]


def test_filter_contract_normalizes_and_rejects_unknown_fields_and_operators() -> None:
    filters = AssetSearchFilters(
        ip=" 192.0.2.10 ",
        role=" Domain Controller ",
        model_confidence_max=0.7,
        last_detection_at_from="2026-09-10T08:00:00+03:30",
    )
    assert filters.ip == "192.0.2.10"
    assert filters.role == "Domain Controller"
    assert filters.last_detection_at_from == datetime.fromisoformat(
        "2026-09-10T04:30:00+00:00"
    )
    with pytest.raises(ValidationError):
        AssetSearchFilters.model_validate({"role__contains": "Controller"})
    with pytest.raises(ValidationError):
        AssetSearchFilters.model_validate({"cypher": "MATCH (n) RETURN n"})
    with pytest.raises(ValidationError):
        AssetSearchFilters(model_confidence_min=0.8, model_confidence_max=0.7)
    with pytest.raises(ValidationError):
        AssetSearchFilters(last_detection_at_from="2026-09-10T08:00:00")


def test_repository_uses_allowlisted_parameterized_filters_and_active_version() -> None:
    repository = _repository()
    injected = "Domain Controller' OR true //"
    result = repository.search_assets(
        AssetSearchRequest(
            filters=AssetSearchFilters(
                role=injected,
                product="Active Directory",
                roles="LDAP Server",
                model_confidence_max=0.7,
                unknown_score_min=0.1,
            ),
            limit=2,
        )
    )
    assert result.active_graph_version == "active-v2"
    assert result.returned_count == 1
    assert result.rows[0].roles == ("Domain Controller", "LDAP Server")
    calls = repository.driver.session_instance.calls  # type: ignore[attr-defined]
    search_query, search_params = next(
        (query, params) for query, params in calls if "RETURN a.graph_key AS graph_key" in query
    )
    assert injected not in search_query
    assert search_params["role"] == injected
    assert "a.graph_version = $active_version" in search_query
    assert "$roles IN coalesce(a.roles, [])" in search_query
    assert "SKIP" not in search_query.upper()


def test_cursor_is_opaque_validated_and_bound_to_sort_contract() -> None:
    repository = _repository(default_limit=1, max_limit=2)
    session = repository.driver.session_instance  # type: ignore[attr-defined]
    original_run = session.run

    def two_rows(query: Any, **params: object) -> _Result:
        result = original_run(query, **params)
        cypher = getattr(query, "text", str(query))
        if "RETURN a.graph_key AS graph_key" in cypher:
            return _Result([result.rows[0], {**result.rows[0], "graph_key": "192.0.2.11", "ip": "192.0.2.11", "_sort_value": "192.0.2.11"}])
        return result

    session.run = two_rows  # type: ignore[method-assign]
    first = repository.search_assets(AssetSearchRequest(limit=1))
    assert first.truncated is True
    assert first.next_cursor and "192.0.2.10" not in first.next_cursor
    second = repository.search_assets(AssetSearchRequest(limit=1, cursor=first.next_cursor))
    assert second.returned_count == 1
    with pytest.raises(ValueError, match="does not match"):
        repository.search_assets(
            AssetSearchRequest(limit=1, sort="asset_name", cursor=first.next_cursor)
        )
    with pytest.raises(ValueError, match="Invalid"):
        repository.search_assets(AssetSearchRequest(limit=1, cursor="not-base64!"))


def test_policy_bounds_service_and_repository_limits() -> None:
    repository = _repository(default_limit=2, max_limit=3)
    service = GraphService(repository.settings)
    service.repository = repository
    assert service.search_assets(AssetSearchRequest()).returned_count == 1
    with pytest.raises(ValueError, match="between 1 and 3"):
        service.search_assets(AssetSearchRequest(limit=4))
    with pytest.raises(ValueError, match="between 1 and 3"):
        repository.search_assets(AssetSearchRequest(limit=4))


def test_aggregate_contract_counts_in_cypher_and_bounds_group_count() -> None:
    repository = _repository()
    count = repository.aggregate_assets(
        AssetAggregateRequest(filters=AssetSearchFilters(role="Domain Controller"))
    )
    grouped = repository.aggregate_assets(
        AssetAggregateRequest(operation="group_count", group_by="status", limit=2)
    )
    assert count.count == 1
    assert grouped.count == 1
    assert [(group.value, group.count) for group in grouped.groups] == [("CONFIRMED", 2)]
    assert grouped.groups[0].member_ips == ("192.0.2.10", "192.0.2.11")
    assert grouped.groups[0].member_ips_truncated is False
    queries = [query for query, _ in repository.driver.session_instance.calls]  # type: ignore[attr-defined]
    assert any("RETURN count(a) AS count" in query for query in queries)
    assert any("collect(member_ip)[0..$member_limit] AS member_ips" in query for query in queries)
    aggregate_call = next(
        params
        for query, params in repository.driver.session_instance.calls  # type: ignore[attr-defined]
        if "collect(member_ip)" in query
    )
    assert aggregate_call["member_limit"] == 20
    with pytest.raises(ValidationError):
        AssetAggregateRequest(operation="group_count")
    with pytest.raises(ValidationError):
        AssetAggregateRequest(operation="count", group_by="vendor")


def test_aggregate_member_identity_contract_is_canonical_and_strictly_bounded() -> None:
    group = AssetAggregateGroup(
        value="server",
        count=30,
        member_ips=[" 192.0.2.2 ", "192.0.2.1"],
        member_ips_truncated=True,
    )
    assert group.member_ips == ("192.0.2.2", "192.0.2.1")
    assert group.member_ips_truncated is True

    with pytest.raises(ValidationError, match="bounded cap"):
        AssetAggregateGroup(
            value="server",
            count=21,
            member_ips=[f"192.0.2.{index}" for index in range(1, 22)],
        )
    with pytest.raises(ValidationError, match="invalid"):
        AssetAggregateGroup(value="server", count=1, member_ips=["not-an-ip"])
