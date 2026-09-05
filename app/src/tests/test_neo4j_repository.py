"""Offline contract tests for the bounded Neo4j graph repository."""
from __future__ import annotations

from contextlib import contextmanager

from src.config.settings import get_settings
from src.core.graph.neo4j import GraphQueryPolicy, Neo4jGraphRepository


class _Result:
    def __init__(self, row): self.row = row
    def single(self): return self.row
    def consume(self): return None


class _Session:
    def __init__(self, rows): self.rows = iter(rows); self.calls = []
    def run(self, query, **params):
        self.calls.append((getattr(query, "text", query), params))
        return _Result(next(self.rows))


class _Driver:
    def __init__(self, rows): self.session_value = _Session(rows)
    @contextmanager
    def session(self): yield self.session_value


def _repository(*rows):
    settings = get_settings()
    driver = _Driver(rows)
    return Neo4jGraphRepository(driver, settings), driver


def test_policy_reuses_existing_graph_limits() -> None:
    settings = get_settings()
    policy = GraphQueryPolicy.from_settings(settings)
    assert policy.max_hops == settings.graph_max_path_length
    assert policy.max_neighbors == settings.graph_api_max_neighbors
    assert policy.full_neighbors_hard_max == settings.graph_full_neighbors_hard_max


def test_relationship_preserves_direction_and_active_projection_filter() -> None:
    repository, driver = _repository({"source_present": True, "target_present": True, "forward_edge": True, "reverse_edge": False})
    result = repository.get_relationship("10.0.0.1", "10.0.0.2")
    query, params = driver.session_value.calls[0]
    assert result["relationship"] == "forward_direct_relationship"
    assert params == {"source": "10.0.0.1", "target": "10.0.0.2"}
    assert "GraphMetadata {id: 'active'}" in query
    assert "graph_version: m.active_graph_version" in query
    assert "$source" in query and "$target" in query


def test_path_uses_controlled_relationship_and_static_bound() -> None:
    repository, driver = _repository({"source_present": True, "target_present": True, "nodes": ["10.0.0.1", "10.0.0.2"]})
    result = repository.find_path("10.0.0.1", "10.0.0.2", max_hops=2)
    query, params = driver.session_value.calls[0]
    assert result["found"] is True
    assert result["edge_count"] == 1
    assert "COMMUNICATES_WITH*1..2" in query
    assert "[*]" not in query
    assert params == {"source": "10.0.0.1", "target": "10.0.0.2"}


def test_neighbors_are_sorted_and_bounded_by_query_policy() -> None:
    repository, driver = _repository({"found": True, "total": 3, "rows": [
        {"ip": "10.0.0.2", "direction": "out", "edge_weight": 3},
        {"ip": "10.0.0.3", "direction": "in", "edge_weight": 1},
    ]})
    result = repository.get_neighbors("10.0.0.1", "both", limit=2)
    query, params = driver.session_value.calls[0]
    assert result["returned"] == 2 and result["truncated"] is True
    assert "ORDER BY item.edge_weight DESC, item.ip ASC, item.direction ASC" in query
    assert params["limit"] == 2 and params["direction"] == "both"
