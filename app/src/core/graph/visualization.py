"""Visualization helpers for topology graphs."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import tempfile
from pathlib import Path
from typing import Any

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


def _filter_graph_by_subnet(graph: Any, subnet_filter: str):
    """Return a subgraph containing only valid IP nodes inside the CIDR filter."""
    selected = str(subnet_filter or "").strip()
    if not selected:
        return graph.copy()

    try:
        network = ipaddress.ip_network(selected, strict=False)
    except ValueError:
        logger.warning("event=graph_visualization_subnet_filter_invalid subnet_filter=%r", selected)
        return graph.subgraph([]).copy()

    nodes_to_keep = []
    for node in graph.nodes():
        try:
            node_ip = ipaddress.ip_address(str(node).strip())
        except ValueError:
            continue
        if node_ip.version == network.version and node_ip in network:
            nodes_to_keep.append(node)

    return graph.subgraph(nodes_to_keep).copy()


def generate_pyvis_graph(
    max_nodes: int = 200,
    min_degree: int = 0,
    subnet_filter: str = "",
    height: str = "700px",
    width: str = "100%",
    cdn_resources: str = "local",
    enable_node_click_bridge: bool = False,
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

    subgraph = _filter_graph_by_subnet(graph, subnet_filter)

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
        cdn_resources=cdn_resources,
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
        if enable_node_click_bridge:
            html = _inject_node_click_bridge(html)
        logger.info(
            "event=graph_visualization_completed rendered_nodes=%s rendered_edges=%s html_chars=%s",
            subgraph.number_of_nodes(),
            subgraph.number_of_edges(),
            len(html),
        )
        return html
    finally:
        html_path.unlink(missing_ok=True)


def _inject_node_click_bridge(html: str) -> str:
    """Add a tiny PyVis/vis-network click bridge for the Streamlit wrapper.

    PyVis exposes the rendered vis-network instance as a global ``network``
    variable. The bridge posts select/clear events to the parent iframe;
    Python validates selected node IDs before using them as Copilot context.
    """
    bridge_script = """
        <script type="text/javascript">
        (function () {
            var lastNode = null;
            var lastSentAt = 0;
            var graphSelectionEventCounter = 0;
            var lastViewportInteractionAt = 0;

            function nextEventId() {
                graphSelectionEventCounter += 1;
                return String(Date.now()) + "-" + String(graphSelectionEventCounter);
            }

            function markViewportInteraction() {
                lastViewportInteractionAt = Date.now();
            }

            function recentViewportInteraction() {
                return Date.now() - lastViewportInteractionAt < 250;
            }

            function sendNode(nodeId) {
                if (nodeId === null || nodeId === undefined) {
                    return;
                }

                var clickedNode = String(nodeId);
                var now = Date.now();
                if (clickedNode === lastNode && now - lastSentAt < 150) {
                    return;
                }

                lastNode = clickedNode;
                lastSentAt = now;
                window.parent.postMessage({
                    type: "soorin_graph_selection",
                    action: "select",
                    event_id: nextEventId(),
                    node: clickedNode
                }, "*");
            }

            function sendClearSelection() {
                var now = Date.now();
                if (lastNode === null && now - lastSentAt < 150) {
                    return;
                }

                lastNode = null;
                lastSentAt = now;
                if (window.network && typeof window.network.unselectAll === "function") {
                    window.network.unselectAll();
                }
                window.parent.postMessage({
                    type: "soorin_graph_selection",
                    action: "clear",
                    event_id: nextEventId(),
                    node: null
                }, "*");
            }

            function bindNetworkClickBridge() {
                if (window.__soorinNodeClickBridgeBound) {
                    return;
                }

                if (!window.network || typeof window.network.on !== "function") {
                    window.setTimeout(bindNetworkClickBridge, 100);
                    return;
                }

                window.__soorinNodeClickBridgeBound = true;
                window.network.on("click", function (params) {
                    if (params && params.nodes && params.nodes.length === 1) {
                        sendNode(params.nodes[0]);
                    } else if (params && params.nodes && params.nodes.length === 0 && !recentViewportInteraction()) {
                        sendClearSelection();
                    }
                });
                window.network.on("selectNode", function (params) {
                    if (params && params.nodes && params.nodes.length === 1) {
                        sendNode(params.nodes[0]);
                    }
                });
                window.network.on("dragStart", markViewportInteraction);
                window.network.on("dragging", markViewportInteraction);
                window.network.on("dragEnd", markViewportInteraction);
                window.network.on("zoom", markViewportInteraction);
            }

            bindNetworkClickBridge();
        }());
        </script>
    """
    body_index = html.lower().rfind("</body>")
    if body_index == -1:
        logger.warning("event=graph_click_bridge_injection_anchor_missing")
        return html + bridge_script
    return html[:body_index] + bridge_script + html[body_index:]
