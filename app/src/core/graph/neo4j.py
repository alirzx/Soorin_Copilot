"""Neo4j persistence boundary for the observed topology projection.

Product remains authoritative.  Each successful full snapshot is written with
an unpublished version and becomes readable only when GraphMetadata points at
that version.  This keeps the prior graph readable if a refresh fails.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Iterator

from src.config.settings import Settings
from src.core.graph.enrichment_models import (
    AssetEnrichmentMutation,
    AssetEnrichmentEligibility,
    EnrichmentAssetPage,
    EnrichmentWriteResult,
)
from src.core.graph.retrieval import (
    GraphRetrievalSpec,
    _apply_completeness_contract,
    _comparison_context,
    _empty_result,
    _neighbor_context,
    _relationship_context,
    _two_hop_context,
)
from src.core.graph.subnet import get_subnet
from src.core.product_client.schemas import TopologyConnectionRecord

logger = logging.getLogger(__name__)

_GRAPH_MUTATION_LOCK = threading.RLock()


def _serialized_graph_mutation(method: Any) -> Any:
    """Serialize topology publication and enrichment writes within one runtime."""
    @wraps(method)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _GRAPH_MUTATION_LOCK:
            return method(*args, **kwargs)

    return wrapped


try:  # Keep configuration/import checks usable before the production image is rebuilt.
    from neo4j import GraphDatabase, Query
    from neo4j.exceptions import Neo4jError, ServiceUnavailable
except ImportError:  # pragma: no cover - exercised only in dependency-missing environments
    GraphDatabase = None  # type: ignore[assignment]
    Query = None  # type: ignore[assignment]
    Neo4jError = Exception
    ServiceUnavailable = Exception


class Neo4jUnavailable(RuntimeError):
    pass


class Neo4jSchemaError(RuntimeError):
    pass


class GraphSyncValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GraphProjectionStatus:
    active_graph_version: str | None
    nodes: int
    edges: int
    last_successful_sync: str | None
    new_pending_assets: int = 0


@dataclass(frozen=True)
class GraphQueryPolicy:
    """The only place graph expansion limits are derived from Settings."""
    max_hops: int
    max_neighbors: int
    one_hop_max_nodes: int
    full_neighbors_hard_max: int
    max_nodes: int
    max_edges: int
    comparison_peer_limit: int
    comparison_shared_peer_limit: int
    timeout_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "GraphQueryPolicy":
        return cls(
            max_hops=settings.graph_max_path_length,
            max_neighbors=settings.graph_api_max_neighbors,
            one_hop_max_nodes=settings.graph_one_hop_max_nodes,
            full_neighbors_hard_max=settings.graph_full_neighbors_hard_max,
            max_nodes=settings.graph_two_hop_max_nodes,
            max_edges=settings.graph_max_edges,
            comparison_peer_limit=settings.graph_comparison_max_peers_per_entity,
            comparison_shared_peer_limit=settings.graph_comparison_max_shared_peers,
            timeout_seconds=settings.neo4j_query_timeout_seconds,
        )


class _ProjectionAdjacency:
    """Minimal directed adjacency view hydrated from the active Neo4j projection.

    It deliberately implements only the topology operations used by the bounded
    context contract. It is not a second graph store; all source records come
    from a bounded query of the active Community projection.
    """

    def __init__(self, nodes: list[str], edges: list[dict[str, object]]) -> None:
        self._nodes = set(nodes)
        self.nodes: dict[str, dict[str, object]] = {node: {} for node in self._nodes}
        self._outbound: dict[str, dict[str, int]] = {node: {} for node in self._nodes}
        self._inbound: dict[str, dict[str, int]] = {node: {} for node in self._nodes}
        for edge in edges:
            source, target = str(edge["source"]), str(edge["target"])
            weight = int(edge.get("weight") or 1)
            self._nodes.update((source, target))
            self.nodes.setdefault(source, {})
            self.nodes.setdefault(target, {})
            self._outbound.setdefault(source, {})[target] = weight
            self._inbound.setdefault(target, {})[source] = weight
            self._outbound.setdefault(target, {})
            self._inbound.setdefault(source, {})

    def __contains__(self, node: object) -> bool:
        return node in self._nodes

    def predecessors(self, node: str):
        return self._inbound.get(node, {}).keys()

    def successors(self, node: str):
        return self._outbound.get(node, {}).keys()

    def degree(self, node: str) -> int:
        return self.in_degree(node) + self.out_degree(node)

    def in_degree(self, node: str) -> int:
        return len(self._inbound.get(node, {}))

    def out_degree(self, node: str) -> int:
        return len(self._outbound.get(node, {}))

    def has_edge(self, source: str, target: str) -> bool:
        return target in self._outbound.get(source, {})

    def edge_weight(self, source: str, target: str) -> int:
        return self._outbound[source][target]

    def __getitem__(self, source: str) -> dict[str, dict[str, int]]:
        return {target: {"weight": weight} for target, weight in self._outbound[source].items()}


class Neo4jDriver:
    """One application-owned pooled driver; sessions are short lived."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._driver: Any | None = None

    def start(self) -> None:
        if self._driver is not None:
            return
        if GraphDatabase is None:
            raise Neo4jUnavailable("The official neo4j Python driver is not installed.")
        if not self.settings.neo4j_password:
            raise Neo4jUnavailable("SOORIN_NEO4J_PASSWORD is required when Neo4j graph runtime is enabled.")
        self._driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_user, self.settings.neo4j_password),
            max_connection_pool_size=self.settings.neo4j_max_connection_pool_size,
        )
        try:
            self._driver.verify_connectivity(database=self.settings.neo4j_database)
        except Exception as exc:
            self.close()
            raise Neo4jUnavailable("Neo4j connectivity verification failed.") from exc
        logger.info("event=neo4j_driver_initialized database=%s", self.settings.neo4j_database)

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    @contextmanager
    def session(self) -> Iterator[Any]:
        self.start()
        assert self._driver is not None
        with self._driver.session(database=self.settings.neo4j_database) as session:
            yield session


