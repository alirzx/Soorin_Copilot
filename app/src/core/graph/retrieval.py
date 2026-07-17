"""Bounded graph retrieval scopes for Copilot context."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import networkx as nx

from src.config.settings import Settings
from src.core.context.models import GraphDirection, GraphScope, IntentName, RelationshipMode, ResolvedEntity
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
    intent: IntentName = "graph_neighbors"
    relationship_mode: RelationshipMode = "none"
    exhaustive_connections_requested: bool = False


def _apply_completeness_contract(
    result: dict[str, object],
    spec: GraphRetrievalSpec,
) -> dict[str, object]:
    """Attach one canonical contract for retrieval, serialization, and user scope."""
    retrieval_truncated = bool(result.get("retrieval_truncated", result.get("truncated", False)))
    retrieval_complete = not retrieval_truncated
    requested_scope_complete = retrieval_complete or spec.scope == "node_summary"
    result.update(
        {
            "requested_scope": spec.scope,
            "retrieval_complete": retrieval_complete,
            "retrieval_truncated": retrieval_truncated,
            "retrieval_truncation_reason": result.get("retrieval_truncation_reason") or result.get("truncation_reason"),
            "serialized_context_complete_for_retrieved_subset": True,
            "serialized_context_truncated": False,
            "serialized_context_truncation_reason": None,
            "requested_scope_complete": requested_scope_complete,
            "complete_for_user_request": requested_scope_complete,
            "exhaustive_connections_requested": spec.exhaustive_connections_requested,
        }
    )
    return result


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
        "included_node_count": 0,
        "context_node_count": 0,
        "candidate_edge_count": 0,
        "retrieved_edge_count": 0,
        "returned_edge_count": 0,
        "included_edge_count": 0,
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


def _node_dict(node: str, *, target: str = "", hop: int = 1, inbound: bool = False, outbound: bool = False) -> dict[str, object]:
    return {
        "id": node,
        "hop": 0 if node == target else hop,
        "subnet": get_subnet(node),
        "inbound": inbound,
        "outbound": outbound,
        "bidirectional": inbound and outbound,
    }


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
    if spec.intent == "graph_relationships" and len(spec.entities) == 2:
        if spec.scope == "multi_entity_comparison" or spec.relationship_mode == "compare":
            result = _comparison_context(graph, spec, settings)
        else:
            result = _relationship_context(graph, spec)
    elif spec.scope == "path":
        result = _path_context(graph, spec, settings)
    else:
        target_ip = spec.entities[0].value if spec.entities else ""
        if not target_ip or target_ip not in graph:
            result = _empty_result(spec, target_ip, node_found=False)
        elif spec.scope == "two_hop":
            result = _two_hop_context(graph, spec, settings)
        else:
            result = _neighbor_context(graph, spec, settings)
    return _apply_completeness_contract(result, spec)


def _neighbor_context(graph: nx.DiGraph, spec: GraphRetrievalSpec, settings: Settings) -> dict[str, object]:
    target = spec.entities[0].value
    inbound_all = sorted(graph.predecessors(target))
    outbound_all = sorted(graph.successors(target))
    bidirectional_all = sorted(set(inbound_all).intersection(outbound_all))
    inbound, outbound, bidirectional = _neighbors(graph, target, spec.direction)
    all_candidate_nodes = [target, *sorted(set([*inbound, *outbound]).difference({target}))]
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
        "included_node_count": 0,
        "context_node_count": 0,
        "candidate_edge_count": len(candidate_edges),
        "retrieved_edge_count": len(retrieved_edges),
        "returned_edge_count": len(retrieved_edges),
        "included_edge_count": 0,
        "context_edge_count": 0,
        "nodes": [
            _node_dict(
                node,
                target=target,
                inbound=node in inbound,
                outbound=node in outbound,
            )
            for node in retrieved_nodes
        ],
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
    candidate_nodes = {target}
    candidate_edge_keys: set[tuple[str, str]] = set()
    eligible_edge_count = 0

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
            edge_key = (src, dst)
            if edge_key in candidate_edge_keys:
                continue
            candidate_edge_keys.add(edge_key)
            peer = dst if src == node else src
            candidate_nodes.add(peer)
            if peer not in seen and len(seen) < settings.graph_two_hop_max_nodes:
                seen.add(peer)
                hops[peer] = hop + 1
                queue.append((peer, hop + 1))
            if src in seen and dst in seen:
                eligible_edge_count += 1
                if len(edges) < settings.graph_max_edges:
                    edges.append(_edge_dict(src, dst, graph))

    inbound_all = sorted(graph.predecessors(target))
    outbound_all = sorted(graph.successors(target))
    bidirectional_all = sorted(set(inbound_all).intersection(outbound_all))
    retrieval_reasons = []
    candidate_node_count = len(candidate_nodes)
    candidate_edge_count = len(candidate_edge_keys)
    if candidate_node_count > settings.graph_two_hop_max_nodes:
        retrieval_reasons.append(_reason("node_limit", settings.graph_two_hop_max_nodes, candidate_node_count, len(seen)))
    if eligible_edge_count > settings.graph_max_edges:
        retrieval_reasons.append(_reason("edge_limit", settings.graph_max_edges, eligible_edge_count, len(edges)))
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
        "included_node_count": 0,
        "context_node_count": 0,
        "candidate_edge_count": candidate_edge_count,
        "retrieved_edge_count": len(edges),
        "returned_edge_count": len(edges),
        "included_edge_count": 0,
        "context_edge_count": 0,
        "nodes": [
            _node_dict(
                node,
                target=target,
                hop=hops[node],
                inbound=node in inbound_all,
                outbound=node in outbound_all,
            )
            for node in sorted(seen, key=lambda n: (hops[n], n))
        ],
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
    source_present = source in graph
    target_present = target in graph
    base = _empty_result(spec, source, node_found=source_present and target_present)
    base.update(
        {
            "source_ip": source,
            "destination_ip": target,
            "target_ips": [source, target],
            "source_present": source_present,
            "target_present": target_present,
            "path_exists": False,
            "path_found": False,
            "path_nodes": [],
            "path_edges": [],
            "hop_count": None,
        }
    )
    if source not in graph or target not in graph:
        return base
    try:
        candidate_path_nodes = nx.shortest_path(graph, source=source, target=target)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        base.update({"node_found": True})
        return base
    candidate_node_count = len(candidate_path_nodes)
    candidate_edge_count = max(0, candidate_node_count - 1)
    if candidate_edge_count > settings.graph_max_path_length:
        path_nodes = candidate_path_nodes[: settings.graph_max_path_length + 1]
        truncated = True
        truncation_reason = f"path_length_limit:{settings.graph_max_path_length}"
    else:
        path_nodes = candidate_path_nodes
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
            "path_found": True,
            "path_nodes": path_nodes,
            "path_edges": path_edges,
            "hop_count": len(path_nodes) - 1,
            "candidate_node_count": candidate_node_count,
            "retrieved_node_count": len(path_nodes),
            "returned_node_count": len(path_nodes),
            "included_node_count": 0,
            "context_node_count": 0,
            "candidate_edge_count": candidate_edge_count,
            "retrieved_edge_count": len(path_edges),
            "returned_edge_count": len(path_edges),
            "included_edge_count": 0,
            "context_edge_count": 0,
            "nodes": [_node_dict(node, target=source, hop=index) for index, node in enumerate(path_nodes)],
            "edges": path_edges,
            "retrieval_truncated": truncated,
            "retrieval_truncation_reasons": [_reason("path_length_limit", settings.graph_max_path_length, candidate_node_count, len(path_nodes))] if truncated else [],
            "retrieval_truncation_reason": truncation_reason,
            "context_truncated": False,
            "context_truncation_reason": None,
            "truncated": truncated,
            "truncation_reason": truncation_reason,
        }
    )
    return base


def _relationship_context(graph: nx.DiGraph, spec: GraphRetrievalSpec) -> dict[str, object]:
    source = spec.entities[0].value
    target = spec.entities[1].value
    source_present = source in graph
    target_present = target in graph
    forward_edge = bool(source_present and target_present and graph.has_edge(source, target))
    reverse_edge = bool(source_present and target_present and graph.has_edge(target, source))
    edges = []
    if forward_edge:
        edges.append(_edge_dict(source, target, graph))
    if reverse_edge:
        edges.append(_edge_dict(target, source, graph))
    if forward_edge and reverse_edge:
        relationship = "bidirectional_direct_relationship"
    elif forward_edge:
        relationship = "forward_direct_relationship"
    elif reverse_edge:
        relationship = "reverse_direct_relationship"
    elif source_present and target_present:
        relationship = "no_direct_relationship"
    else:
        relationship = "entity_missing_from_active_graph"
    return {
        "target_ip": source,
        "target_ips": [source, target],
        "node_found": source_present and target_present,
        "scope": "one_hop",
        "direction": "both",
        "depth": 1,
        "relationship_mode": "direct",
        "source": source,
        "target": target,
        "source_present": source_present,
        "target_present": target_present,
        "forward_edge": forward_edge,
        "reverse_edge": reverse_edge,
        "relationship": relationship,
        "relationship_status": relationship,
        "bidirectional": forward_edge and reverse_edge,
        "candidate_node_count": 2,
        "retrieved_node_count": int(source_present) + int(target_present),
        "returned_node_count": int(source_present) + int(target_present),
        "included_node_count": 0,
        "context_node_count": 0,
        "candidate_edge_count": int(forward_edge) + int(reverse_edge),
        "retrieved_edge_count": len(edges),
        "returned_edge_count": len(edges),
        "included_edge_count": 0,
        "context_edge_count": 0,
        "nodes": [_node_dict(node, target=source, hop=0 if node == source else 1) for node in (source, target) if node in graph],
        "edges": edges,
        "retrieval_truncated": False,
        "retrieval_truncation_reasons": [],
        "retrieval_truncation_reason": None,
        "context_truncated": False,
        "context_truncation_reason": None,
        "limitations": GRAPH_CONTEXT_LIMITATIONS,
    }


def _direct_peer_sets(graph: nx.DiGraph, node: str) -> tuple[set[str], set[str], set[str]]:
    if node not in graph:
        return set(), set(), set()
    inbound = set(graph.predecessors(node))
    outbound = set(graph.successors(node))
    return inbound, outbound, inbound.intersection(outbound)


def _entity_summary(graph: nx.DiGraph, node: str, settings: Settings) -> dict[str, object]:
    inbound, outbound, bidirectional = _direct_peer_sets(graph, node)
    peers = sorted(inbound.union(outbound))
    limited_peers = peers[: settings.graph_comparison_max_peers_per_entity]
    return {
        "ip": node,
        "present": node in graph,
        "inbound_total": len(inbound),
        "outbound_total": len(outbound),
        "bidirectional_total": len(bidirectional),
        "total_peer_count": len(peers),
        "peers_retrieved": limited_peers,
        "peer_retrieved_count": len(limited_peers),
        "subnets": sorted({get_subnet(peer) for peer in peers}),
        "retrieval_truncated": len(limited_peers) < len(peers),
    }


def _comparison_context(graph: nx.DiGraph, spec: GraphRetrievalSpec, settings: Settings) -> dict[str, object]:
    entity_a = spec.entities[0].value
    entity_b = spec.entities[1].value
    a_summary = _entity_summary(graph, entity_a, settings)
    b_summary = _entity_summary(graph, entity_b, settings)
    a_inbound, a_outbound, _ = _direct_peer_sets(graph, entity_a)
    b_inbound, b_outbound, _ = _direct_peer_sets(graph, entity_b)
    a_peers = a_inbound.union(a_outbound)
    b_peers = b_inbound.union(b_outbound)
    shared = sorted(a_peers.intersection(b_peers))
    shared_limited = shared[: settings.graph_comparison_max_shared_peers]
    a_unique = sorted(a_peers.difference(b_peers))
    b_unique = sorted(b_peers.difference(a_peers))
    direct = _relationship_context(graph, spec)
    a_subnets = set(a_summary["subnets"])
    b_subnets = set(b_summary["subnets"])
    degree_comparison = {
        "entity_a_total_peer_count": a_summary["total_peer_count"],
        "entity_b_total_peer_count": b_summary["total_peer_count"],
        "entity_a_inbound_total": a_summary["inbound_total"],
        "entity_b_inbound_total": b_summary["inbound_total"],
        "entity_a_outbound_total": a_summary["outbound_total"],
        "entity_b_outbound_total": b_summary["outbound_total"],
        "entity_a_bidirectional_total": a_summary["bidirectional_total"],
        "entity_b_bidirectional_total": b_summary["bidirectional_total"],
        "higher_total_peer_entity": entity_a
        if int(a_summary["total_peer_count"]) > int(b_summary["total_peer_count"])
        else entity_b
        if int(b_summary["total_peer_count"]) > int(a_summary["total_peer_count"])
        else "tie",
        "broader_outbound_entity": entity_a
        if int(a_summary["outbound_total"]) > int(b_summary["outbound_total"])
        else entity_b
        if int(b_summary["outbound_total"]) > int(a_summary["outbound_total"])
        else "tie",
        "broader_inbound_entity": entity_a
        if int(a_summary["inbound_total"]) > int(b_summary["inbound_total"])
        else entity_b
        if int(b_summary["inbound_total"]) > int(a_summary["inbound_total"])
        else "tie",
    }
    subnet_comparison = {
        "entity_a_subnets": sorted(a_subnets),
        "entity_b_subnets": sorted(b_subnets),
        "shared_subnets": sorted(a_subnets.intersection(b_subnets)),
        "entity_a_unique_subnets": sorted(a_subnets.difference(b_subnets)),
        "entity_b_unique_subnets": sorted(b_subnets.difference(a_subnets)),
    }
    nodes = [
        _node_dict(node, target=entity_a, hop=0 if node in {entity_a, entity_b} else 1)
        for node in [entity_a, entity_b, *shared_limited]
        if node in graph
    ]
    edges = list(direct.get("edges") or [])
    retrieval_truncated = (
        bool(a_summary["retrieval_truncated"])
        or bool(b_summary["retrieval_truncated"])
        or len(shared_limited) < len(shared)
    )
    reasons = []
    if a_summary["retrieval_truncated"] or b_summary["retrieval_truncated"]:
        reasons.append(_reason("comparison_peer_limit", settings.graph_comparison_max_peers_per_entity, len(a_peers) + len(b_peers), len(a_summary["peers_retrieved"]) + len(b_summary["peers_retrieved"])))
    if len(shared_limited) < len(shared):
        reasons.append(_reason("comparison_shared_peer_limit", settings.graph_comparison_max_shared_peers, len(shared), len(shared_limited)))
    return {
        "target_ip": entity_a,
        "target_ips": [entity_a, entity_b],
        "node_found": bool(a_summary["present"] or b_summary["present"]),
        "scope": "multi_entity_comparison",
        "direction": "both",
        "depth": 1,
        "relationship_mode": "compare",
        "entities": [entity_a, entity_b],
        "entity_a": a_summary,
        "entity_b": b_summary,
        "direct_relationship": {
            "a_to_b": direct["forward_edge"],
            "b_to_a": direct["reverse_edge"],
            "relationship": direct["relationship"],
            "relationship_status": direct["relationship_status"],
            "bidirectional": direct["bidirectional"],
        },
        "degree_comparison": degree_comparison,
        "subnet_comparison": subnet_comparison,
        "shared_peer_total": len(shared),
        "shared_peers_retrieved": shared_limited,
        "shared_peers_retrieved_count": len(shared_limited),
        "entity_a_unique_peer_total": len(a_unique),
        "entity_b_unique_peer_total": len(b_unique),
        "candidate_node_count": 2 + len(shared),
        "retrieved_node_count": len(nodes),
        "returned_node_count": len(nodes),
        "included_node_count": 0,
        "context_node_count": 0,
        "candidate_edge_count": len(edges),
        "retrieved_edge_count": len(edges),
        "returned_edge_count": len(edges),
        "included_edge_count": 0,
        "context_edge_count": 0,
        "nodes": nodes,
        "edges": edges,
        "retrieval_truncated": retrieval_truncated,
        "retrieval_truncation_reasons": reasons,
        "retrieval_truncation_reason": _reason_string(reasons),
        "context_truncated": False,
        "context_truncation_reason": None,
        "limitations": GRAPH_CONTEXT_LIMITATIONS,
    }
