"""Graph query service for topology analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.config.settings import Settings, get_settings
from src.core.graph.refresh import get_refresh_status
from src.core.graph.neo4j import Neo4jDriver, Neo4jGraphRepository, Neo4jUnavailable
from src.core.graph.retrieval import GraphRetrievalSpec
from src.core.graph.subnet import get_subnet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphStatus:
    loaded: bool
    nodes: int
    edges: int
    directed: bool
    artifact_available: bool
    active_graph_loaded_at: str | None = None
    active_graph_source: str | None = None
    active_graph_version: str | None = None
    refresh_enabled: bool = False
    refresh_running: bool = False
    refresh_interval_seconds: int = 0
    refresh_last_attempt_at: str | None = None
    refresh_last_success_at: str | None = None
    refresh_last_failure_at: str | None = None
    refresh_last_error_type: str | None = None
    refresh_last_error_message: str | None = None
    refresh_consecutive_failures: int = 0
    raw_snapshot_path: str | None = None
    processed_snapshot_path: str | None = None
    last_known_good: bool = False


class GraphService:
    """Deterministic query service for the observed communication graph."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.driver = Neo4jDriver(self.settings)
        self.repository = Neo4jGraphRepository(self.driver, self.settings)

    def status(self) -> GraphStatus:
        refresh = get_refresh_status()
        try:
            projection = self.repository.status()
        except Neo4jUnavailable:
            projection = None
        common = {
            # Only the published Neo4j metadata may identify the active graph.
            # Refresh state describes attempts; it must never advertise staging.
            "active_graph_loaded_at": projection.last_successful_sync if projection else None,
            "active_graph_source": "neo4j_projection" if projection and projection.active_graph_version else None,
            "active_graph_version": projection.active_graph_version if projection else None,
            "refresh_enabled": bool(refresh.get("enabled", False)),
            "refresh_running": bool(refresh.get("running", False)),
            "refresh_interval_seconds": int(refresh.get("interval_seconds", 0) or 0),
            "refresh_last_attempt_at": refresh.get("last_attempt_at"),
            "refresh_last_success_at": refresh.get("last_success_at"),
            "refresh_last_failure_at": refresh.get("last_failure_at"),
            "refresh_last_error_type": refresh.get("last_error_type"),
            "refresh_last_error_message": refresh.get("last_error_message"),
            "refresh_consecutive_failures": int(refresh.get("consecutive_failures", 0) or 0),
            "raw_snapshot_path": refresh.get("raw_snapshot_path"),
            "processed_snapshot_path": refresh.get("processed_snapshot_path"),
        }
        if projection is None or projection.active_graph_version is None:
            logger.info(
                "event=graph_query type=status loaded=false artifact_available=%s",
                False,
            )
            return GraphStatus(
                loaded=False,
                nodes=0,
                edges=0,
                directed=True,
                artifact_available=False,
                last_known_good=False,
                **common,
            )

        logger.info(
            "event=graph_query type=status loaded=true nodes=%s edges=%s artifact_available=%s",
            projection.nodes,
            projection.edges,
            False,
        )
        return GraphStatus(
            loaded=True,
            nodes=projection.nodes,
            edges=projection.edges,
            directed=True,
            artifact_available=False,
            last_known_good=True,
            **common,
        )

    def stats(self) -> dict[str, Any]:
        stats = self.repository.stats()
        logger.info("event=graph_query type=stats nodes=%s edges=%s", stats["total_nodes"], stats["total_edges"])
        return stats

    def context(self, spec: GraphRetrievalSpec) -> dict[str, object]:
        """Return normalized context with published-projection provenance."""
        context = self.repository.get_context(spec)
        projection = self.repository.status()
        return {
            **context,
            "graph_provider": "neo4j_projection",
            "active_graph_version": projection.active_graph_version,
            "graph_last_successful_sync": projection.last_successful_sync,
        }

    def node(self, ip: str) -> dict[str, Any]:
        target_ip = ip.strip()
        summary = self.repository.get_summary(target_ip)
        found = bool(summary["found"])
        logger.info("event=graph_api_node target_ip=%s found=%s", target_ip, found)
        if not found:
            return {"ip": target_ip, "found": False, "degree": {"in": 0, "out": 0, "total": 0}}
        return {
            "ip": target_ip,
            "found": True,
            "degree": {
                "in": summary["in_degree"],
                "out": summary["out_degree"],
                "total": summary["degree"],
            },
        }

    def neighbors(self, ip: str, *, direction: str = "both", limit: int = 20) -> dict[str, Any]:
        target_ip = ip.strip()
        direction = direction.strip().lower()
        result = self.repository.get_neighbors(target_ip, direction, limit)
        logger.info(
            "event=graph_api_neighbors target_ip=%s direction=%s found=true result_count=%s returned=%s",
            target_ip,
            direction,
            result["total"],
            result["returned"],
        )
        return {
            **result,
        }

    def path(self, source: str, target: str) -> dict[str, Any]:
        source_ip = source.strip()
        target_ip = target.strip()
        logger.info("event=graph_api_path source_ip=%s target_ip=%s", source_ip, target_ip)
        semantics = "Observed communication-graph path, not proof of routed network path."

        if source_ip == target_ip:
            return {"source": source_ip, "target": target_ip, "found": True, "path": [source_ip], "edge_count": 0, "semantics": semantics}
        result = self.repository.find_path(source_ip, target_ip)
        if not result["source_present"]:
            result["reason"] = "source_not_found"
        elif not result["target_present"]:
            result["reason"] = "target_not_found"
        elif not result["found"]:
            result["reason"] = "no_observed_communication_graph_path"
        return {**result, "semantics": semantics}

    def relationship(self, source: str, target: str) -> dict[str, Any]:
        return self.repository.get_relationship(source.strip(), target.strip())

    def comparison(self, entity_a: str, entity_b: str) -> dict[str, Any]:
        return self.repository.compare_assets(entity_a.strip(), entity_b.strip())

    def topology(
        self,
        *,
        max_nodes: int | None = None,
        min_degree: int | None = None,
        subnet: str = "",
    ) -> dict[str, Any]:
        requested_nodes = self.settings.graph_max_ui_nodes if max_nodes is None else max_nodes
        requested_degree = self.settings.graph_default_min_degree if min_degree is None else min_degree
        return self.repository.topology(
            max_nodes=requested_nodes,
            min_degree=requested_degree,
            subnet=subnet.strip(),
        )


