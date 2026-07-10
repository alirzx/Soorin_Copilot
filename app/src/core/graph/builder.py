"""Build NetworkX topology graphs from product API records."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

import networkx as nx

from src.core.product_client.schemas import TopologyConnectionRecord


logger = logging.getLogger(__name__)


def ip_range(ip: str) -> str:
    if ip.startswith("192.168."):
        return "192.168.x.x (Private C)"
    if ip.startswith("10."):
        return "10.x.x.x (Private A)"
    if any(ip.startswith(f"172.{i}.") for i in range(16, 32)):
        return "172.16-31.x.x (Private B)"
    if ip.startswith("169.254."):
        return "169.254.x.x (Link-Local)"
    return "Other/Public"


def build_topology_graph(records: list[TopologyConnectionRecord]) -> tuple[nx.DiGraph, int]:
    """Build a directed observed-communication graph from validated records.

    A directed edge `src_ip -> dst_ip` means the product observed a unique
    communication pair. Duplicate records increase edge weight; they do not
    prove packet routing or network reachability.
    """
    logger.info("event=graph_build_started records=%s", len(records))
    graph = nx.DiGraph()
    processed_edges = 0

    for record in records:
        if graph.has_edge(record.src_ip, record.dst_ip):
            graph[record.src_ip][record.dst_ip]["weight"] += record.weight
        else:
            graph.add_edge(record.src_ip, record.dst_ip, weight=record.weight)
        processed_edges += 1

    logger.info(
        "event=graph_build_completed records=%s processed_edges=%s nodes=%s edges=%s",
        len(records),
        processed_edges,
        graph.number_of_nodes(),
        graph.number_of_edges(),
    )
    return graph, processed_edges


def build_graph_stats(
    graph: nx.DiGraph,
    *,
    raw_records_count: int,
    processed_edges: int,
    fetch_duration_seconds: float,
    source_endpoint_path: str,
) -> dict[str, object]:
    in_degrees = dict(graph.in_degree())
    out_degrees = dict(graph.out_degree())
    top_in = sorted(in_degrees.items(), key=lambda item: item[1], reverse=True)[:10]
    top_out = sorted(out_degrees.items(), key=lambda item: item[1], reverse=True)[:10]
    all_ips = list(graph.nodes())
    range_counts = Counter(ip_range(ip) for ip in all_ips)
    total_degree = sum(dict(graph.degree()).values())

    return {
        "total_nodes": graph.number_of_nodes(),
        "total_edges": graph.number_of_edges(),
        "raw_records": raw_records_count,
        "processed_edges": processed_edges,
        "avg_degree": round(total_degree / graph.number_of_nodes(), 1) if graph.number_of_nodes() else 0,
        "top_destinations": [{"ip": ip, "incoming": degree} for ip, degree in top_in],
        "top_sources": [{"ip": ip, "outgoing": degree} for ip, degree in top_out],
        "ip_range_distribution": dict(range_counts),
        "fetch_timestamp": datetime.now().isoformat(),
        "fetch_duration_seconds": round(fetch_duration_seconds, 1),
        "source_endpoint_path": source_endpoint_path,
    }
