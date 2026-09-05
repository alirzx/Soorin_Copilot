"""Live Community parity gate; opt in with SOORIN_RUN_NEO4J_INTEGRATION=1.

The fixture is deliberately independent of the NetworkX runtime cache.  Product
records are the input for both the NetworkX oracle and the Neo4j projection.
"""
from __future__ import annotations

import os

import networkx as nx
import pytest

from src.config.settings import get_settings
from src.core.graph.neo4j import Neo4jDriver, Neo4jGraphRepository
from src.core.product_client.schemas import TopologyConnectionRecord


pytestmark = pytest.mark.skipif(
    os.getenv("SOORIN_RUN_NEO4J_INTEGRATION") != "1",
    reason="set SOORIN_RUN_NEO4J_INTEGRATION=1 to run against the local Community database",
)


def _records() -> list[TopologyConnectionRecord]:
    # a/b cover direct, reverse, bidirectional, shared and unique peers. hub
    # has a supernode-like fan-out; the chain supplies two-hop and path cases.
    pairs = [
        ("10.0.0.1", "10.0.0.2"), ("10.0.0.2", "10.0.0.1"),
        ("10.0.0.1", "10.0.1.10"), ("10.0.0.2", "10.0.1.10"),
        ("10.0.0.1", "10.0.2.10"), ("10.0.3.10", "10.0.0.2"),
        ("10.0.0.2", "10.0.4.10"), ("10.0.4.10", "10.0.5.10"),
        ("10.0.0.1", "10.0.6.10"), ("10.0.6.10", "10.0.5.10"),
        ("10.0.9.1", "10.0.9.2"),
    ]
    pairs.extend(("10.0.8.1", f"10.1.{index // 250}.{index % 250 + 1}") for index in range(130))
    return [TopologyConnectionRecord(source, target) for source, target in pairs]


@pytest.fixture(scope="module")
def repository() -> Neo4jGraphRepository:
    settings = get_settings()
    driver = Neo4jDriver(settings)
    repository = Neo4jGraphRepository(driver, settings)
    repository.bootstrap_schema()
    repository.sync_snapshot(_records(), "community-parity-v1")
    yield repository
    driver.close()


@pytest.fixture(scope="module")
def oracle() -> nx.DiGraph:
    graph = nx.DiGraph()
    graph.add_weighted_edges_from((record.src_ip, record.dst_ip, record.weight) for record in _records())
    return graph


def _peers(graph: nx.DiGraph, node: str) -> set[str]:
    return set(graph.predecessors(node)).union(graph.successors(node)) if node in graph else set()


def test_community_identity_and_summary_parity(repository: Neo4jGraphRepository, oracle: nx.DiGraph) -> None:
    with repository.driver.session() as session:
        metadata = session.run(
            "CALL dbms.components() YIELD name, versions, edition "
            "WHERE name = 'Neo4j Kernel' RETURN versions[0] AS version, edition"
        ).single()
    assert metadata["version"] == "2026.07.1"
    assert metadata["edition"] == "community"
    summary = repository.get_summary("10.0.0.1")
    assert summary == {
        "ip": "10.0.0.1", "found": True, "inbound_total": oracle.in_degree("10.0.0.1"),
        "outbound_total": oracle.out_degree("10.0.0.1"), "bidirectional_total": 1,
        "in_degree": oracle.in_degree("10.0.0.1"), "out_degree": oracle.out_degree("10.0.0.1"),
        "degree": oracle.degree("10.0.0.1"),
    }
    assert repository.get_summary("10.99.99.99")["found"] is False


def test_neighbors_relationship_and_path_parity(repository: Neo4jGraphRepository, oracle: nx.DiGraph) -> None:
    neighbors = repository.get_neighbors("10.0.0.1", "both", limit=1000)
    assert neighbors["total"] == oracle.in_degree("10.0.0.1") + oracle.out_degree("10.0.0.1")
    assert [row["ip"] for row in neighbors["neighbors"]] == ["10.0.0.2", "10.0.0.2", "10.0.1.10", "10.0.2.10", "10.0.6.10"]
    assert repository.get_relationship("10.0.0.1", "10.0.0.2")["relationship"] == "bidirectional_direct_relationship"
    assert repository.get_relationship("10.0.0.1", "10.0.5.10")["relationship"] == "no_direct_relationship"
    assert repository.get_relationship("10.0.0.1", "10.99.99.99")["relationship"] == "entity_missing_from_active_graph"
    path = repository.find_path("10.0.0.1", "10.0.5.10", max_hops=3)
    assert path["path"] == nx.shortest_path(oracle, "10.0.0.1", "10.0.5.10")
    assert repository.find_path("10.0.5.10", "10.0.0.1", max_hops=3)["found"] is False


def test_comparison_limits_active_version_and_idempotency(repository: Neo4jGraphRepository, oracle: nx.DiGraph) -> None:
    result = repository.compare_assets("10.0.0.1", "10.0.0.2")
    a_peers, b_peers = _peers(oracle, "10.0.0.1"), _peers(oracle, "10.0.0.2")
    assert result["shared_peers_retrieved"] == sorted(a_peers.intersection(b_peers))
    assert result["shared_peer_total"] == len(a_peers.intersection(b_peers))
    assert result["entity_a_unique_peer_total"] == len(a_peers.difference(b_peers))
    assert result["entity_b_unique_peer_total"] == len(b_peers.difference(a_peers))
    assert result["direct_relationship"] == {
        "a_to_b": True,
        "b_to_a": True,
        "relationship": "bidirectional_direct_relationship",
        "relationship_status": "bidirectional_direct_relationship",
        "bidirectional": True,
    }
    # Repeating the same version is idempotent and an unpublished V2 remains invisible.
    first = repository.status()
    second = repository.sync_snapshot(_records(), "community-parity-v1")
    assert (first.nodes, first.edges) == (second.nodes, second.edges)
    repository._write_staging_nodes(["10.99.99.1"], "community-parity-v2")  # noqa: SLF001 - active-version gate.
    assert repository.get_summary("10.99.99.1")["found"] is False


def test_supernode_bounding_and_two_hop_oracle_fixture(repository: Neo4jGraphRepository, oracle: nx.DiGraph) -> None:
    """Prove the fixture contains the expansion cases used by the next cutover gate."""
    hub = "10.0.8.1"
    bounded = repository.get_neighbors(hub, "out", limit=7)
    assert oracle.out_degree(hub) == 130
    assert bounded["total"] == 130
    assert bounded["returned"] == 7
    assert bounded["truncated"] is True
    assert set(oracle.successors("10.0.0.1")).intersection(oracle.predecessors("10.0.5.10")) == {"10.0.6.10"}
