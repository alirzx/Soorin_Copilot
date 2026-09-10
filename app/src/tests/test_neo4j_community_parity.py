"""Live Community parity gate; opt in with SOORIN_RUN_NEO4J_INTEGRATION=1.

The fixture is deliberately independent of any graph runtime cache. Product
records are the input for the Neo4j projection and a bounded adjacency oracle.
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.config.settings import get_settings
from src.core.graph.neo4j import Neo4jDriver, Neo4jGraphRepository, _ProjectionAdjacency
from src.core.graph.enrichment_models import AssetEnrichmentMutation
from src.core.graph.retrieval import GraphRetrievalSpec, _apply_completeness_contract, _two_hop_context
from src.core.graph.neo4j import GraphSyncValidationError
from src.core.context.models import ResolvedEntity
from src.core.product_client import ProductAssetDetectionOverview
from src.core.product_client.schemas import TopologyConnectionRecord


pytestmark = pytest.mark.skipif(
    os.getenv("SOORIN_RUN_NEO4J_INTEGRATION") != "1",
    reason="set SOORIN_RUN_NEO4J_INTEGRATION=1 to run against the local Community database",
)


# These are durable contract fixtures for the backend-neutral retrieval contract.
EXPECTED_TWO_HOP_NODE_IDS = {
    "inbound": ["10.0.0.1", "10.0.0.2", "10.0.7.10", "10.0.1.10", "10.0.2.10", "10.0.3.10"],
    "outbound": ["10.0.0.1", "10.0.0.2", "10.0.1.10", "10.0.2.10", "10.0.6.10", "10.0.4.10", "10.0.5.10", "10.0.7.10"],
    "both": ["10.0.0.1", "10.0.0.2", "10.0.1.10", "10.0.2.10", "10.0.6.10", "10.0.7.10", "10.0.3.10", "10.0.4.10", "10.0.5.10"],
}
EXPECTED_TWO_HOP_EDGE_COUNTS = {"inbound": 6, "outbound": 10, "both": 13}


def _records() -> list[TopologyConnectionRecord]:
    # a/b cover direct, reverse, bidirectional, shared and unique peers. hub
    # has a supernode-like fan-out; the chain supplies two-hop and path cases.
    pairs = [
        ("10.0.0.1", "10.0.0.2"), ("10.0.0.2", "10.0.0.1"),
        ("10.0.0.1", "10.0.1.10"), ("10.0.0.2", "10.0.1.10"),
        ("10.0.0.1", "10.0.2.10"), ("10.0.3.10", "10.0.0.2"),
        ("10.0.0.2", "10.0.4.10"), ("10.0.4.10", "10.0.5.10"),
        ("10.0.0.1", "10.0.6.10"), ("10.0.6.10", "10.0.5.10"),
        ("10.0.1.10", "10.0.7.10"), ("10.0.2.10", "10.0.7.10"),
        ("10.0.7.10", "10.0.0.1"), ("10.0.7.10", "10.0.6.10"),
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
def oracle() -> _ProjectionAdjacency:
    records = _records()
    return _ProjectionAdjacency(
        sorted({ip for record in records for ip in (record.src_ip, record.dst_ip)}),
        [{"source": record.src_ip, "target": record.dst_ip, "weight": record.weight} for record in records],
    )


def _peers(graph: _ProjectionAdjacency, node: str) -> set[str]:
    return set(graph.predecessors(node)).union(graph.successors(node)) if node in graph else set()


def _two_hop_spec(direction: str) -> GraphRetrievalSpec:
    return GraphRetrievalSpec(
        scope="two_hop",
        direction=direction,  # type: ignore[arg-type]
        depth=2,
        entities=[ResolvedEntity(type="ip", value="10.0.0.1", source="fixture")],
    )


def test_community_identity_and_summary_parity(repository: Neo4jGraphRepository, oracle: _ProjectionAdjacency) -> None:
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


def test_neighbors_relationship_and_path_parity(repository: Neo4jGraphRepository, oracle: _ProjectionAdjacency) -> None:
    neighbors = repository.get_neighbors("10.0.0.1", "both", limit=1000)
    assert neighbors["total"] == oracle.in_degree("10.0.0.1") + oracle.out_degree("10.0.0.1")
    assert [row["ip"] for row in neighbors["neighbors"]] == ["10.0.0.2", "10.0.0.2", "10.0.1.10", "10.0.2.10", "10.0.6.10", "10.0.7.10"]
    assert repository.get_relationship("10.0.0.1", "10.0.0.2")["relationship"] == "bidirectional_direct_relationship"
    assert repository.get_relationship("10.0.0.1", "10.0.5.10")["relationship"] == "no_direct_relationship"
    assert repository.get_relationship("10.0.0.1", "10.99.99.99")["relationship"] == "entity_missing_from_active_graph"
    path = repository.find_path("10.0.0.1", "10.0.5.10", max_hops=3)
    assert path["path"] == ["10.0.0.1", "10.0.6.10", "10.0.5.10"]
    assert repository.find_path("10.0.5.10", "10.0.0.1", max_hops=3)["found"] is False


def test_comparison_limits_active_version_and_idempotency(repository: Neo4jGraphRepository, oracle: _ProjectionAdjacency) -> None:
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
    with pytest.raises(GraphSyncValidationError):
        repository.sync_snapshot([], "community-parity-v3")
    assert repository.status().active_graph_version == "community-parity-v1"
    assert repository.get_context(_two_hop_spec("both"))["node_found"] is True


def test_supernode_bounding_and_two_hop_oracle_fixture(repository: Neo4jGraphRepository, oracle: _ProjectionAdjacency) -> None:
    """Prove the fixture contains the expansion cases used by the next cutover gate."""
    hub = "10.0.8.1"
    bounded = repository.get_neighbors(hub, "out", limit=7)
    assert oracle.out_degree(hub) == 130
    assert bounded["total"] == 130
    assert bounded["returned"] == 7
    assert bounded["truncated"] is True
    assert set(oracle.successors("10.0.0.1")).intersection(oracle.predecessors("10.0.5.10")) == {"10.0.6.10"}


@pytest.mark.parametrize("direction", ["inbound", "outbound", "both"])
def test_two_hop_context_matches_adjacency_contract_for_every_direction(
    repository: Neo4jGraphRepository, oracle: _ProjectionAdjacency, direction: str
) -> None:
    spec = _two_hop_spec(direction)
    expected = _apply_completeness_contract(_two_hop_context(oracle, spec, repository.settings), spec)
    actual = repository.get_context(spec)
    assert actual == expected
    assert [node["id"] for node in actual["nodes"]] == EXPECTED_TWO_HOP_NODE_IDS[direction]
    assert actual["retrieved_edge_count"] == EXPECTED_TWO_HOP_EDGE_COUNTS[direction]


def test_two_hop_caps_and_completeness_match_adjacency_contract(repository: Neo4jGraphRepository, oracle: _ProjectionAdjacency) -> None:
    settings = replace(repository.settings, graph_two_hop_max_nodes=4, graph_max_edges=3)
    limited = Neo4jGraphRepository(repository.driver, settings)
    spec = _two_hop_spec("both")
    expected = _apply_completeness_contract(_two_hop_context(oracle, spec, settings), spec)
    actual = limited.get_context(spec)
    assert actual == expected
    assert actual["retrieval_truncated"] is True
    assert actual["retrieval_complete"] is False
    assert actual["retrieval_truncation_reason"] == "node_limit:4"


def test_comparison_context_matches_adjacency_contract_and_active_projection(repository: Neo4jGraphRepository, oracle: _ProjectionAdjacency) -> None:
    spec = GraphRetrievalSpec(
        scope="multi_entity_comparison",
        direction="both",
        depth=1,
        entities=[
            ResolvedEntity(type="ip", value="10.0.0.1", source="fixture"),
            ResolvedEntity(type="ip", value="10.0.0.2", source="fixture"),
        ],
        intent="graph_relationships",
        relationship_mode="compare",
    )
    from src.core.graph.retrieval import _comparison_context

    expected = _apply_completeness_contract(_comparison_context(oracle, spec, repository.settings), spec)
    actual = repository.get_context(spec)
    assert actual == expected
    assert actual["entities"] == ["10.0.0.1", "10.0.0.2"]
    assert actual["retrieval_complete"] is True


def test_comparison_peer_caps_match_adjacency_contract(repository: Neo4jGraphRepository, oracle: _ProjectionAdjacency) -> None:
    settings = replace(
        repository.settings,
        graph_comparison_max_peers_per_entity=1,
        graph_comparison_max_shared_peers=1,
    )
    limited = Neo4jGraphRepository(repository.driver, settings)
    spec = GraphRetrievalSpec(
        scope="multi_entity_comparison",
        direction="both",
        depth=1,
        entities=[
            ResolvedEntity(type="ip", value="10.0.0.1", source="fixture"),
            ResolvedEntity(type="ip", value="10.0.0.2", source="fixture"),
        ],
        intent="graph_relationships",
        relationship_mode="compare",
    )
    from src.core.graph.retrieval import _comparison_context

    expected = _apply_completeness_contract(_comparison_context(oracle, spec, settings), spec)
    actual = limited.get_context(spec)
    assert actual == expected
    assert actual["retrieval_truncated"] is True
    assert actual["retrieval_complete"] is False
    assert actual["retrieval_truncation_reason"] == "comparison_peer_limit:1"


def test_enrichment_lkg_idempotency_and_topology_version_carry_forward(
    repository: Neo4jGraphRepository,
) -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
    overview = ProductAssetDetectionOverview.from_payload(
        {
            "assetName": "SOORIN-DC",
            "ip": "10.0.0.1",
            "status": "CONFIRMED",
            "suggestedType": "Domain Controller",
            "modelConfidence": 0.99,
            "mappingConfidence": 0.98,
            "unknownScore": 0.01,
            "classificationSummary": "Validated Community enrichment fixture.",
            "vendor": "VMware",
            "product": "Active Directory",
            "role": "Domain Controller",
            "roles": ["Domain Controller", "DNS Server"],
            "tag": "services",
            "subTag": "Active Directory",
            "lastDetectionAt": "2026-09-09T13:04:41.835Z",
        },
        expected_ip="10.0.0.1",
    )
    success = AssetEnrichmentMutation.success(
        overview, attempted_at=now, freshness_seconds=259200
    )

    with repository.driver.session() as session:
        session.run(
            "MATCH (m:GraphMetadata {id: 'active'}) "
            "MATCH (a:Asset {graph_version: m.active_graph_version, graph_key: $key}) "
            "SET a.unrelated_marker = 'keep'",
            key="10.0.0.1",
        ).consume()
    first = repository.apply_enrichment_batch([success])
    second = repository.apply_enrichment_batch([success])
    assert first == second
    assert first.updated == 1

    with repository.driver.session() as session:
        before_failure = session.run(
            "MATCH (m:GraphMetadata {id: 'active'}) "
            "MATCH (a:Asset {graph_version: m.active_graph_version, graph_key: $key}) "
            "OPTIONAL MATCH (a)-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->() "
            "RETURN properties(a) AS properties, count(r) AS outgoing",
            key="10.0.0.1",
        ).single()
    assert before_failure["properties"]["role"] == "Domain Controller"
    assert before_failure["properties"]["roles"] == ["Domain Controller", "DNS Server"]
    assert before_failure["properties"]["unrelated_marker"] == "keep"
    assert before_failure["outgoing"] > 0

    failure = AssetEnrichmentMutation.failure(
        "10.0.0.1",
        attempted_at=now,
        retry_seconds=3600,
        unavailable=False,
        error="Product timeout after bounded retries",
    )
    repository.apply_enrichment_batch([failure])
    with repository.driver.session() as session:
        after_failure = session.run(
            "MATCH (m:GraphMetadata {id: 'active'}) "
            "MATCH (a:Asset {graph_version: m.active_graph_version, graph_key: $key}) "
            "RETURN properties(a) AS properties",
            key="10.0.0.1",
        ).single()["properties"]
    assert after_failure["role"] == "Domain Controller"
    assert after_failure["vendor"] == "VMware"
    assert after_failure["enrichment_status"] == "stale"
    assert after_failure["enrichment_last_success_at"] == success.succeeded_at

    due_after_retry = repository.list_due_enrichment_assets(
        after_graph_key=None,
        limit=repository.settings.graph_enrichment_page_size,
        as_of=now.replace(hour=10),
        stale_before=now,
    )
    assert "10.0.0.1" in due_after_retry.graph_keys

    version_two_records = [
        *_records(),
        TopologyConnectionRecord("10.77.0.1", "10.77.0.2"),
    ]
    repository.sync_snapshot(version_two_records, "community-enrichment-v2")
    with repository.driver.session() as session:
        carried = session.run(
            "MATCH (m:GraphMetadata {id: 'active'}) "
            "MATCH (a:Asset {graph_version: m.active_graph_version, graph_key: $key}) "
            "OPTIONAL MATCH (a)-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->() "
            "RETURN m.active_graph_version AS version, properties(a) AS properties, "
            "count(r) AS outgoing",
            key="10.0.0.1",
        ).single()
        old_nodes = session.run(
            "MATCH (a:Asset {graph_version: 'community-parity-v1'}) RETURN count(a) AS count"
        ).single()["count"]
    assert carried["version"] == "community-enrichment-v2"
    assert carried["properties"]["role"] == "Domain Controller"
    assert carried["properties"]["roles"] == ["Domain Controller", "DNS Server"]
    assert carried["properties"]["enrichment_last_success_at"] == success.succeeded_at
    assert carried["outgoing"] == before_failure["outgoing"]
    assert old_nodes == 0
    status = repository.status()
    assert status.nodes == len(
        {ip for record in version_two_records for ip in (record.src_ip, record.dst_ip)}
    )
    assert status.edges == len(version_two_records)
    eligible = repository.list_due_enrichment_assets(
        after_graph_key=None, limit=500, as_of=now, stale_before=now
    )
    assert {"10.77.0.1", "10.77.0.2"}.issubset(eligible.graph_keys)

    missing = repository.apply_enrichment_batch(
        [replace(success, graph_key="10.99.99.99")]
    )
    assert missing.updated == 0
    assert missing.missing_graph_keys == ("10.99.99.99",)
