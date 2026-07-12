"""Graph query service for topology analysis."""

from __future__ import annotations

import ipaddress
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

import networkx as nx

from src.config.settings import Settings, get_settings
from src.core.graph.loader import get_cached_graph, get_graph, get_graph_metadata
from src.core.graph.refresh import get_refresh_status
from src.core.graph.storage import resolve_path

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

    def status(self) -> GraphStatus:
        cached = get_cached_graph()
        artifact_available = resolve_path(self.settings.graph_pickle_path).exists()
        metadata = get_graph_metadata()
        refresh = get_refresh_status()
        common = {
            "active_graph_loaded_at": metadata.get("active_graph_loaded_at") or refresh.get("active_graph_loaded_at"),
            "active_graph_source": metadata.get("active_graph_source") or refresh.get("active_graph_source"),
            "active_graph_version": metadata.get("active_graph_version") or refresh.get("active_graph_version"),
            "refresh_enabled": bool(refresh.get("enabled", False)),
            "refresh_running": bool(refresh.get("running", False)),
            "refresh_interval_seconds": int(refresh.get("interval_seconds", 0) or 0),
            "refresh_last_attempt_at": refresh.get("last_attempt_at"),
            "refresh_last_success_at": refresh.get("last_success_at"),
            "refresh_last_failure_at": refresh.get("last_failure_at"),
            "refresh_last_error_type": refresh.get("last_error_type"),
            "refresh_last_error_message": refresh.get("last_error_message"),
            "refresh_consecutive_failures": int(refresh.get("consecutive_failures", 0) or 0),
            "raw_snapshot_path": refresh.get("raw_snapshot_path") or metadata.get("raw_snapshot_path"),
            "processed_snapshot_path": refresh.get("processed_snapshot_path") or metadata.get("processed_snapshot_path"),
        }
        if cached is None:
            logger.info(
                "event=graph_query type=status loaded=false artifact_available=%s",
                artifact_available,
            )
            return GraphStatus(
                loaded=False,
                nodes=0,
                edges=0,
                directed=True,
                artifact_available=artifact_available,
                last_known_good=False,
                **common,
            )

        logger.info(
            "event=graph_query type=status loaded=true nodes=%s edges=%s artifact_available=%s",
            cached.number_of_nodes(),
            cached.number_of_edges(),
            artifact_available,
        )
        return GraphStatus(
            loaded=True,
            nodes=cached.number_of_nodes(),
            edges=cached.number_of_edges(),
            directed=cached.is_directed(),
            artifact_available=artifact_available,
            last_known_good=True,
            **common,
        )

    def stats(self) -> dict[str, Any]:
        return get_stats()

    def node(self, ip: str) -> dict[str, Any]:
        graph = get_graph()
        target_ip = ip.strip()
        found = target_ip in graph
        logger.info("event=graph_api_node target_ip=%s found=%s", target_ip, found)
        if not found:
            return {"ip": target_ip, "found": False, "degree": {"in": 0, "out": 0, "total": 0}}
        return {
            "ip": target_ip,
            "found": True,
            "degree": {
                "in": graph.in_degree(target_ip),
                "out": graph.out_degree(target_ip),
                "total": graph.degree(target_ip),
            },
        }

    def neighbors(self, ip: str, *, direction: str = "both", limit: int = 20) -> dict[str, Any]:
        graph = get_graph()
        target_ip = ip.strip()
        direction = direction.strip().lower()
        limit = min(self.settings.graph_api_max_neighbors, max(1, int(limit)))

        if target_ip not in graph:
            logger.info("event=graph_api_neighbors target_ip=%s direction=%s found=false", target_ip, direction)
            return {
                "target_ip": target_ip,
                "found": False,
                "direction": direction,
                "total": 0,
                "returned": 0,
                "neighbors": [],
            }

        records: list[dict[str, Any]] = []
        if direction in {"out", "both"}:
            records.extend(
                {
                    "ip": peer,
                    "direction": "out",
                    "edge_weight": int(graph[target_ip][peer].get("weight", 1)),
                }
                for peer in graph.successors(target_ip)
            )
        if direction in {"in", "both"}:
            records.extend(
                {
                    "ip": peer,
                    "direction": "in",
                    "edge_weight": int(graph[peer][target_ip].get("weight", 1)),
                }
                for peer in graph.predecessors(target_ip)
            )

        records = sorted(records, key=lambda item: (-item["edge_weight"], item["ip"], item["direction"]))
        returned = min(len(records), limit)
        logger.info(
            "event=graph_api_neighbors target_ip=%s direction=%s found=true result_count=%s returned=%s",
            target_ip,
            direction,
            len(records),
            returned,
        )
        return {
            "target_ip": target_ip,
            "found": True,
            "direction": direction,
            "total": len(records),
            "returned": returned,
            "neighbors": records[:limit],
        }

    def path(self, source: str, target: str) -> dict[str, Any]:
        graph = get_graph()
        source_ip = source.strip()
        target_ip = target.strip()
        logger.info("event=graph_api_path source_ip=%s target_ip=%s", source_ip, target_ip)
        semantics = "Observed communication-graph path, not proof of routed network path."

        if source_ip == target_ip:
            return {"source": source_ip, "target": target_ip, "found": True, "path": [source_ip], "edge_count": 0, "semantics": semantics}
        if source_ip not in graph:
            return {"source": source_ip, "target": target_ip, "found": False, "path": [], "edge_count": 0, "reason": "source_not_found", "semantics": semantics}
        if target_ip not in graph:
            return {"source": source_ip, "target": target_ip, "found": False, "path": [], "edge_count": 0, "reason": "target_not_found", "semantics": semantics}

        try:
            path = nx.shortest_path(graph, source=source_ip, target=target_ip)
        except nx.NetworkXNoPath:
            return {"source": source_ip, "target": target_ip, "found": False, "path": [], "edge_count": 0, "reason": "no_observed_communication_graph_path", "semantics": semantics}

        return {"source": source_ip, "target": target_ip, "found": True, "path": path, "edge_count": len(path) - 1, "semantics": semantics}


def get_stats() -> dict[str, Any]:
    """Get basic graph statistics."""
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
    G = get_graph()
    prefix = subnet_prefix.strip()
    return sorted(node for node in G.nodes() if node.startswith(prefix))


def get_node_list(page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """Get paginated list of all nodes."""
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

def get_subnet(ip: str) -> str:
    """Return the IPv4 /24 subnet in canonical CIDR notation."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "other"
    if address.version == 4:
        network = ipaddress.ip_network(f"{address}/24", strict=False)
        return str(network)
    return "other"


def get_subnet_list() -> list[dict[str, Any]]:
    """Get list of subnets with node counts."""
    G = get_graph()
    subnet_counts = Counter(get_subnet(n) for n in G.nodes())
    logger.info("event=graph_query type=subnet_list subnets=%s", len(subnet_counts))
    return [
        {"subnet": subnet, "count": count}
        for subnet, count in subnet_counts.most_common()
    ]
