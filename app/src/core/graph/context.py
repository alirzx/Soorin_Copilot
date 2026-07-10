"""Compact graph context digests for future Copilot use."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from src.core.graph.loader import get_graph
from src.core.graph.service import get_subnet


logger = logging.getLogger(__name__)

GRAPH_CONTEXT_LIMITATIONS = [
    "Edges represent observed unique IP communication pairs.",
    "Current topology records do not prove packet routing or reachability.",
    "Port, protocol, byte, process, and frequency data may be unavailable.",
]


@dataclass(frozen=True)
class GraphContextDigest:
    """Model-ready graph evidence for a single IP, not yet wired into Copilot."""

    target_ip: str
    node_found: bool
    degree: dict[str, int]
    top_inbound_peers: list[str]
    top_outbound_peers: list[str]
    bidirectional_peers: list[str]
    subnets_reached: list[str]
    limitations: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_graph_context(ip: str, *, neighbor_limit: int = 20) -> dict[str, object]:
    graph = get_graph()
    target_ip = ip.strip()
    neighbor_limit = min(100, max(1, int(neighbor_limit or 20)))

    if target_ip not in graph:
        logger.info("event=graph_context_digest_created target_ip=%s node_found=false", target_ip)
        return GraphContextDigest(
            target_ip=target_ip,
            node_found=False,
            degree={"in": 0, "out": 0, "total": 0},
            top_inbound_peers=[],
            top_outbound_peers=[],
            bidirectional_peers=[],
            subnets_reached=[],
            limitations=GRAPH_CONTEXT_LIMITATIONS,
        ).to_dict()

    inbound = sorted(graph.predecessors(target_ip), key=lambda peer: graph[peer][target_ip].get("weight", 1), reverse=True)
    outbound = sorted(graph.successors(target_ip), key=lambda peer: graph[target_ip][peer].get("weight", 1), reverse=True)
    bidirectional = sorted(set(inbound).intersection(outbound))
    reached_subnets = sorted({get_subnet(peer) for peer in outbound})

    logger.info(
        "event=graph_context_digest_created target_ip=%s node_found=true inbound=%s outbound=%s bidirectional=%s subnets=%s",
        target_ip,
        len(inbound),
        len(outbound),
        len(bidirectional),
        len(reached_subnets),
    )
    return GraphContextDigest(
        target_ip=target_ip,
        node_found=True,
        degree={
            "in": graph.in_degree(target_ip),
            "out": graph.out_degree(target_ip),
            "total": graph.degree(target_ip),
        },
        top_inbound_peers=inbound[:neighbor_limit],
        top_outbound_peers=outbound[:neighbor_limit],
        bidirectional_peers=bidirectional[:neighbor_limit],
        subnets_reached=reached_subnets,
        limitations=GRAPH_CONTEXT_LIMITATIONS,
    ).to_dict()
