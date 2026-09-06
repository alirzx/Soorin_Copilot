"""Neo4j-backed topology page served through the authenticated Graph API."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from collections.abc import Collection, Mapping
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import streamlit as st
from pyvis.network import Network

from src.config.settings import get_settings
from src.web.components.topology_graph import NO_GRAPH_SELECTION_EVENT, topology_graph_component
from src.web.local_simulation import copilot_auth_headers


logger = logging.getLogger(__name__)
SELECTED_COPILOT_IP_KEY = "selected_copilot_ip"
LAST_GRAPH_SELECTION_EVENT_KEY = "topology_graph_last_selection_event_id"
TOPOLOGY_GRAPH_VERSION_KEY = "topology_graph_snapshot_version"

_SUBNET_COLORS = {
    "192.168.0.": "#FF6B6B",
    "192.168.21.": "#4ECDC4",
    "192.168.30.": "#45B7D1",
    "192.168.23.": "#96CEB4",
    "192.168.20.": "#FFEAA7",
    "10.": "#DDA0DD",
    "172.": "#98D8C8",
}


class GraphApiError(RuntimeError):
    """A safe, user-facing failure from the authenticated Graph API."""


def _graph_api_get(settings: Any, path: str, *, params: Mapping[str, object] | None = None) -> dict[str, Any]:
    try:
        response = requests.get(
            f"{settings.api_base_url.rstrip('/')}{path}",
            headers=copilot_auth_headers(settings.copilot_api_key),
            params=params,
            timeout=min(10, settings.api_timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            raise GraphApiError("Graph API authentication failed.") from exc
        raise GraphApiError("Graph API returned an error.") from exc
    except requests.Timeout as exc:
        raise GraphApiError("Graph API request timed out.") from exc
    except requests.ConnectionError as exc:
        raise GraphApiError("Graph API is not reachable.") from exc
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        raise GraphApiError("Graph API returned an invalid response.") from exc
    if not isinstance(payload, dict):
        raise GraphApiError("Graph API returned an invalid response.")
    return payload


def fetch_graph_status(settings: Any) -> dict[str, Any]:
    """Read the small published-projection status signal from the API."""
    return _graph_api_get(settings, "/graph/status")


def _should_reload_graph_snapshot(known_version: str | None, active_version: str | None) -> bool:
    """Return whether the API-backed topology component must be remounted."""
    return bool(active_version and active_version != known_version)


def _fetch_active_graph_version(settings: Any) -> str | None:
    """Keep the version helper small for callers that only need invalidation."""
    try:
        payload = fetch_graph_status(settings)
    except GraphApiError as exc:
        logger.warning("event=ui_graph_snapshot_status_unavailable error_type=%s", type(exc).__name__)
        return None
    if not payload.get("loaded"):
        return None
    version = str(payload.get("active_graph_version") or "").strip()
    return version or None


def _fetch_topology(settings: Any, *, max_nodes: int, min_degree: int, subnet: str) -> dict[str, Any]:
    payload = _graph_api_get(
        settings,
        "/graph/topology",
        params={"max_nodes": max_nodes, "min_degree": min_degree, "subnet": subnet},
    )
    if not isinstance(payload.get("nodes"), list) or not isinstance(payload.get("edges"), list):
        raise GraphApiError("Graph API returned an invalid topology response.")
    return payload


def _fetch_node(settings: Any, ip: str) -> dict[str, Any]:
    return _graph_api_get(settings, f"/graph/nodes/{ip}")


def _topology_node_ids(topology: Mapping[str, Any]) -> set[str]:
    return {
        str(node.get("ip") or "").strip()
        for node in topology.get("nodes") or []
        if isinstance(node, Mapping) and str(node.get("ip") or "").strip()
    }


def _retained_selected_graph_ip(selected_ip: str | None, nodes: Collection[str]) -> str | None:
    """Keep a selection only while the published projection still contains it."""
    normalized = str(selected_ip or "").strip()
    return normalized if normalized and normalized in nodes else None


def build_copilot_ui_context(selected_ip: str | None) -> dict[str, str] | None:
    """Build optional Copilot UI context from the current topology selection."""
    normalized = str(selected_ip or "").strip()
    return {"selected_ip": normalized} if normalized else None


def _validated_clicked_graph_ip(clicked_node: str | None, nodes: Collection[str]) -> str | None:
    if not clicked_node:
        return None
    node = str(clicked_node).strip()
    try:
        parsed_ip = ip_address(node)
    except ValueError:
        logger.warning("event=ui_graph_node_click_rejected reason=invalid_ip clicked_node=%r", node)
        return None
    if parsed_ip.version != 4:
        logger.warning("event=ui_graph_node_click_rejected reason=not_ipv4 clicked_node=%r", node)
        return None
    normalized_ip = str(parsed_ip)
    if normalized_ip not in nodes:
        logger.warning("event=ui_graph_node_click_rejected reason=node_not_in_topology clicked_node=%r", node)
        return None
    return normalized_ip


def _resolve_graph_selection_event(selection_event: object, nodes: Collection[str]) -> tuple[str, str | None, str]:
    """Normalize a component event into one of: none, select, or clear."""
    if selection_event == NO_GRAPH_SELECTION_EVENT:
        return "none", None, ""
    if selection_event is None:
        return "clear", None, ""
    if isinstance(selection_event, dict):
        action = str(selection_event.get("action") or "").strip().lower()
        event_id = str(selection_event.get("event_id") or "").strip()
        if action == "clear":
            return "clear", None, event_id
        if action == "select":
            selected_ip = _validated_clicked_graph_ip(selection_event.get("node"), nodes)
            return ("select", selected_ip, event_id) if selected_ip else ("none", None, event_id)
        return "none", None, event_id
    selected_ip = _validated_clicked_graph_ip(str(selection_event), nodes)
    return ("select", selected_ip, "") if selected_ip else ("none", None, "")


def _subnet_for_ip(ip: str) -> str:
    parsed = ip_address(ip)
    prefix = 24 if parsed.version == 4 else 64
    return str(ip_network(f"{parsed}/{prefix}", strict=False))


def _color_for_ip(ip: str) -> str:
    for prefix, color in _SUBNET_COLORS.items():
        if ip.startswith(prefix):
            return color
    hue = int(hashlib.md5(_subnet_for_ip(ip).encode()).hexdigest()[:6], 16) % 360
    return f"hsl({hue}, 60%, 65%)"


def _node_size(degree: int, max_degree: int) -> int:
    return 10 if max_degree <= 0 else int(10 + (degree / max_degree) * 40)


def _inject_node_click_bridge(html: str) -> str:
    """Bridge bounded PyVis node clicks to the existing Streamlit component."""
    bridge_script = """
        <script type="text/javascript">
        (function () {
            var lastNode = null, lastSentAt = 0, counter = 0, lastViewportInteractionAt = 0;
            function eventId() { counter += 1; return String(Date.now()) + "-" + String(counter); }
            function moved() { lastViewportInteractionAt = Date.now(); }
            function recentlyMoved() { return Date.now() - lastViewportInteractionAt < 250; }
            function send(action, node) {
                var now = Date.now();
                if (node === lastNode && now - lastSentAt < 150) { return; }
                lastNode = node; lastSentAt = now;
                window.parent.postMessage({type: "soorin_graph_selection", action: action, event_id: eventId(), node: node}, "*");
            }
            function bind() {
                if (window.__soorinNodeClickBridgeBound) { return; }
                if (!window.network || typeof window.network.on !== "function") { window.setTimeout(bind, 100); return; }
                window.__soorinNodeClickBridgeBound = true;
                window.network.on("click", function (params) {
                    if (params && params.nodes && params.nodes.length === 1) { send("select", String(params.nodes[0])); }
                    else if (params && params.nodes && params.nodes.length === 0 && !recentlyMoved()) {
                        if (typeof window.network.unselectAll === "function") { window.network.unselectAll(); }
                        send("clear", null);
                    }
                });
                window.network.on("selectNode", function (params) { if (params && params.nodes && params.nodes.length === 1) { send("select", String(params.nodes[0])); } });
                window.network.on("dragStart", moved); window.network.on("dragging", moved); window.network.on("dragEnd", moved); window.network.on("zoom", moved);
            }
            bind();
        }());
        </script>
    """
    body_index = html.lower().rfind("</body>")
    return html + bridge_script if body_index == -1 else html[:body_index] + bridge_script + html[body_index:]


def build_topology_html(topology: Mapping[str, Any]) -> str:
    """Render API-normalized topology records without loading a local graph."""
    node_ids = _topology_node_ids(topology)
    degrees = {
        str(node.get("ip")): int(node.get("degree") or 0)
        for node in topology.get("nodes") or []
        if isinstance(node, Mapping) and str(node.get("ip") or "") in node_ids
    }
    max_degree = max(degrees.values(), default=1)
    network = Network(height="650px", width="100%", directed=True, notebook=False, bgcolor="#ffffff", font_color="#333333", cdn_resources="in_line")
    network.set_options("""{"physics":{"solver":"forceAtlas2Based","forceAtlas2Based":{"gravitationalConstant":-80,"centralGravity":0.01,"springLength":120,"springConstant":0.08,"damping":0.4},"maxVelocity":50,"minVelocity":0.1,"stabilization":{"enabled":true,"iterations":200}},"interaction":{"hover":true,"tooltipDelay":200,"zoomView":true,"dragView":true},"edges":{"color":{"color":"#cccccc","highlight":"#ff6b6b","hover":"#45b7d1"},"smooth":{"type":"continuous"}}}""")
    for ip, degree in sorted(degrees.items()):
        network.add_node(ip, label=ip, title=f"<b>{ip}</b><br>Subnet: {_subnet_for_ip(ip)}<br>Degree: {degree}", color=_color_for_ip(ip), size=_node_size(degree, max_degree), borderWidth=1, borderWidthSelected=3)
    for edge in topology.get("edges") or []:
        if not isinstance(edge, Mapping):
            continue
        source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
        if source in node_ids and target in node_ids:
            weight = max(1, int(edge.get("weight") or 1))
            network.add_edge(source, target, title=f"Weight: {weight}", width=min(weight / 2, 3))
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as handle:
        html_path = Path(handle.name)
    try:
        network.save_graph(str(html_path))
        return _inject_node_click_bridge(html_path.read_text(encoding="utf-8"))
    finally:
        html_path.unlink(missing_ok=True)


def _subnet_summaries(topology: Mapping[str, Any]) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for ip in _topology_node_ids(topology):
        try:
            subnet = _subnet_for_ip(ip)
        except ValueError:
            continue
        counts[subnet] = counts.get(subnet, 0) + 1
    return [{"subnet": subnet, "count": count} for subnet, count in sorted(counts.items())]


def _clear_missing_selected_ip(settings: Any, selected_ip: str | None, *, version_changed: bool) -> bool:
    if not selected_ip or not version_changed:
        return False
    try:
        exists = bool(_fetch_node(settings, selected_ip).get("found"))
    except GraphApiError as exc:
        logger.warning("event=ui_graph_selection_validation_unavailable error_type=%s", type(exc).__name__)
        return False
    return not exists


@st.fragment(run_every="30s")
def show_topology_page(*, embedded: bool = False) -> None:
    """Display the bounded topology projected by the authenticated Graph API."""
    if embedded:
        st.subheader("Live Topology")
    else:
        st.title("Network Topology")
    st.caption("Observed communication relationships. Paths do not prove routed packet paths.")
    st.caption(f"Selected topology target: {st.session_state.get(SELECTED_COPILOT_IP_KEY) or 'none'}")
    settings = get_settings()
    try:
        status = fetch_graph_status(settings)
    except GraphApiError as exc:
        st.warning(str(exc))
        return
    if not status.get("loaded"):
        st.warning("Graph is not available. Wait for the published Neo4j projection to become ready.")
        return
    snapshot_version = str(status.get("active_graph_version") or "").strip() or None
    known_version = st.session_state.get(TOPOLOGY_GRAPH_VERSION_KEY)
    version_changed = _should_reload_graph_snapshot(known_version, snapshot_version)
    if version_changed:
        st.session_state[TOPOLOGY_GRAPH_VERSION_KEY] = snapshot_version
        logger.info("event=ui_graph_snapshot_reloaded provider=graph_api version=%s", snapshot_version)
    selected_ip = st.session_state.get(SELECTED_COPILOT_IP_KEY)
    if _clear_missing_selected_ip(settings, selected_ip, version_changed=version_changed):
        st.session_state[SELECTED_COPILOT_IP_KEY] = None
        logger.info("event=ui_graph_selection_cleared source=published_projection_change")
        st.rerun(scope="app")
    ui_max_nodes = max(1, int(settings.graph_max_ui_nodes))
    ui_min_nodes = min(50, ui_max_nodes)
    default_min_degree = max(0, int(settings.graph_default_min_degree))
    try:
        seed_topology = _fetch_topology(settings, max_nodes=ui_max_nodes, min_degree=default_min_degree, subnet="")
    except GraphApiError as exc:
        st.warning(str(exc))
        return
    subnet_info = _subnet_summaries(seed_topology)
    tab_graph, tab_explore, tab_path, tab_nodes = st.tabs(["📊 Interactive Graph", "🔍 Explore IP", "🛤️ Find Path", "📋 All Nodes"])
    with tab_graph:
        st.subheader("Interactive Network Topology")
        col1, col2, col3 = st.columns(3)
        with col1:
            max_nodes = st.slider("Max nodes to display", min_value=ui_min_nodes, max_value=ui_max_nodes, value=ui_max_nodes, step=max(1, min(50, ui_max_nodes)), help="Bounded by SOORIN_GRAPH_MAX_UI_NODES.")
        with col2:
            min_degree = st.slider("Minimum degree", min_value=0, max_value=20, value=min(20, default_min_degree), step=1, help="Only show nodes with at least this many connections.")
        with col3:
            subnet_options = ["All Subnets"] + [str(item["subnet"]) for item in subnet_info]
            subnet_filter = st.selectbox("Filter by subnet", options=subnet_options, help="Show only nodes in a specific subnet.")
        subnet_value = "" if subnet_filter == "All Subnets" else subnet_filter
        try:
            topology = seed_topology if (max_nodes == ui_max_nodes and min_degree == int(seed_topology.get("min_degree") or 0) and not subnet_value) else _fetch_topology(settings, max_nodes=max_nodes, min_degree=min_degree, subnet=subnet_value)
            stats = _graph_api_get(settings, "/graph/stats")
        except GraphApiError as exc:
            st.warning(str(exc))
            return
        node_ids = _topology_node_ids(topology)
        with st.spinner("Rendering bounded Neo4j topology..."):
            html_graph = build_topology_html(topology)
        selection_event = topology_graph_component(html=html_graph, height=680, key=f"topology_graph_{snapshot_version or 'unknown'}_{max_nodes}_{min_degree}_{subnet_value or 'all'}")
        selection_action, clicked_ip, selection_event_id = _resolve_graph_selection_event(selection_event, node_ids)
        if selection_event_id and st.session_state.get(LAST_GRAPH_SELECTION_EVENT_KEY) == selection_event_id:
            selection_action = "none"
        if selection_action == "clear":
            if selection_event_id:
                st.session_state[LAST_GRAPH_SELECTION_EVENT_KEY] = selection_event_id
            if st.session_state.get(SELECTED_COPILOT_IP_KEY):
                st.session_state[SELECTED_COPILOT_IP_KEY] = None
                logger.info("event=ui_graph_selection_cleared session_id=%s source=graph_background_click", st.session_state.get("session_id", ""))
                st.rerun()
        elif selection_action == "select" and clicked_ip and clicked_ip != st.session_state.get(SELECTED_COPILOT_IP_KEY):
            if selection_event_id:
                st.session_state[LAST_GRAPH_SELECTION_EVENT_KEY] = selection_event_id
            previous_ip = st.session_state.get(SELECTED_COPILOT_IP_KEY) or ""
            st.session_state[SELECTED_COPILOT_IP_KEY] = clicked_ip
            logger.info("event=ui_investigation_target_updated session_id=%s previous_ip=%s selected_ip=%s source=graph_node_click", st.session_state.get("session_id", ""), previous_ip, clicked_ip)
            st.rerun()
        st.divider()
        st.subheader("Legend")
        legend_cols = st.columns(4)
        for index, info in enumerate(subnet_info[:8]):
            subnet = str(info["subnet"])
            sample_ip = str(ip_network(subnet, strict=False).network_address)
            legend_cols[index % 4].markdown(f'<span style="display:inline-block;width:12px;height:12px;background:{_color_for_ip(sample_ip)};border-radius:2px;margin-right:6px;"></span><b>{subnet}</b> ({info["count"]} IPs)', unsafe_allow_html=True)
        st.divider()
        metric_1, metric_2, metric_3, metric_4 = st.columns(4)
        metric_1.metric("Total IPs", int(stats.get("total_nodes") or 0))
        metric_2.metric("Total Connections", int(stats.get("total_edges") or 0))
        metric_3.metric("Avg Degree", stats.get("avg_degree") or 0)
        metric_4.metric("Subnets", len(subnet_info))
        if snapshot_version:
            st.caption(f"Published graph version: {snapshot_version}")
    with tab_explore:
        st.subheader("Explore an IP")
        ip_input = st.text_input("Enter IP address", placeholder="e.g., 192.168.0.149", key="explore_ip")
        if ip_input:
            started = time.perf_counter()
            try:
                neighbors = _graph_api_get(settings, f"/graph/nodes/{ip_input.strip()}/neighbors", params={"direction": "both", "limit": min(50, int(settings.graph_api_max_neighbors))})
            except GraphApiError as exc:
                st.error(str(exc)); neighbors = None
            latency_ms = int((time.perf_counter() - started) * 1000)
            if neighbors is not None and not neighbors.get("found"):
                st.error(f"IP {ip_input.strip()} not found in graph")
            elif neighbors is not None:
                records = [record for record in neighbors.get("neighbors") or [] if isinstance(record, Mapping)]
                outgoing = [str(record.get("ip")) for record in records if record.get("direction") == "out"]
                incoming = [str(record.get("ip")) for record in records if record.get("direction") == "in"]
                logger.info("event=ui_graph_node_inspection target_ip=%s node_found=true neighbor_count=%s latency_ms=%s", ip_input.strip(), neighbors.get("total", 0), latency_ms)
                if st.button("Use as Copilot target", width="stretch"):
                    previous_ip = st.session_state.get(SELECTED_COPILOT_IP_KEY) or ""
                    st.session_state[SELECTED_COPILOT_IP_KEY] = ip_input.strip()
                    logger.info("event=ui_investigation_target_updated session_id=%s previous_ip=%s selected_ip=%s source=topology_explore", st.session_state.get("session_id", ""), previous_ip, ip_input.strip())
                    st.rerun()
                metric_1, metric_2, metric_3 = st.columns(3)
                metric_1.metric("Total Degree", int(neighbors.get("total") or 0)); metric_2.metric("Outgoing", len(outgoing)); metric_3.metric("Incoming", len(incoming))
                list_1, list_2 = st.columns(2)
                with list_1:
                    st.write("**Outgoing Connections**")
                    st.dataframe(pd.DataFrame(outgoing, columns=["IP"]), width="stretch", height=300) if outgoing else st.caption("None")
                with list_2:
                    st.write("**Incoming Connections**")
                    st.dataframe(pd.DataFrame(incoming, columns=["IP"]), width="stretch", height=300) if incoming else st.caption("None")
    with tab_path:
        st.subheader("Find Path Between IPs")
        source_column, target_column = st.columns(2)
        with source_column:
            source = st.text_input("Source IP", placeholder="e.g., 192.168.30.115", key="path_src")
        with target_column:
            target = st.text_input("Destination IP", placeholder="e.g., 192.168.0.125", key="path_dst")
        if source and target and st.button("Find Shortest Path", width="stretch"):
            try:
                result = _graph_api_get(settings, "/graph/path", params={"source": source, "target": target})
            except GraphApiError as exc:
                st.error(str(exc))
            else:
                if result.get("found"):
                    st.success(f"Path found: {int(result.get('edge_count') or 0)} hops")
                    st.write(" → ".join(str(ip) for ip in result.get("path") or []))
                else:
                    st.error(str(result.get("reason") or "No observed communication-graph path found."))
    with tab_nodes:
        st.subheader("Bounded Topology Nodes")
        rows = [{"IP": str(node.get("ip")), "Degree": int(node.get("degree") or 0)} for node in topology.get("nodes") or [] if isinstance(node, Mapping)]
        st.caption(f"Showing {len(rows)} nodes from the bounded active topology response.")
        st.dataframe(pd.DataFrame(rows), width="stretch", height=500)
