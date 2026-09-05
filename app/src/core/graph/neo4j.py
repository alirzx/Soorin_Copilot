"""Neo4j persistence boundary for the observed topology projection.

Product remains authoritative.  Each successful full snapshot is written with
an unpublished version and becomes readable only when GraphMetadata points at
that version.  This keeps the prior graph readable if a refresh fails.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from src.config.settings import Settings
from src.core.product_client.schemas import TopologyConnectionRecord

logger = logging.getLogger(__name__)

try:  # Keep configuration/import checks usable before the production image is rebuilt.
    from neo4j import GraphDatabase
    from neo4j.exceptions import Neo4jError, ServiceUnavailable
except ImportError:  # pragma: no cover - exercised only in dependency-missing environments
    GraphDatabase = None  # type: ignore[assignment]
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


@dataclass(frozen=True)
class GraphQueryPolicy:
    """The only place graph expansion limits are derived from Settings."""
    max_hops: int
    max_neighbors: int
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
            max_nodes=settings.graph_two_hop_max_nodes,
            max_edges=settings.graph_max_edges,
            comparison_peer_limit=settings.graph_comparison_max_peers_per_entity,
            comparison_shared_peer_limit=settings.graph_comparison_max_shared_peers,
            timeout_seconds=settings.neo4j_query_timeout_seconds,
        )


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
            "CREATE INDEX asset_version_ip IF NOT EXISTS FOR (a:Asset) ON (a.graph_version, a.ip)",
        )
        try:
            with self.driver.session() as session:
                for statement in statements:
                    session.run(statement).consume()
        except Exception as exc:
            raise Neo4jSchemaError("Neo4j graph schema bootstrap failed.") from exc
        logger.info("event=graph_schema_bootstrapped database=%s", self.settings.neo4j_database)

    def sync_snapshot(self, records: list[TopologyConnectionRecord], version: str) -> GraphProjectionStatus:
        pairs = self._normalize(records)
        if not pairs:
            raise GraphSyncValidationError("Product topology response contained no valid graph records.")
        nodes = sorted({ip for pair in pairs for ip in (pair["source"], pair["target"])})
        self._write_staging_nodes(nodes, version)
        self._write_staging_edges(pairs, version)
        self._validate_staging(version, len(nodes), len(pairs))
        self._publish(version)
        self._delete_inactive_versions(version)
        return GraphProjectionStatus(version, len(nodes), len(pairs), _utc_now())

    def status(self) -> GraphProjectionStatus:
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (a:Asset {graph_version: m.active_graph_version})
        OPTIONAL MATCH (:Asset {graph_version: m.active_graph_version})-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->()
        RETURN m.active_graph_version AS version, m.last_successful_sync AS last_successful_sync,
               count(DISTINCT a) AS nodes, count(DISTINCT r) AS edges
        """
        with self.driver.session() as session:
            record = session.run(query, timeout=self.settings.neo4j_query_timeout_seconds).single()
        if record is None:
            return GraphProjectionStatus(None, 0, 0, None)
        return GraphProjectionStatus(record["version"], int(record["nodes"] or 0), int(record["edges"] or 0), record["last_successful_sync"])

    def get_summary(self, ip: str) -> dict[str, object]:
        """Aggregate a single asset without enumerating peer identities."""
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (a:Asset {graph_version: m.active_graph_version, graph_key: $ip})
        CALL {
          WITH a, m
          OPTIONAL MATCH (a)<-[incoming:COMMUNICATES_WITH {graph_version: m.active_graph_version}]-()
          RETURN count(incoming) AS inbound
        }
        CALL {
          WITH a, m
          OPTIONAL MATCH (a)-[outgoing:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->()
          RETURN count(outgoing) AS outbound
        }
        CALL {
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
        CALL {
          WITH a, m, $direction AS direction
          WITH a, m, direction WHERE a IS NOT NULL
          MATCH (a)-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(peer:Asset {graph_version: m.active_graph_version})
          WHERE direction IN ['out', 'both']
          RETURN peer.ip AS ip, 'out' AS direction, r.weight AS edge_weight
          UNION ALL
          WITH a, m, direction WHERE a IS NOT NULL
          MATCH (peer:Asset {graph_version: m.active_graph_version})-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(a)
          WHERE direction IN ['in', 'both']
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

    def find_path(self, source: str, target: str, max_hops: int | None = None) -> dict[str, object]:
        hops = min(self.policy.max_hops, max(1, int(max_hops or self.policy.max_hops)))
        query = """
        MATCH (m:GraphMetadata {id: 'active'})
        OPTIONAL MATCH (source:Asset {graph_version: m.active_graph_version, graph_key: $source})
        OPTIONAL MATCH (target:Asset {graph_version: m.active_graph_version, graph_key: $target})
        CALL {
          WITH source, target, m
          WITH source, target, m WHERE source IS NOT NULL AND target IS NOT NULL
          MATCH path = shortestPath((source)-[:COMMUNICATES_WITH*..$max_hops]->(target))
          RETURN [node IN nodes(path) | node.ip] AS nodes
          ORDER BY size(nodes) ASC, nodes ASC LIMIT 1
        }
        RETURN source IS NOT NULL AS source_present, target IS NOT NULL AS target_present, nodes
        """
        row = self._single(query, source=source, target=target, max_hops=hops)
        nodes = list(row["nodes"] or []) if row else []
        return {"source": source, "target": target, "source_present": bool(row and row["source_present"]),
                "target_present": bool(row and row["target_present"]), "found": bool(nodes), "path": nodes,
                "edge_count": max(0, len(nodes) - 1), "max_hops": hops}

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
        WITH m, collect(a.ip) AS ips
        MATCH (source:Asset {graph_version: m.active_graph_version})-[r:COMMUNICATES_WITH {graph_version: m.active_graph_version}]->(target:Asset {graph_version: m.active_graph_version})
        WHERE source.ip IN ips AND target.ip IN ips
        RETURN ips, collect({source: source.ip, target: target.ip, weight: r.weight})[0..$edge_limit] AS edges
        """
        row = self._single(query, limit=cap, edge_limit=self.policy.max_edges, min_degree=max(0, int(min_degree)), subnet=subnet)
        return {"nodes": list(row["ips"] or []) if row else [], "edges": list(row["edges"] or []) if row else []}

    def _single(self, query: str, **params: object) -> Any:
        try:
            with self.driver.session() as session:
                return session.run(query, **params, timeout=self.policy.timeout_seconds).single()
        except Exception as exc:
            raise Neo4jUnavailable("Neo4j graph query failed.") from exc

    def _write_staging_nodes(self, nodes: list[str], version: str) -> None:
        query = """
        UNWIND $rows AS row
        MERGE (a:Asset {graph_key: row.ip, graph_version: $version})
        SET a.ip = row.ip
        """
        self._batched(query, [{"ip": ip} for ip in nodes], version)

    def _write_staging_edges(self, pairs: list[dict[str, Any]], version: str) -> None:
        query = """
        UNWIND $rows AS row
        MATCH (source:Asset {graph_key: row.source, graph_version: $version})
        MATCH (target:Asset {graph_key: row.target, graph_version: $version})
        MERGE (source)-[r:COMMUNICATES_WITH {graph_version: $version}]->(target)
        SET r.weight = row.weight
        """
        self._batched(query, pairs, version)

    def _batched(self, query: str, rows: list[dict[str, Any]], version: str) -> None:
        size = self.settings.neo4j_sync_batch_size
        try:
            with self.driver.session() as session:
                for offset in range(0, len(rows), size):
                    batch = rows[offset : offset + size]
                    session.execute_write(lambda tx: tx.run(query, rows=batch, version=version).consume())
                    logger.info("event=graph_sync_batch_completed version=%s rows=%s", version, len(batch))
        except Exception as exc:
            raise Neo4jUnavailable("Neo4j batch synchronization failed.") from exc

    def _validate_staging(self, version: str, expected_nodes: int, expected_edges: int) -> None:
        query = """
        MATCH (a:Asset {graph_version: $version})
        OPTIONAL MATCH (:Asset {graph_version: $version})-[r:COMMUNICATES_WITH {graph_version: $version}]->()
        RETURN count(DISTINCT a) AS nodes, count(DISTINCT r) AS edges
        """
        with self.driver.session() as session:
            record = session.run(query, version=version, timeout=self.settings.neo4j_query_timeout_seconds).single()
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