def get_stats() -> dict[str, Any]:
    """Get basic graph statistics."""
    # Legacy Streamlit helper.  Task 2 moves these UI-only callers; the online
    # GraphService above is Neo4j-only.
    from collections import Counter
    from src.core.graph.loader import get_graph
    G = get_graph()
    logger.info("event=graph_query type=stats nodes=%s edges=%s", G.number_of_nodes(), G.number_of_edges())

    in_degrees = dict(G.in_degree())
    out_degrees = dict(G.out_degree())

    top_in = sorted(in_degrees.items(), key=lambda x: x[1], reverse=True)[:10]
    top_out = sorted(out_degrees.items(), key=lambda x: x[1], reverse=True)[:10]

    def ip_range(ip: str) -> str:
        if ip.startswith("192.168."):
            return "192.168.x.x"
        elif ip.startswith("10."):
            return "10.x.x.x"
        elif any(ip.startswith(f"172.{i}.") for i in range(16, 32)):
            return "172.16-31.x.x"
        elif ip.startswith("169.254."):
            return "169.254.x.x"
        else:
            return "Other"

    all_ips = list(G.nodes())
    range_counts = Counter(ip_range(ip) for ip in all_ips)

    return {
        "total_nodes": G.number_of_nodes(),
        "total_edges": G.number_of_edges(),
        "avg_degree": round(sum(dict(G.degree()).values()) / G.number_of_nodes(), 1) if G.number_of_nodes() else 0,
        "top_destinations": [{"ip": ip, "incoming": deg} for ip, deg in top_in],
        "top_sources": [{"ip": ip, "outgoing": deg} for ip, deg in top_out],
        "ip_range_distribution": dict(range_counts),
    }


