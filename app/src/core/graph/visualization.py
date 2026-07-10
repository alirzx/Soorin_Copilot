"""Visualization helpers for topology graphs."""

from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path

from pyvis.network import Network

from src.core.graph.loader import get_graph
from src.core.graph.service import get_subnet


logger = logging.getLogger(__name__)

SUBNET_COLORS = {
    "192.168.0.": "#FF6B6B",
    "192.168.21.": "#4ECDC4",
    "192.168.30.": "#45B7D1",
    "192.168.23.": "#96CEB4",
    "192.168.20.": "#FFEAA7",
    "10.": "#DDA0DD",
    "172.": "#98D8C8",
}


def get_color(ip: str) -> str:
    """Get a stable color for an IP based on subnet."""
    subnet = get_subnet(ip)
    for prefix, color in SUBNET_COLORS.items():
        if ip.startswith(prefix):
            return color
    hash_val = int(hashlib.md5(subnet.encode()).hexdigest()[:6], 16)
    hue = hash_val % 360
    return f"hsl({hue}, 60%, 65%)"


def _get_node_size(degree: int, max_degree: int) -> int:
    if max_degree == 0:
        return 10
    normalized = degree / max_degree
    return int(10 + normalized * 40)


def generate_pyvis_graph(
    max_nodes: int = 200,
    min_degree: int = 0,
    subnet_filter: str = "",
    height: str = "700px",
    width: str = "100%",
) -> str:
    """Generate an interactive PyVis graph visualization as HTML.

    Visualization stays separate from deterministic graph query services.
    """
    graph = get_graph()
    logger.info(
        "event=graph_visualization_started nodes=%s edges=%s max_nodes=%s min_degree=%s subnet_filter=%s",
        graph.number_of_nodes(),
        graph.number_of_edges(),
        max_nodes,
        min_degree,
        subnet_filter or "all",
    )

    if subnet_filter:
        nodes_to_keep = [node for node in graph.nodes() if node.startswith(subnet_filter)]
        subgraph = graph.subgraph(nodes_to_keep).copy()
    else:
        subgraph = graph.copy()

    if min_degree > 0:
        nodes_to_keep = [node for node in subgraph.nodes() if subgraph.degree(node) >= min_degree]
        subgraph = subgraph.subgraph(nodes_to_keep).copy()

    if subgraph.number_of_nodes() > max_nodes:
        top_nodes = sorted(subgraph.degree(), key=lambda item: item[1], reverse=True)[:max_nodes]
        subgraph = subgraph.subgraph([node for node, _ in top_nodes]).copy()

    net = Network(
        height=height,
        width=width,
        directed=True,
        notebook=False,
        bgcolor="#ffffff",
        font_color="#333333",
    )
    net.set_options("""
    {
        "physics": {
            "solver": "forceAtlas2Based",
            "forceAtlas2Based": {
                "gravitationalConstant": -80,
                "centralGravity": 0.01,
                "springLength": 120,
                "springConstant": 0.08,
                "damping": 0.4
            },
            "maxVelocity": 50,
            "minVelocity": 0.1,
            "stabilization": {
                "enabled": true,
                "iterations": 200
            }
        },
        "interaction": {
            "hover": true,
            "tooltipDelay": 200,
            "zoomView": true,
            "dragView": true
        },
        "edges": {
            "color": {
                "color": "#cccccc",
                "highlight": "#ff6b6b",
                "hover": "#45b7d1"
            },
            "smooth": {
                "type": "continuous"
            }
        }
    }
    """)

    degrees = dict(subgraph.degree())
    max_degree = max(degrees.values()) if degrees else 1

    for node in subgraph.nodes():
        degree = degrees.get(node, 0)
        subnet = get_subnet(node)
        net.add_node(
            node,
            label=node,
            title=f"<b>{node}</b><br>Subnet: {subnet}<br>Degree: {degree}",
            color=get_color(node),
            size=_get_node_size(degree, max_degree),
            borderWidth=1,
            borderWidthSelected=3,
        )

    for src, dst in subgraph.edges():
        weight = subgraph[src][dst].get("weight", 1)
        net.add_edge(src, dst, title=f"Weight: {weight}", width=min(weight / 2, 3))

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as handle:
        html_path = Path(handle.name)

    try:
        net.save_graph(str(html_path))
        html = html_path.read_text(encoding="utf-8")
        logger.info(
            "event=graph_visualization_completed rendered_nodes=%s rendered_edges=%s html_chars=%s",
            subgraph.number_of_nodes(),
            subgraph.number_of_edges(),
            len(html),
        )
        return html
    finally:
        html_path.unlink(missing_ok=True)
