"""Focused Task-1A contracts for Neo4j-backed graph service and routes."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.graph_routes import graph_context, graph_neighbors, graph_node, graph_path, graph_stats, graph_status, graph_topology
from src.core.graph.neo4j import GraphProjectionStatus
from src.core.graph.service import GraphService, GraphStatus


class _GraphServiceFixture:
    def status(self) -> GraphStatus:
        return GraphStatus(
            loaded=True, nodes=3, edges=2, directed=True, artifact_available=False,
            active_graph_loaded_at="2026-09-05T10:00:00+00:00",
            active_graph_source="neo4j_projection", active_graph_version="v1",
            last_known_good=True,
        )

    def stats(self):
        return {
            "total_nodes": 3, "total_edges": 2, "avg_degree": 1.3,
            "top_destinations": [{"ip": "10.0.0.2", "incoming": 2}],
            "top_sources": [{"ip": "10.0.0.1", "outgoing": 2}],
            "ip_range_distribution": {"10.x.x.x": 3},
        }

    def node(self, ip: str):
        return {"ip": ip, "found": ip == "10.0.0.1", "degree": {"in": 1, "out": 1, "total": 2} if ip == "10.0.0.1" else {"in": 0, "out": 0, "total": 0}}

    def neighbors(self, ip: str, *, direction: str, limit: int):
        return {"target_ip": ip, "found": True, "direction": direction, "total": 1, "returned": 1, "neighbors": [{"ip": "10.0.0.2", "direction": "out", "edge_weight": 2}]}

    def context(self, spec):
        assert spec.entities[0].value == "10.0.0.1"
        return {"node_found": True, "in_degree": 1, "out_degree": 1, "degree": 2, "top_inbound_peers": ["10.0.0.2"], "top_outbound_peers": ["10.0.0.2"], "bidirectional_peers": ["10.0.0.2"], "subnets_reached": ["10.0.0.0/24"], "limitations": []}

    def path(self, source: str, target: str):
        return {"source": source, "target": target, "found": True, "path": [source, target], "edge_count": 1, "semantics": "Observed communication-graph path, not proof of routed network path."}

    def topology(self, *, max_nodes, min_degree, subnet):
        return {"nodes": [{"ip": "10.0.0.1", "degree": 2}], "edges": [{"source": "10.0.0.1", "target": "10.0.0.2", "weight": 2}], "max_nodes": 10 if max_nodes is None else max_nodes, "min_degree": 1 if min_degree is None else min_degree, "subnet": subnet}


def test_graph_routes_preserve_contracts_and_route_through_service() -> None:
    service = _GraphServiceFixture()
    status = graph_status(service=service)
    stats = graph_stats(service=service)
    node = graph_node("10.0.0.1", service=service)
    neighbors = graph_neighbors("10.0.0.1", direction="out", limit=1, service=service)
    context = graph_context("10.0.0.1", service=service)
    path = graph_path(source="10.0.0.1", target="10.0.0.2", service=service)
    topology = graph_topology(max_nodes=10, min_degree=1, subnet="10.", service=service)

    assert status.active_graph_version == "v1"
    assert status.artifact_available is False
    assert stats.total_nodes == 3
    assert node.found is True
    assert graph_node("10.0.0.9", service=service).found is False
    assert neighbors.returned == 1
    assert context.node_found is True
    assert path.found is True
    assert topology.max_nodes == 10


def test_graph_routes_reject_invalid_ips_and_invalid_bounds() -> None:
    service = _GraphServiceFixture()
    with pytest.raises(HTTPException, match="Invalid ip"):
        graph_node("not-an-ip", service=service)
    with pytest.raises(HTTPException, match="Invalid source"):
        graph_path(source="not-an-ip", target="10.0.0.2", service=service)


def test_graph_service_operations_delegate_to_neo4j_repository(monkeypatch) -> None:
    class _Repository:
        def stats(self): return {"total_nodes": 0, "total_edges": 0, "avg_degree": 0, "top_destinations": [], "top_sources": [], "ip_range_distribution": {}}
        def get_context(self, spec): return {"scope": spec.scope}
        def status(self): return GraphProjectionStatus("published-v1", 3, 2, "2026-09-05T10:00:00+00:00")
        def get_relationship(self, source, target): return {"source": source, "target": target}
        def compare_assets(self, left, right): return {"entities": [left, right]}
        def topology(self, **kwargs): return kwargs

    service = GraphService()
    service.repository = _Repository()
    assert service.stats()["total_nodes"] == 0
    assert service.context(type("Spec", (), {"scope": "node_summary"})())["active_graph_version"] == "published-v1"
    assert service.relationship(" 10.0.0.1 ", " 10.0.0.2 ")["source"] == "10.0.0.1"
    assert service.comparison(" 10.0.0.1 ", " 10.0.0.2 ")["entities"] == ["10.0.0.1", "10.0.0.2"]
    assert service.topology(max_nodes=9, min_degree=2, subnet=" 10. ") == {"max_nodes": 9, "min_degree": 2, "subnet": "10."}


def test_status_only_advertises_the_published_neo4j_projection(monkeypatch) -> None:
    class _Repository:
        def status(self):
            return GraphProjectionStatus("published-v1", 3, 2, "2026-09-05T10:00:00+00:00")

    service = GraphService()
    service.repository = _Repository()
    monkeypatch.setattr(
        "src.core.graph.service.get_refresh_status",
        lambda: {"active_graph_version": "staging-v2", "active_graph_source": "staging", "interval_seconds": 3600},
    )

    status = service.status()

    assert status.active_graph_version == "published-v1"
    assert status.active_graph_source == "neo4j_projection"
    assert status.nodes == 3 and status.edges == 2