def get_neighbors(ip: str, depth: int = 1) -> dict[str, Any]:
    """Get neighbors of an IP up to a certain depth."""
    from src.core.graph.loader import get_graph
    G = get_graph()
    target_ip = ip.strip()
    logger.info("event=graph_query type=neighbors target_ip=%s", target_ip)

    if target_ip not in G:
        logger.info("event=graph_neighbors_result target_ip=%s node_found=false", target_ip)
        return {"error": f"IP {target_ip} not found in graph"}

    successors = sorted(G.successors(target_ip))
    predecessors = sorted(G.predecessors(target_ip))
    logger.info(
        "event=graph_neighbors_result target_ip=%s node_found=true incoming=%s outgoing=%s",
        target_ip,
        len(predecessors),
        len(successors),
    )

    return {
        "ip": target_ip,
        "outgoing_count": len(successors),
        "incoming_count": len(predecessors),
        "outgoing": successors[:50],
        "incoming": predecessors[:50],
        "total_degree": G.degree(target_ip),
    }


def get_path(src_ip: str, dst_ip: str, max_length: int = 5) -> dict[str, Any]:
    """Find shortest path in the observed communication graph.

    This does not prove actual packet routing or network-layer reachability.
    """
    import networkx as nx
    from src.core.graph.loader import get_graph

    G = get_graph()
    source = src_ip.strip()
    destination = dst_ip.strip()
    logger.info("event=graph_query type=path source_ip=%s destination_ip=%s", source, destination)

    if source not in G:
        return {"error": f"Source IP {source} not found"}
    if destination not in G:
        return {"error": f"Destination IP {destination} not found"}

    try:
        path = nx.shortest_path(G, source=source, target=destination)
    except nx.NetworkXNoPath:
        logger.info("event=graph_path_result source_ip=%s destination_ip=%s found=false", source, destination)
        return {"error": f"No observed communication-graph path found between {source} and {destination}"}

    if len(path) > max_length + 1:
        logger.info(
            "event=graph_path_result source_ip=%s destination_ip=%s found=true hops=%s truncated=true",
            source,
            destination,
            len(path) - 1,
        )
        return {"error": f"Path too long ({len(path) - 1} hops, max {max_length})"}

    logger.info(
        "event=graph_path_result source_ip=%s destination_ip=%s found=true hops=%s",
        source,
        destination,
        len(path) - 1,
    )
    return {
        "source": source,
        "destination": destination,
        "hops": len(path) - 1,
        "path": path,
        "semantics": "Shortest path in observed communication graph, not proof of routed network path.",
    }


def get_subnet_nodes(subnet_prefix: str) -> list[str]:
    """Get all nodes in a subnet."""
    from src.core.graph.loader import get_graph
    G = get_graph()
    prefix = subnet_prefix.strip()
    return sorted(node for node in G.nodes() if node.startswith(prefix))


def get_node_list(page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """Get paginated list of all nodes."""
    from src.core.graph.loader import get_graph
    G = get_graph()
    all_nodes = sorted(G.nodes())

    total = len(all_nodes)
    page = max(1, int(page or 1))
    page_size = min(500, max(1, int(page_size or 50)))
    start = (page - 1) * page_size
    end = start + page_size

    nodes_page = all_nodes[start:end]

    node_data = []
    for node in nodes_page:
        node_data.append(
            {
                "ip": node,
                "in_degree": G.in_degree(node),
                "out_degree": G.out_degree(node),
                "total_degree": G.degree(node),
            }
        )

    logger.info("event=graph_query type=node_list page=%s page_size=%s returned=%s total=%s", page, page_size, len(node_data), total)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "nodes": node_data,
    }

def get_subnet_list() -> list[dict[str, Any]]:
    """Get list of subnets with node counts."""
    from collections import Counter
    from src.core.graph.loader import get_graph
    G = get_graph()
    subnet_counts = Counter(get_subnet(n) for n in G.nodes())
    logger.info("event=graph_query type=subnet_list subnets=%s", len(subnet_counts))
    return [
        {"subnet": subnet, "count": count}
        for subnet, count in subnet_counts.most_common()
    ]
