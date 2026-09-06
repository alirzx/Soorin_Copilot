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


def test_stats_are_active_projection_only_and_preserve_legacy_shape() -> None:
    repository, driver = _repository({
        "total_nodes": 4,
        "total_edges": 3,
        "ips": ["10.0.0.1", "192.168.1.4", "172.20.0.8", "198.51.100.1"],
        "top_destinations": [{"ip": "10.0.0.1", "incoming": 2}],
        "top_sources": [{"ip": "192.168.1.4", "outgoing": 2}],
    })

    result = repository.stats()
    query, params = driver.session_value.calls[0]

    assert params == {}
    assert "GraphMetadata {id: 'active'}" in query
    assert "graph_version: m.active_graph_version" in query
    assert result == {
        "total_nodes": 4,
        "total_edges": 3,
        "avg_degree": 1.5,
        "top_destinations": [{"ip": "10.0.0.1", "incoming": 2}],
        "top_sources": [{"ip": "192.168.1.4", "outgoing": 2}],
        "ip_range_distribution": {
            "10.x.x.x": 1,
            "192.168.x.x": 1,
            "172.16-31.x.x": 1,
            "Other": 1,
        },
    }


def test_topology_is_bounded_by_settings_and_query_policy() -> None:
    repository, driver = _repository({
        "nodes": [{"ip": "10.0.0.1", "degree": 2}],
        "edges": [{"source": "10.0.0.1", "target": "10.0.0.2", "weight": 3}],
    })

    result = repository.topology(max_nodes=999_999, min_degree=-2, subnet="10.")
    query, params = driver.session_value.calls[0]

    assert "LIMIT $limit" in query
    assert "[0..$edge_limit]" in query
    assert params["limit"] == repository.settings.graph_max_ui_nodes
    assert params["edge_limit"] == repository.policy.max_edges
    assert params["min_degree"] == 0
    assert params["subnet"] == "10."
    assert result["nodes"] == [{"ip": "10.0.0.1", "degree": 2}]
    assert result["max_nodes"] == repository.settings.graph_max_ui_nodes
