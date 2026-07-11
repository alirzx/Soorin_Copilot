"""Bounded graph retrieval scopes for Copilot context."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import networkx as nx

from src.config.settings import Settings
from src.core.context.models import GraphDirection, GraphScope, ResolvedEntity
from src.core.graph.loader import get_graph
from src.core.graph.service import get_subnet


GRAPH_CONTEXT_LIMITATIONS = [
    "Edges represent observed unique IP communication pairs.",
    "Current topology records do not prove packet routing or reachability.",
    "Port, protocol, byte, process, frequency, and traffic volume data are not included.",
]


@dataclass(frozen=True)
class GraphRetrievalSpec:
    scope: GraphScope
    direction: GraphDirection
    depth: int
    entities: list[ResolvedEntity]


def _empty_result(spec: GraphRetrievalSpec, target_ip: str, *, node_found: bool) -> dict[str, object]:
    return {
        "target_ip": target_ip,
        "node_found": node_found,
        "scope": spec.scope,
        "direction": spec.direction,
        "depth": spec.depth,
        "inbound_total": 0,
        "inbound_retrieved": 0,
        "inbound_returned": 0,
        "inbound_context_included": 0,
        "outbound_total": 0,
        "outbound_retrieved": 0,
        "outbound_returned": 0,
        "outbound_context_included": 0,
        "bidirectional_total": 0,
        "bidirectional_retrieved": 0,
        "bidirectional_returned": 0,
        "bidirectional_context_included": 0,
        "candidate_node_count": 0,
        "retrieved_node_count": 0,
        "returned_node_count": 0,
        "context_node_count": 0,
        "candidate_edge_count": 0,
        "retrieved_edge_count": 0,
        "returned_edge_count": 0,
        "context_edge_count": 0,
        "nodes": [],
        "edges": [],
        "subnets_reached": [],
        "retrieval_truncated": False,
        "retrieval_truncation_reasons": [],
        "retrieval_truncation_reason": None,
        "context_truncated": False,
        "context_truncation_reason": None,
        "truncated": False,
        "truncation_reason": None,
        "limitations": GRAPH_CONTEXT_LIMITATIONS,
    }


def _edge_dict(src: str, dst: str, graph: nx.DiGraph) -> dict[str, object]:
    return {"source": src, "target": dst, "weight": graph[src][dst].get("weight", 1)}


def _neighbors(graph: nx.DiGraph, target: str, direction: GraphDirection) -> tuple[list[str], list[str], list[str]]:
    inbound = sorted(graph.predecessors(target))
    outbound = sorted(graph.successors(target))
    bidirectional = sorted(set(inbound).intersection(outbound))
    if direction == "inbound":
        return inbound, [], bidirectional
    if direction == "outbound":
        return [], outbound, bidirectional
    if direction == "both":
        return inbound, outbound, bidirectional
    return [], [], []


def _truncate_nodes(nodes: list[str], limit: int) -> tuple[list[str], bool]:
    if len(nodes) <= limit:
        return nodes, False
    return nodes[:limit], True


def _reason(reason_type: str, limit: int, candidate: int, returned: int) -> dict[str, object]:
    return {"type": reason_type, "limit": limit, "candidate": candidate, "returned": returned}


def _reason_string(reasons: list[dict[str, object]]) -> str | None:
    if not reasons:
        return None
    first = reasons[0]
    return f"{first['type']}:{first['limit']}"


def retrieve_graph_context(spec: GraphRetrievalSpec, settings: Settings) -> dict[str, object]:
    graph = get_graph()
    target_ip = spec.entities[0].value if spec.entities else ""
    if not target_ip or target_ip not in graph:
        return _empty_result(spec, target_ip, node_found=False)

    if spec.scope == "path":
        return _path_context(graph, spec, settings)
    if spec.scope == "two_hop":
        return _two_hop_context(graph, spec, settings)
    return _neighbor_context(graph, spec, settings)


def _neighbor_context(graph: nx.DiGraph, spec: GraphRetrievalSpec, settings: Settings) -> dict[str, object]:
    target = spec.entities[0].value
    inbound_all = sorted(graph.predecessors(target))
    outbound_all = sorted(graph.successors(target))
    bidirectional_all = sorted(set(inbound_all).intersection(outbound_all))
    inbound, outbound, bidirectional = _neighbors(graph, target, spec.direction)
    all_candidate_nodes = sorted(set([target, *inbound, *outbound]))
    candidate_edges = [_edge_dict(src, target, graph) for src in inbound] + [_edge_dict(target, dst, graph) for dst in outbound]

    if spec.scope == "node_summary":
        node_limit = min(settings.graph_one_hop_max_nodes, 20)
        edge_limit = min(settings.graph_max_edges, 40)
    elif spec.scope == "full_neighbors":
        node_limit = settings.graph_full_neighbors_hard_max
        edge_limit = settings.graph_max_edges
    else:
        node_limit = settings.graph_one_hop_max_nodes
        edge_limit = settings.graph_max_edges

    retrieved_nodes, node_truncated = _truncate_nodes(all_candidate_nodes, node_limit)
    retrieved_node_set = set(retrieved_nodes)
    candidate_edges_in_nodes = [edge for edge in candidate_edges if edge["source"] in retrieved_node_set and edge["target"] in retrieved_node_set]
    edge_truncated = len(candidate_edges_in_nodes) > edge_limit
    retrieved_edges = candidate_edges_in_nodes[:edge_limit]
    retrieval_reasons = []
    if node_truncated:
        retrieval_reasons.append(_reason("node_limit", node_limit, len(all_candidate_nodes), len(retrieved_nodes)))
    if edge_truncated:
        retrieval_reasons.append(_reason("edge_limit", edge_limit, len(candidate_edges_in_nodes), len(retrieved_edges)))
    retrieval_truncated = bool(retrieval_reasons)
    retrieval_reason = _reason_string(retrieval_reasons)

    inbound_retrieved = len([node for node in inbound if node in retrieved_node_set])
    outbound_retrieved = len([node for node in outbound if node in retrieved_node_set])
    bidirectional_retrieved = len([node for node in bidirectional if node in retrieved_node_set])

    return {
        "target_ip": target,
        "node_found": True,
        "scope": spec.scope,
        "direction": spec.direction,
        "depth": spec.depth,
        "inbound_total": len(inbound_all),
        "inbound_retrieved": inbound_retrieved,
        "inbound_returned": inbound_retrieved,
        "inbound_context_included": 0,
        "outbound_total": len(outbound_all),
        "outbound_retrieved": outbound_retrieved,
        "outbound_returned": outbound_retrieved,
        "outbound_context_included": 0,
        "bidirectional_total": len(bidirectional_all),
        "bidirectional_retrieved": bidirectional_retrieved,
        "bidirectional_returned": bidirectional_retrieved,
        "bidirectional_context_included": 0,
        "candidate_node_count": len(all_candidate_nodes),
        "retrieved_node_count": len(retrieved_nodes),
        "returned_node_count": len(retrieved_nodes),
        "context_node_count": 0,
        "candidate_edge_count": len(candidate_edges),
        "retrieved_edge_count": len(retrieved_edges),
        "returned_edge_count": len(retrieved_edges),
        "context_edge_count": 0,
        "nodes": [{"id": node, "hop": 0 if node == target else 1, "subnet": get_subnet(node)} for node in retrieved_nodes],
        "edges": retrieved_edges,
        "top_inbound_peers": inbound[: min(20, len(inbound))],
        "top_outbound_peers": outbound[: min(20, len(outbound))],
        "bidirectional_peers": bidirectional[: min(20, len(bidirectional))],
        "subnets_reached": sorted({get_subnet(peer) for peer in outbound}),
        "retrieval_truncated": retrieval_truncated,
        "retrieval_truncation_reasons": retrieval_reasons,
        "retrieval_truncation_reason": retrieval_reason,
        "context_truncated": False,
        "context_truncation_reason": None,
        "truncated": retrieval_truncated,
        "truncation_reason": retrieval_reason,
        "limitations": GRAPH_CONTEXT_LIMITATIONS,
    }


def _two_hop_context(graph: nx.DiGraph, spec: GraphRetrievalSpec, settings: Settings) -> dict[str, object]:
    target = spec.entities[0].value
    seen = {target}
    hops = {target: 0}
    queue: deque[tuple[str, int]] = deque([(target, 0)])
    edges: list[dict[str, object]] = []
    candidate_node_count = 1
    candidate_edge_count = 0

    while queue:
        node, hop = queue.popleft()
        if hop >= 2:
            continue
        next_nodes = []
        if spec.direction in {"inbound", "both"}:
            next_nodes.extend((peer, node) for peer in sorted(graph.predecessors(node)))
        if spec.direction in {"outbound", "both"}:
            next_nodes.extend((node, peer) for peer in sorted(graph.successors(node)))
        for src, dst in next_nodes:
            candidate_edge_count += 1
            peer = dst if src == node else src
            if peer not in seen:
                candidate_node_count += 1
            if len(edges) < settings.graph_max_edges:
                edges.append(_edge_dict(src, dst, graph))
            if peer not in seen and len(seen) < settings.graph_two_hop_max_nodes:
                seen.add(peer)
                hops[peer] = hop + 1
                queue.append((peer, hop + 1))

    inbound_all = sorted(graph.predecessors(target))
    outbound_all = sorted(graph.successors(target))
    bidirectional_all = sorted(set(inbound_all).intersection(outbound_all))
    retrieval_reasons = []
    if candidate_node_count > settings.graph_two_hop_max_nodes:
        retrieval_reasons.append(_reason("node_limit", settings.graph_two_hop_max_nodes, candidate_node_count, len(seen)))
    if candidate_edge_count > settings.graph_max_edges:
        retrieval_reasons.append(_reason("edge_limit", settings.graph_max_edges, candidate_edge_count, len(edges)))
    retrieval_truncated = bool(retrieval_reasons)
    retrieval_reason = _reason_string(retrieval_reasons)

    return {
        "target_ip": target,
        "node_found": True,
        "scope": spec.scope,
        "direction": spec.direction,
        "depth": 2,
        "inbound_total": len(inbound_all),
        "inbound_retrieved": len([node for node in inbound_all if node in seen]),
        "inbound_returned": len([node for node in inbound_all if node in seen]),
        "inbound_context_included": 0,
        "outbound_total": len(outbound_all),
        "outbound_retrieved": len([node for node in outbound_all if node in seen]),
        "outbound_returned": len([node for node in outbound_all if node in seen]),
        "outbound_context_included": 0,
        "bidirectional_total": len(bidirectional_all),
        "bidirectional_retrieved": len([node for node in bidirectional_all if node in seen]),
        "bidirectional_returned": len([node for node in bidirectional_all if node in seen]),
        "bidirectional_context_included": 0,
        "candidate_node_count": candidate_node_count,
        "retrieved_node_count": len(seen),
        "returned_node_count": len(seen),
        "context_node_count": 0,
        "candidate_edge_count": candidate_edge_count,
        "retrieved_edge_count": len(edges),
        "returned_edge_count": len(edges),
        "context_edge_count": 0,
        "nodes": [{"id": node, "hop": hops[node], "subnet": get_subnet(node)} for node in sorted(seen, key=lambda n: (hops[n], n))],
        "edges": edges,
        "retrieval_truncated": retrieval_truncated,
        "retrieval_truncation_reasons": retrieval_reasons,
        "retrieval_truncation_reason": retrieval_reason,
        "context_truncated": False,
        "context_truncation_reason": None,
        "truncated": retrieval_truncated,
        "truncation_reason": retrieval_reason,
        "limitations": GRAPH_CONTEXT_LIMITATIONS,
    }


def _path_context(graph: nx.DiGraph, spec: GraphRetrievalSpec, settings: Settings) -> dict[str, object]:
    source = spec.entities[0].value if spec.entities else ""
    target = spec.entities[1].value if len(spec.entities) > 1 else ""
    base = _empty_result(spec, source, node_found=bool(source in graph))
    base.update({"source_ip": source, "destination_ip": target, "path_exists": False, "path_nodes": [], "path_edges": [], "hop_count": None})
    if source not in graph or target not in graph:
        return base
    try:
        path_nodes = nx.shortest_path(graph, source=source, target=target)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        base.update({"node_found": True})
        return base
    if len(path_nodes) - 1 > settings.graph_max_path_length:
        path_nodes = path_nodes[: settings.graph_max_path_length + 1]
        truncated = True
        truncation_reason = f"path_length_limit:{settings.graph_max_path_length}"
    else:
        truncated = False
        truncation_reason = None
    path_edges = [_edge_dict(path_nodes[i], path_nodes[i + 1], graph) for i in range(len(path_nodes) - 1)]
    base.update(
        {
            "node_found": True,
            "scope": "path",
            "direction": spec.direction,
            "depth": 0,
            "path_exists": True,
            "path_nodes": path_nodes,
            "path_edges": path_edges,
            "hop_count": len(path_nodes) - 1,
            "candidate_node_count": len(path_nodes),
            "retrieved_node_count": len(path_nodes),
            "returned_node_count": len(path_nodes),
            "context_node_count": 0,
            "candidate_edge_count": len(path_edges),
            "retrieved_edge_count": len(path_edges),
            "returned_edge_count": len(path_edges),
            "context_edge_count": 0,
            "nodes": [{"id": node, "hop": index, "subnet": get_subnet(node)} for index, node in enumerate(path_nodes)],
            "edges": path_edges,
            "retrieval_truncated": truncated,
            "retrieval_truncation_reasons": [_reason("path_length_limit", settings.graph_max_path_length, len(path_nodes), len(path_nodes))] if truncated else [],
            "retrieval_truncation_reason": truncation_reason,
            "context_truncated": False,
            "context_truncation_reason": None,
            "truncated": truncated,
            "truncation_reason": truncation_reason,
        }
    )
    return base