class Neo4jGraphRepository:
    """Controlled Cypher operations; no user text can become Cypher."""

    def __init__(self, driver: Neo4jDriver, settings: Settings) -> None:
        self.driver = driver
        self.settings = settings
        self.policy = GraphQueryPolicy.from_settings(settings)

    def bootstrap_schema(self) -> None:
        statements = (
            "CREATE CONSTRAINT asset_graph_identity IF NOT EXISTS FOR (a:Asset) REQUIRE (a.graph_key, a.graph_version) IS UNIQUE",
            "CREATE CONSTRAINT graph_metadata_id IF NOT EXISTS FOR (m:GraphMetadata) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT soorin_runtime_lease_name IF NOT EXISTS FOR (lease:SoorinRuntimeLease) REQUIRE lease.name IS UNIQUE",
            "CREATE INDEX asset_version_ip IF NOT EXISTS FOR (a:Asset) ON (a.graph_version, a.ip)",
            "CREATE INDEX asset_enrichment_schedule IF NOT EXISTS FOR (a:Asset) ON (a.graph_version, a.enrichment_status, a.enrichment_next_due_at)",
        )
        try:
            with self.driver.session() as session:
                for statement in statements:
                    session.run(statement).consume()
        except Exception as exc:
            raise Neo4jSchemaError("Neo4j graph schema bootstrap failed.") from exc
        logger.info("event=graph_schema_bootstrapped database=%s", self.settings.neo4j_database)

    @_serialized_graph_mutation
    def sync_snapshot(self, records: list[TopologyConnectionRecord], version: str) -> GraphProjectionStatus:
        pairs = self._normalize(records)
        if not pairs:
            raise GraphSyncValidationError("Product topology response contained no valid graph records.")
        nodes = sorted({ip for pair in pairs for ip in (pair["source"], pair["target"])})
        new_pending_assets = self._write_staging_nodes(nodes, version)
        self._write_staging_edges(pairs, version)
        self._validate_staging(version, len(nodes), len(pairs))
        self._publish(version)
        self._delete_inactive_versions(version)
        return GraphProjectionStatus(
            version,
            len(nodes),
            len(pairs),
            _utc_now(),
            int(new_pending_assets or 0),
        )

    def status(self) -> GraphProjectionStatus:
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (a:Asset {graph_version: m.active_graph_version})
        OPTIONAL MATCH (:Asset {graph_version: m.active_graph_version})-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->()
        RETURN m.active_graph_version AS version, m.last_successful_sync AS last_successful_sync,
               count(DISTINCT a) AS nodes, count(DISTINCT r) AS edges
        """
        with self.driver.session() as session:
            record = session.run(self._query(query),).single()
        if record is None:
            return GraphProjectionStatus(None, 0, 0, None)
        return GraphProjectionStatus(record["version"], int(record["nodes"] or 0), int(record["edges"] or 0), record["last_successful_sync"])

    def stats(self) -> dict[str, object]:
        """Return active-projection aggregates without hydrating a graph object."""
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        CALL (m) {
          WITH m
          MATCH (a:Asset {graph_version: m.active_graph_version})
          RETURN count(a) AS total_nodes, collect(a.ip) AS ips
        }
        CALL (m) {
          WITH m
          MATCH ()-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->()
          RETURN count(r) AS total_edges
        }
        CALL (m) {
          WITH m
          MATCH ()-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(target:Asset {graph_version: m.active_graph_version})
          WITH target.ip AS ip, count(r) AS incoming
          ORDER BY incoming DESC, ip ASC LIMIT 10
          RETURN collect({ip: ip, incoming: incoming}) AS top_destinations
        }
        CALL (m) {
          WITH m
          MATCH (source:Asset {graph_version: m.active_graph_version})-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->()
          WITH source.ip AS ip, count(r) AS outgoing
          ORDER BY outgoing DESC, ip ASC LIMIT 10
          RETURN collect({ip: ip, outgoing: outgoing}) AS top_sources
        }
        RETURN total_nodes, total_edges, ips, top_destinations, top_sources
        """
        row = self._single(query)
        if row is None:
            return {
                "total_nodes": 0, "total_edges": 0, "avg_degree": 0,
                "top_destinations": [], "top_sources": [], "ip_range_distribution": {},
            }
        total_nodes = int(row["total_nodes"] or 0)
        total_edges = int(row["total_edges"] or 0)
        ranges: dict[str, int] = {}
        for ip in row["ips"] or []:
            label = self._ip_range(str(ip))
            ranges[label] = ranges.get(label, 0) + 1
        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "avg_degree": round((2 * total_edges) / total_nodes, 1) if total_nodes else 0,
            "top_destinations": list(row["top_destinations"] or []),
            "top_sources": list(row["top_sources"] or []),
            "ip_range_distribution": ranges,
        }

    def get_summary(self, ip: str) -> dict[str, object]:
        """Aggregate a single asset without enumerating peer identities."""
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (a:Asset {graph_version: m.active_graph_version, graph_key: $ip})
        CALL (a, m) {
          WITH a, m
          OPTIONAL MATCH (a)<-[incoming:COMMUNICATES_WITH {graph_version: m.active_graph_version}]-()
          RETURN count(incoming) AS inbound
        }
        CALL (a, m) {
          WITH a, m
          OPTIONAL MATCH (a)-[outgoing:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->()
          RETURN count(outgoing) AS outbound
        }
        CALL (a, m) {
          WITH a, m
          OPTIONAL MATCH (a)-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(peer)
          WHERE EXISTS { MATCH (peer)-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(a) }
          RETURN count(peer) AS bidirectional
        }
        RETURN a IS NOT NULL AS found, inbound, outbound, bidirectional
        """
        row = self._single(query, ip=ip)
        found = bool(row and row["found"])
        inbound = int(row["inbound"] or 0) if row else 0
        outbound = int(row["outbound"] or 0) if row else 0
        return {"ip": ip, "found": found, "inbound_total": inbound, "outbound_total": outbound,
                "bidirectional_total": int(row["bidirectional"] or 0) if row else 0,
                "in_degree": inbound, "out_degree": outbound, "degree": inbound + outbound}

    def get_neighbors(self, ip: str, direction: str, limit: int) -> dict[str, object]:
        """Return deterministically ranked direct neighbors with the bound in Cypher."""
        normalized_direction = direction if direction in {"in", "out", "both"} else "both"
        cap = min(max(1, int(limit)), self.policy.max_neighbors)
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (a:Asset {graph_version: m.active_graph_version, graph_key: $ip})
        CALL (a, m) {
          WITH a, m
          WITH a, m WHERE a IS NOT NULL
          MATCH (a)-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(peer:Asset {graph_version: m.active_graph_version})
          WHERE $direction IN ['out', 'both']
          RETURN peer.ip AS ip, 'out' AS direction, r.weight AS edge_weight
          UNION ALL
          WITH a, m WHERE a IS NOT NULL
          MATCH (peer:Asset {graph_version: m.active_graph_version})-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(a)
          WHERE $direction IN ['in', 'both']
          RETURN peer.ip AS ip, 'in' AS direction, r.weight AS edge_weight
        }
        WITH a, collect({ip: ip, direction: direction, edge_weight: edge_weight}) AS all_rows
        UNWIND CASE WHEN a IS NULL THEN [] ELSE all_rows END AS item
        WITH a, item ORDER BY item.edge_weight DESC, item.ip ASC, item.direction ASC
        WITH a, collect(item) AS rows
        RETURN a IS NOT NULL AS found, size(rows) AS total, rows[0..$limit] AS rows
        """
        row = self._single(query, ip=ip, direction=normalized_direction, limit=cap)
        found = bool(row and row["found"])
        rows = list(row["rows"] or []) if row else []
        return {"target_ip": ip, "found": found, "direction": normalized_direction,
                "total": int(row["total"] or 0) if row else 0, "returned": len(rows), "neighbors": rows,
                "truncated": bool(row and int(row["total"] or 0) > len(rows))}

    def get_context(self, spec: GraphRetrievalSpec) -> dict[str, object]:
        """Return the exact bounded context contract from the active projection."""
        target = spec.entities[0].value if spec.entities else ""
        if spec.intent == "graph_relationships" and len(spec.entities) == 2:
            left, right = spec.entities[0].value, spec.entities[1].value
            _, left_nodes, left_edges = self._active_neighborhood(left, 1)
            _, right_nodes, right_edges = self._active_neighborhood(right, 1)
            graph = _ProjectionAdjacency(
                sorted(set(left_nodes).union(right_nodes)),
                list({(edge["source"], edge["target"]): edge for edge in [*left_edges, *right_edges]}.values()),
            )
            if spec.scope == "multi_entity_comparison" or spec.relationship_mode == "compare":
                return _apply_completeness_contract(_comparison_context(graph, spec, self.settings), spec)
            return _apply_completeness_contract(_relationship_context(graph, spec), spec)
        if spec.scope == "path":
            source = spec.entities[0].value if spec.entities else ""
            destination = spec.entities[1].value if len(spec.entities) > 1 else ""
            path = self.find_path(source, destination, self.policy.max_hops)
            base = _empty_result(spec, source, node_found=bool(path["source_present"] and path["target_present"]))
            nodes = list(path["path"])
            edges = [
                {"source": nodes[index], "target": nodes[index + 1], "weight": 1}
                for index in range(max(0, len(nodes) - 1))
            ]
            base.update({
                "source_ip": source, "destination_ip": destination, "target_ips": [source, destination],
                "source_present": path["source_present"], "target_present": path["target_present"],
                "path_exists": path["found"], "path_found": path["found"], "path_nodes": nodes,
                "path_edges": edges, "hop_count": path["edge_count"] if path["found"] else None,
                "nodes": [{"id": node, "hop": index, "subnet": get_subnet(node), "inbound": False, "outbound": False, "bidirectional": False} for index, node in enumerate(nodes)],
                "edges": edges, "candidate_node_count": len(nodes), "retrieved_node_count": len(nodes),
                "returned_node_count": len(nodes), "candidate_edge_count": len(edges), "retrieved_edge_count": len(edges),
                "returned_edge_count": len(edges), "node_found": bool(path["source_present"] and path["target_present"]),
            })
            return _apply_completeness_contract(base, spec)
        if spec.scope != "two_hop":
            if spec.scope in {"node_summary", "one_hop", "full_neighbors"}:
                found, nodes, edges = self._active_neighborhood(target, 1)
                if not found:
                    return _apply_completeness_contract(_empty_result(spec, target, node_found=False), spec)
                return _apply_completeness_contract(
                    _neighbor_context(_ProjectionAdjacency(nodes, edges), spec, self.settings),
                    spec,
                )
            raise ValueError("Neo4j context parity currently supports graph neighbor and relationship scopes only.")
        found, nodes, edges = self._active_neighborhood(target, 2)
        if not found:
            return _apply_completeness_contract(_empty_result(spec, target, node_found=False), spec)
        graph = _ProjectionAdjacency(nodes, edges)
        # The helper consumes only this bounded directed adjacency view, keeping
        # ordering and completeness behavior independent of a graph library.
        return _apply_completeness_contract(_two_hop_context(graph, spec, self.settings), spec)

    def _active_neighborhood(self, target: str, hops: int) -> tuple[bool, list[str], list[dict[str, object]]]:
        """Hydrate only the active undirected neighborhood needed for parity."""
        query = """
            MATCH (m:GraphMetadata {id: 'active'})
            OPTIONAL MATCH (root:Asset {graph_version: m.active_graph_version, graph_key: $target})
            CALL (root, m) {
              WITH root, m WHERE root IS NOT NULL
              MATCH path = (root)-[:COMMUNICATES_WITH*0..__MAX_HOPS__]-(candidate:Asset {graph_version: m.active_graph_version})
              WITH collect(DISTINCT candidate) AS candidates
              UNWIND candidates AS source
              OPTIONAL MATCH (source)-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(destination:Asset {graph_version: m.active_graph_version})
              WHERE destination IN candidates
              RETURN collect(DISTINCT source.ip) AS nodes,
                     collect(DISTINCT {source: source.ip, target: destination.ip, weight: r.weight}) AS edges
            }
            RETURN root IS NOT NULL AS found, nodes, edges
            """.replace("__MAX_HOPS__", str(hops))
        row = self._single(query, target=target)
        if not row or not row["found"]:
            return False, [], []
        edges = [edge for edge in list(row["edges"] or []) if edge.get("source") and edge.get("target")]
        return True, list(row["nodes"] or []), edges

    def find_path(self, source: str, target: str, max_hops: int | None = None) -> dict[str, object]:
        hops = min(self.policy.max_hops, max(1, int(max_hops or self.policy.max_hops)))
        # Cypher does not parameterize variable-length pattern bounds. `hops` is
        # a validated application policy value, never a user value.
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (source:Asset {graph_version: m.active_graph_version, graph_key: $source})
        OPTIONAL MATCH (target:Asset {graph_version: m.active_graph_version, graph_key: $target})
        CALL (source, target, m) {
          WITH source, target, m
          WITH source, target, m WHERE source IS NOT NULL AND target IS NOT NULL
          MATCH path = shortestPath((source)-[:COMMUNICATES_WITH*1..__MAX_HOPS__]->(target))
          RETURN [node IN nodes(path) | node.ip] AS nodes
          ORDER BY size(nodes) ASC, nodes ASC LIMIT 1
        }
        RETURN source IS NOT NULL AS source_present, target IS NOT NULL AS target_present, nodes
        """.replace("__MAX_HOPS__", str(hops))
        row = self._single(query, source=source, target=target)
        nodes = list(row["nodes"] or []) if row else []
        return {"source": source, "target": target, "source_present": bool(row and row["source_present"]),
                "target_present": bool(row and row["target_present"]), "found": bool(nodes), "path": nodes,
                "edge_count": max(0, len(nodes) - 1), "max_hops": hops}

    def get_relationship(self, source: str, target: str) -> dict[str, object]:
        """Exact directed direct-relationship result; staging versions are excluded."""
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (source:Asset {graph_version: m.active_graph_version, graph_key: $source})
        OPTIONAL MATCH (target:Asset {graph_version: m.active_graph_version, graph_key: $target})
        RETURN source IS NOT NULL AS source_present, target IS NOT NULL AS target_present,
          CASE WHEN source IS NULL OR target IS NULL THEN false
               ELSE EXISTS { MATCH (source)-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(target) } END AS forward_edge,
          CASE WHEN source IS NULL OR target IS NULL THEN false
               ELSE EXISTS { MATCH (target)-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(source) } END AS reverse_edge
        """
        row = self._single(query, source=source, target=target)
        source_present = bool(row and row["source_present"])
        target_present = bool(row and row["target_present"])
        forward, reverse = bool(row and row["forward_edge"]), bool(row and row["reverse_edge"])
        if forward and reverse:
            relationship = "bidirectional_direct_relationship"
        elif forward:
            relationship = "forward_direct_relationship"
        elif reverse:
            relationship = "reverse_direct_relationship"
        elif source_present and target_present:
            relationship = "no_direct_relationship"
        else:
            relationship = "entity_missing_from_active_graph"
        return {"source": source, "target": target, "source_present": source_present,
                "target_present": target_present, "forward_edge": forward, "reverse_edge": reverse,
                "bidirectional": forward and reverse, "relationship": relationship,
                "relationship_status": relationship}

    def compare_assets(self, entity_a: str, entity_b: str) -> dict[str, object]:
        """Bounded, deterministic parity record for the existing pair comparison."""
        peer_cap, shared_cap = self.policy.comparison_peer_limit, self.policy.comparison_shared_peer_limit
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (a:Asset {graph_version: m.active_graph_version, graph_key: $entity_a})
        OPTIONAL MATCH (b:Asset {graph_version: m.active_graph_version, graph_key: $entity_b})
        CALL {
          WITH a, m
          OPTIONAL MATCH (a)-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]-(peer:Asset {graph_version: m.active_graph_version})
          RETURN count(DISTINCT peer) AS a_total
        }
        CALL {
          WITH a, m
          OPTIONAL MATCH (a)-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]-(peer:Asset {graph_version: m.active_graph_version})
          WITH DISTINCT peer ORDER BY peer.ip ASC LIMIT $peer_cap
          RETURN collect(peer.ip) AS a_peers
        }
        CALL {
          WITH b, m
          OPTIONAL MATCH (b)-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]-(peer:Asset {graph_version: m.active_graph_version})
          RETURN count(DISTINCT peer) AS b_total
        }
        CALL {
          WITH b, m
          OPTIONAL MATCH (b)-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]-(peer:Asset {graph_version: m.active_graph_version})
          WITH DISTINCT peer ORDER BY peer.ip ASC LIMIT $peer_cap
          RETURN collect(peer.ip) AS b_peers
        }
        RETURN a IS NOT NULL AS a_present, b IS NOT NULL AS b_present, a_total, b_total, a_peers, b_peers
        """
        row = self._single(query, entity_a=entity_a, entity_b=entity_b, peer_cap=peer_cap)
        a_peers = sorted(str(peer) for peer in (row["a_peers"] or []) if peer) if row else []
        b_peers = sorted(str(peer) for peer in (row["b_peers"] or []) if peer) if row else []
        shared = sorted(set(a_peers).intersection(b_peers))
        relationship = self.get_relationship(entity_a, entity_b)
        a_total = int(row["a_total"] or 0) if row else 0
        b_total = int(row["b_total"] or 0) if row else 0
        # Shared identities need one bounded query too; the first-cap peer lists
        # are deliberately not treated as a complete overlap calculation.
        shared_limited = self._shared_peers(entity_a, entity_b, shared_cap)
        truncated = len(a_peers) < a_total or len(b_peers) < b_total
        return {
            "entities": [entity_a, entity_b], "node_found": bool(row and (row["a_present"] or row["b_present"])),
            "entity_a": self._comparison_entity(entity_a, bool(row and row["a_present"]), a_total, a_peers),
            "entity_b": self._comparison_entity(entity_b, bool(row and row["b_present"]), b_total, b_peers),
            "direct_relationship": {"a_to_b": relationship["forward_edge"], "b_to_a": relationship["reverse_edge"],
                                    "relationship": relationship["relationship"], "relationship_status": relationship["relationship_status"],
                                    "bidirectional": relationship["bidirectional"]},
            "shared_peer_total": len(shared), "shared_peers_retrieved": shared_limited,
            "shared_peers_retrieved_count": len(shared_limited),
            "entity_a_unique_peer_total": len(set(a_peers).difference(b_peers)),
            "entity_b_unique_peer_total": len(set(b_peers).difference(a_peers)),
            "retrieval_truncated": truncated,
        }

    @staticmethod
    def _comparison_entity(ip: str, present: bool, total: int, peers: list[str]) -> dict[str, object]:
        return {"ip": ip, "present": present, "total_peer_count": total,
                "peers_retrieved": peers, "peer_retrieved_count": len(peers),
                "retrieval_truncated": len(peers) < total}

    def _shared_peers(self, entity_a: str, entity_b: str, limit: int) -> list[str]:
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        MATCH (a:Asset {graph_version: m.active_graph_version, graph_key: $entity_a})-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]-(peer:Asset {graph_version: m.active_graph_version})
        MATCH (b:Asset {graph_version: m.active_graph_version, graph_key: $entity_b})-[:COMMUNICATES_WITH {graph_version: m.active_graph_version}]-(peer)
        RETURN DISTINCT peer.ip AS ip ORDER BY ip ASC LIMIT $limit
        """
        try:
            with self.driver.session() as session:
                return [str(record["ip"]) for record in session.run(self._query(query), entity_a=entity_a, entity_b=entity_b, limit=limit)]
        except Exception as exc:
            raise Neo4jUnavailable("Neo4j comparison overlap query failed.") from exc

    def topology(self, max_nodes: int, min_degree: int = 0, subnet: str = "") -> dict[str, object]:
        """Bounded UI projection; Streamlit never reads the database directly."""
        cap = min(max(1, int(max_nodes)), self.settings.graph_max_ui_nodes)
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        MATCH (a:Asset {graph_version: m.active_graph_version})
        OPTIONAL MATCH (a)-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]-()
        WITH m, a, count(r) AS degree
        WHERE degree >= $min_degree AND ($subnet = '' OR a.ip STARTS WITH $subnet)
        ORDER BY degree DESC, a.ip ASC LIMIT $limit
        WITH m, collect({ip: a.ip, degree: degree}) AS nodes, collect(a.ip) AS ips
        MATCH (source:Asset {graph_version: m.active_graph_version})-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(target:Asset {graph_version: m.active_graph_version})
        WHERE source.ip IN ips AND target.ip IN ips
        RETURN nodes, collect({source: source.ip, target: target.ip, weight: r.weight})[0..$edge_limit] AS edges
        """
        row = self._single(query, limit=cap, edge_limit=self.policy.max_edges, min_degree=max(0, int(min_degree)), subnet=subnet)
        return {
            "nodes": list(row["nodes"] or []) if row else [],
            "edges": list(row["edges"] or []) if row else [],
            "max_nodes": cap,
            "min_degree": max(0, int(min_degree)),
            "subnet": subnet,
        }

    @staticmethod
    def _ip_range(ip: str) -> str:
        if ip.startswith("192.168."):
            return "192.168.x.x"
        if ip.startswith("10."):
            return "10.x.x.x"
        if any(ip.startswith(f"172.{number}.") for number in range(16, 32)):
            return "172.16-31.x.x"
        if ip.startswith("169.254."):
            return "169.254.x.x"
        return "Other"

    def _single(self, query: str, **params: object) -> Any:
        try:
            with self.driver.session() as session:
                return session.run(self._query(query), **params).single()
        except Exception as exc:
            raise Neo4jUnavailable("Neo4j graph query failed.") from exc

    def list_due_enrichment_assets(
        self,
        *,
        after_graph_key: str | None,
        limit: int,
        as_of: datetime,
        stale_before: datetime,
        prefer_stale: bool = False,
    ) -> EnrichmentAssetPage:
        """Return one keyset page from the active projection only."""
        cap = min(max(1, int(limit)), self.settings.graph_enrichment_page_size)
        after_priority, after_key = self._decode_enrichment_cursor(after_graph_key)
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        MATCH (a:Asset {graph_version: m.active_graph_version})
        WITH a, CASE
          WHEN a.enrichment_status IS NULL OR a.enrichment_status = 'pending'
            THEN CASE WHEN $prefer_stale THEN 1 ELSE 0 END
          ELSE CASE WHEN $prefer_stale THEN 0 ELSE 1 END
        END AS priority
        WHERE (
            a.enrichment_status IS NULL
            OR a.enrichment_status = 'pending'
            OR a.enrichment_next_due_at <= $as_of
            OR a.enrichment_last_success_at <= $stale_before
          )
          AND (
            $after_priority IS NULL
            OR priority > $after_priority
            OR (priority = $after_priority AND a.graph_key > $after_graph_key)
          )
        RETURN a.graph_key AS graph_key, priority
        ORDER BY priority ASC, a.graph_key ASC
        LIMIT $fetch_limit
        """
        try:
            with self.driver.session() as session:
                result = session.run(
                    self._query(query),
                    after_graph_key=after_key,
                    after_priority=after_priority,
                    as_of=as_of.isoformat(),
                    stale_before=stale_before.isoformat(),
                    prefer_stale=prefer_stale,
                    fetch_limit=cap + 1,
                )
                rows = [
                    (int(record.get("priority", 0)), str(record["graph_key"]))
                    for record in result
                ]
        except Exception as exc:
            raise Neo4jUnavailable("Neo4j enrichment enumeration failed.") from exc
        selected = rows[:cap]
        return EnrichmentAssetPage(
            graph_keys=tuple(graph_key for _, graph_key in selected),
            next_cursor=(
                self._encode_enrichment_cursor(*selected[-1])
                if selected
                else after_graph_key
            ),
            has_more=len(rows) > cap,
        )

    def get_asset_enrichment_eligibility(
        self,
        graph_key: str,
        *,
        as_of: datetime,
        stale_before: datetime,
    ) -> AssetEnrichmentEligibility:
        """Inspect one active Asset without contacting Product."""
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (a:Asset {
          graph_version: m.active_graph_version,
          graph_key: $graph_key
        })
        RETURN a IS NOT NULL AS found,
               a.enrichment_status AS status,
               a.enrichment_last_success_at AS last_success_at,
               a.enrichment_next_due_at AS next_due_at,
               CASE WHEN a IS NULL THEN false ELSE (
                 a.enrichment_status IS NULL
                 OR a.enrichment_status = 'pending'
                 OR a.enrichment_next_due_at <= $as_of
                 OR a.enrichment_last_success_at <= $stale_before
               ) END AS needs_refresh
        """
        row = self._single(
            query,
            graph_key=graph_key,
            as_of=as_of.isoformat(),
            stale_before=stale_before.isoformat(),
        )
        if row is None or not bool(row["found"]):
            return AssetEnrichmentEligibility(graph_key, False, False)
        return AssetEnrichmentEligibility(
            graph_key=graph_key,
            found=True,
            needs_refresh=bool(row["needs_refresh"]),
            status=row["status"],
            last_success_at=row["last_success_at"],
            next_due_at=row["next_due_at"],
        )

    def enrichment_backlog_counts(
        self,
        *,
        as_of: datetime,
        stale_before: datetime,
    ) -> dict[str, int]:
        """Calculate bounded state gauges once per scheduler cycle, never per scrape."""
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        MATCH (a:Asset {graph_version: m.active_graph_version})
        RETURN
          count(CASE WHEN a.enrichment_status IS NULL OR a.enrichment_status = 'pending' THEN 1 END) AS pending,
          count(CASE WHEN a.enrichment_status = 'stale' THEN 1 END) AS stale,
          count(CASE WHEN a.enrichment_status = 'error' THEN 1 END) AS error,
          count(CASE WHEN a.enrichment_status = 'unavailable' THEN 1 END) AS unavailable,
          count(CASE WHEN
            a.enrichment_status IS NULL
            OR a.enrichment_status = 'pending'
            OR a.enrichment_next_due_at <= $as_of
            OR a.enrichment_last_success_at <= $stale_before
          THEN 1 END) AS backlog
        """
        row = self._single(
            query,
            as_of=as_of.isoformat(),
            stale_before=stale_before.isoformat(),
        )
        return {
            state: int((row or {}).get(state, 0) or 0)
            for state in ("pending", "stale", "error", "unavailable", "backlog")
        }

    def try_acquire_lease(
        self,
        lease_name: str,
        owner_id: str,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        """Atomically acquire an absent, owned, or expired runtime lease."""
        instant = now or datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(
            instant.timestamp() + ttl_seconds, tz=timezone.utc
        )
        query = """
        MERGE (lease:SoorinRuntimeLease {name: $lease_name})
        ON CREATE SET lease.owner_id = $owner_id,
                      lease.lease_expires_at = $expires_at,
                      lease.updated_at = $now
        WITH lease
        WHERE lease.owner_id = $owner_id
           OR lease.lease_expires_at IS NULL
           OR datetime(lease.lease_expires_at) <= datetime($now)
        SET lease.owner_id = $owner_id,
            lease.lease_expires_at = $expires_at,
            lease.updated_at = $now
        RETURN lease.owner_id = $owner_id AS acquired
        """
        return self._lease_write(
            query,
            lease_name=lease_name,
            owner_id=owner_id,
            now=instant.isoformat(),
            expires_at=expires_at.isoformat(),
        )

    def renew_lease(
        self,
        lease_name: str,
        owner_id: str,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        instant = now or datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(
            instant.timestamp() + ttl_seconds, tz=timezone.utc
        )
        query = """
        MATCH (lease:SoorinRuntimeLease {name: $lease_name, owner_id: $owner_id})
        SET lease.lease_expires_at = $expires_at, lease.updated_at = $now
        RETURN true AS acquired
        """
        return self._lease_write(
            query,
            lease_name=lease_name,
            owner_id=owner_id,
            now=instant.isoformat(),
            expires_at=expires_at.isoformat(),
        )

    def release_lease(self, lease_name: str, owner_id: str) -> bool:
        query = """
        MATCH (lease:SoorinRuntimeLease {name: $lease_name, owner_id: $owner_id})
        DELETE lease
        RETURN true AS acquired
        """
        return self._lease_write(query, lease_name=lease_name, owner_id=owner_id)

    def _lease_write(self, query: str, **params: object) -> bool:
        try:
            with self.driver.session() as session:
                row = session.execute_write(lambda tx: tx.run(query, **params).single())
        except Exception as exc:
            raise Neo4jUnavailable("Neo4j runtime lease operation failed.") from exc
        return bool(row and row.get("acquired", False))

    @_serialized_graph_mutation
    def apply_enrichment_batch(
        self,
        mutations: list[AssetEnrichmentMutation],
    ) -> EnrichmentWriteResult:
        """Apply bounded semantic mutations to currently active Assets."""
        if not mutations:
            return EnrichmentWriteResult(attempted=0, updated=0)
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        UNWIND $rows AS row
        OPTIONAL MATCH (a:Asset {
          graph_version: m.active_graph_version,
          graph_key: row.graph_key
        })
        FOREACH (_ IN CASE WHEN a IS NOT NULL AND row.status = 'success' THEN [1] ELSE [] END |
          SET a += row.properties,
              a.enrichment_status = 'success',
              a.enrichment_updated_at = row.succeeded_at,
              a.enrichment_last_attempt_at = row.attempted_at,
              a.enrichment_last_success_at = row.succeeded_at,
              a.enrichment_next_due_at = row.next_due_at,
              a.enrichment_source = row.source,
              a.enrichment_version = row.version,
              a.enrichment_error = null
        )
        FOREACH (_ IN CASE WHEN a IS NOT NULL AND row.status <> 'success' THEN [1] ELSE [] END |
          SET a.enrichment_status = CASE
                WHEN a.enrichment_last_success_at IS NULL THEN row.status
                ELSE 'stale'
              END,
              a.enrichment_last_attempt_at = row.attempted_at,
              a.enrichment_next_due_at = row.next_due_at,
              a.enrichment_source = row.source,
              a.enrichment_error = row.error
        )
        RETURN count(a) AS updated,
               collect(CASE WHEN a IS NULL THEN row.graph_key END) AS missing_graph_keys
        """
        updated = 0
        missing: list[str] = []
        batch_size = self.settings.graph_enrichment_batch_size
        try:
            with self.driver.session() as session:
                for offset in range(0, len(mutations), batch_size):
                    rows = [item.to_row() for item in mutations[offset : offset + batch_size]]
                    record = session.execute_write(
                        lambda tx: tx.run(query, rows=rows).single()
                    )
                    if record is not None:
                        updated += int(record["updated"] or 0)
                        missing.extend(str(key) for key in (record["missing_graph_keys"] or []))
        except Exception as exc:
            raise Neo4jUnavailable("Neo4j enrichment batch write failed.") from exc
        return EnrichmentWriteResult(
            attempted=len(mutations),
            updated=updated,
            missing_graph_keys=tuple(missing),
        )

    def _write_staging_nodes(self, nodes: list[str], version: str) -> int:
        query = """
        UNWIND $rows AS row
        OPTIONAL MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (previous:Asset {
          graph_key: row.ip,
          graph_version: m.active_graph_version
        })
        MERGE (a:Asset {graph_key: row.ip, graph_version: $version})
        SET a.ip = row.ip
        FOREACH (_ IN CASE WHEN previous IS NULL THEN [] ELSE [1] END |
          SET a.asset_name = previous.asset_name,
              a.status = previous.status,
              a.suggested_type = previous.suggested_type,
              a.model_confidence = previous.model_confidence,
              a.mapping_confidence = previous.mapping_confidence,
              a.unknown_score = previous.unknown_score,
              a.classification_summary = previous.classification_summary,
              a.vendor = previous.vendor,
              a.product = previous.product,
              a.role = previous.role,
              a.roles = previous.roles,
              a.tag = previous.tag,
              a.sub_tag = previous.sub_tag,
              a.last_detection_at = previous.last_detection_at,
              a.enrichment_status = previous.enrichment_status,
              a.enrichment_updated_at = previous.enrichment_updated_at,
              a.enrichment_last_attempt_at = previous.enrichment_last_attempt_at,
              a.enrichment_last_success_at = previous.enrichment_last_success_at,
              a.enrichment_next_due_at = previous.enrichment_next_due_at,
              a.enrichment_source = previous.enrichment_source,
              a.enrichment_version = previous.enrichment_version,
              a.enrichment_error = previous.enrichment_error
        )
        SET a.enrichment_status = coalesce(a.enrichment_status, 'pending')
        RETURN sum(CASE WHEN previous IS NULL THEN 1 ELSE 0 END) AS new_assets
        """
        return self._batched(query, [{"ip": ip} for ip in nodes], version, count_key="new_assets")

    def _write_staging_edges(self, pairs: list[dict[str, Any]], version: str) -> None:
        query = """
        UNWIND $rows AS row
        MATCH (source:Asset {graph_key: row.source, graph_version: $version})
        MATCH (target:Asset {graph_key: row.target, graph_version: $version})
        MERGE (source)-[r:COMMUNICATES_WITH {graph_version: $version}]->(target)
        SET r.weight = row.weight
        """
        self._batched(query, pairs, version)

    def _batched(
        self,
        query: str,
        rows: list[dict[str, Any]],
        version: str,
        *,
        count_key: str | None = None,
    ) -> int:
        size = self.settings.neo4j_sync_batch_size
        total = 0
        try:
            with self.driver.session() as session:
                for offset in range(0, len(rows), size):
                    batch = rows[offset : offset + size]
                    if count_key is None:
                        session.execute_write(lambda tx: tx.run(query, rows=batch, version=version).consume())
                    else:
                        record = session.execute_write(
                            lambda tx: tx.run(query, rows=batch, version=version).single()
                        )
                        total += int((record or {}).get(count_key, 0) or 0)
                    logger.info("event=graph_sync_batch_completed version=%s rows=%s", version, len(batch))
        except Exception as exc:
            raise Neo4jUnavailable("Neo4j batch synchronization failed.") from exc
        return total

    def _validate_staging(self, version: str, expected_nodes: int, expected_edges: int) -> None:
        query = """
        MATCH (a:Asset {graph_version: $version})
        OPTIONAL MATCH (:Asset {graph_version: $version})-[r:COMMUNICATES_WITH {graph_version: $version}]->()
        RETURN count(DISTINCT a) AS nodes, count(DISTINCT r) AS edges
        """
        with self.driver.session() as session:
            record = session.run(self._query(query), version=version).single()
        if record is None or int(record["nodes"] or 0) != expected_nodes or int(record["edges"] or 0) != expected_edges:
            raise GraphSyncValidationError("Neo4j staging projection failed count validation.")

    def _publish(self, version: str) -> None:
        query = """
        MERGE (m:GraphMetadata {id: 'active'})
        SET m.active_graph_version = $version, m.last_successful_sync = $now,
            m.sync_status = 'published', m.schema_version = 1
        """
        with self.driver.session() as session:
            session.execute_write(lambda tx: tx.run(query, version=version, now=_utc_now()).consume())
        logger.info("event=graph_sync_published version=%s", version)

    def _delete_inactive_versions(self, active_version: str) -> None:
        query = "MATCH (a:Asset) WHERE a.graph_version <> $version DETACH DELETE a"
        with self.driver.session() as session:
            session.execute_write(lambda tx: tx.run(query, version=active_version).consume())

    @staticmethod
    def _normalize(records: list[TopologyConnectionRecord]) -> list[dict[str, Any]]:
        weights: dict[tuple[str, str], int] = {}
        for record in records:
            source, target = record.src_ip.strip(), record.dst_ip.strip()
            if not source or not target:
                continue
            weights[(source, target)] = weights.get((source, target), 0) + int(record.weight or 1)
        return [{"source": source, "target": target, "weight": weight} for (source, target), weight in sorted(weights.items())]

    def _query(self, cypher: str) -> Any:
        if Query is None:
            return cypher
        return Query(cypher, timeout=self.policy.timeout_seconds)

    @staticmethod
    def _encode_enrichment_cursor(priority: int, graph_key: str) -> str:
        return f"{priority}|{graph_key}"

    @staticmethod
    def _decode_enrichment_cursor(cursor: str | None) -> tuple[int | None, str | None]:
        if cursor is None:
            return None, None
        priority, separator, graph_key = cursor.partition("|")
        if separator and priority in {"0", "1"}:
            return int(priority), graph_key
        return 0, cursor


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
