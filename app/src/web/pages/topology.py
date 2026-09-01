"""Network Topology page for Streamlit — with interactive PyVis graph."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
from typing import Any
import requests
import streamlit as st
import pandas as pd
import logging
import time

from src.config.settings import get_settings
from src.core.graph.loader import load_graph
from src.core.graph.service import (
    get_stats,
    get_neighbors,
    get_path,
    get_node_list,
    get_subnet_list,
)
from src.core.graph.visualization import generate_pyvis_graph, get_color
from src.web.components.topology_graph import NO_GRAPH_SELECTION_EVENT, topology_graph_component
from src.web.local_simulation import copilot_auth_headers

logger = logging.getLogger(__name__)
SELECTED_COPILOT_IP_KEY = "selected_copilot_ip"
LAST_GRAPH_SELECTION_EVENT_KEY = "topology_graph_last_selection_event_id"
TOPOLOGY_GRAPH_VERSION_KEY = "topology_graph_snapshot_version"


def _should_reload_graph_snapshot(
    known_version: str | None,
    active_version: str | None,
) -> bool:
    """Return whether the local UI graph must be reloaded for an API version."""
    return bool(active_version and active_version != known_version)


def _fetch_active_graph_version(settings: Any) -> str | None:
    """Read the small authoritative graph-version signal without transferring graph data."""
    try:
        response = requests.get(
            f"{settings.api_base_url.rstrip('/')}/graph/status",
            headers=copilot_auth_headers(settings.copilot_api_key),
            timeout=min(10, settings.api_timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        logger.warning("event=ui_graph_snapshot_status_unavailable error_type=%s", type(exc).__name__)
        return None

    if not payload.get("loaded"):
        return None
    version = str(payload.get("active_graph_version") or "").strip()
    return version or None


def _sync_graph_snapshot(settings: Any) -> str | None:
    """Reload the shared local artifact only after the API publishes a new version."""
    active_version = _fetch_active_graph_version(settings)
    known_version = st.session_state.get(TOPOLOGY_GRAPH_VERSION_KEY)
    if _should_reload_graph_snapshot(known_version, active_version):
        load_graph(force_reload=True)
        st.session_state[TOPOLOGY_GRAPH_VERSION_KEY] = active_version
        logger.info("event=ui_graph_snapshot_reloaded version=%s", active_version)
    return active_version


def _retained_selected_graph_ip(selected_ip: str | None, graph: Any) -> str | None:
    """Keep a selection only while it remains in the refreshed graph."""
    normalized = str(selected_ip or "").strip()
    return normalized if normalized and normalized in graph else None


def build_copilot_ui_context(selected_ip: str | None) -> dict[str, str] | None:
    """Build optional Copilot UI context from the current topology selection."""
    normalized = str(selected_ip or "").strip()
    if not normalized:
        return None
    return {"selected_ip": normalized}


def _validated_clicked_graph_ip(clicked_node: str | None, graph: Any) -> str | None:
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
    if normalized_ip not in graph:
        logger.warning("event=ui_graph_node_click_rejected reason=node_not_in_graph clicked_node=%r", node)
        return None

    return normalized_ip


def _resolve_graph_selection_event(selection_event: object, graph: Any) -> tuple[str, str | None, str]:
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
            selected_ip = _validated_clicked_graph_ip(selection_event.get("node"), graph)
            return ("select", selected_ip, event_id) if selected_ip else ("none", None, event_id)
        return "none", None, event_id

    selected_ip = _validated_clicked_graph_ip(str(selection_event), graph)
    return ("select", selected_ip, "") if selected_ip else ("none", None, "")


@st.fragment(run_every="30s")
def show_topology_page(*, embedded: bool = False) -> None:
    """Display the network topology analysis page."""
    if embedded:
        st.subheader("Live Topology")
    else:
        st.title("Network Topology")
    st.caption("Observed communication relationships. Paths do not prove routed packet paths.")
    if st.session_state.get(SELECTED_COPILOT_IP_KEY):
        st.caption(f"Selected topology target: {st.session_state[SELECTED_COPILOT_IP_KEY]}")
    else:
        st.caption("Selected topology target: none")

    settings = get_settings()
    try:
        snapshot_version = _sync_graph_snapshot(settings)
        G = load_graph()
    except FileNotFoundError:
        st.warning("Graph is not available. Fetch topology data before opening this page.")
        return

    selected_ip = st.session_state.get(SELECTED_COPILOT_IP_KEY)
    retained_selected_ip = _retained_selected_graph_ip(selected_ip, G)
    if selected_ip and retained_selected_ip is None:
        st.session_state[SELECTED_COPILOT_IP_KEY] = None
        logger.info("event=ui_graph_selection_cleared source=snapshot_refresh")
        st.rerun(scope="app")

    # ---- Tabs ----
    tab_graph, tab_explore, tab_path, tab_nodes = st.tabs(
        ["📊 Interactive Graph", "🔍 Explore IP", "🛤️ Find Path", "📋 All Nodes"]
    )

    # ================================================================
    # TAB 0: INTERACTIVE GRAPH
    # ================================================================
    with tab_graph:
        st.subheader("Interactive Network Topology")

        # Filters
        col1, col2, col3 = st.columns(3)
        with col1:
            max_nodes = st.slider(
                "Max nodes to display",
                min_value=50,
                max_value=max(400, settings.graph_max_ui_nodes),
                value=settings.graph_max_ui_nodes,
                step=50,
                help="Limit nodes for performance. Higher = more complete, slower.",
            )
        with col2:
            min_degree = st.slider(
                "Minimum degree",
                min_value=0,
                max_value=20,
                value=settings.graph_default_min_degree,
                step=1,
                help="Only show nodes with at least this many connections.",
            )
        with col3:
            subnet_list = get_subnet_list()
            subnet_options = ["All Subnets"] + [s["subnet"] for s in subnet_list]
            subnet_filter = st.selectbox(
                "Filter by subnet",
                options=subnet_options,
                help="Show only nodes in a specific subnet.",
            )

        subnet_value = "" if subnet_filter == "All Subnets" else subnet_filter

        # Generate graph
        with st.spinner("Generating interactive graph..."):
            html_graph = generate_pyvis_graph(
                max_nodes=max_nodes,
                min_degree=min_degree,
                subnet_filter=subnet_value,
                height="650px",
                width="100%",
                cdn_resources="in_line",
                enable_node_click_bridge=True,
            )

        # Display
        selection_event = topology_graph_component(
            html=html_graph,
            height=680,
            key=f"topology_graph_{snapshot_version or 'unknown'}_{max_nodes}_{min_degree}_{subnet_value or 'all'}",
        )
        selection_action, clicked_ip, selection_event_id = _resolve_graph_selection_event(selection_event, G)
        if (
            selection_event_id
            and st.session_state.get(LAST_GRAPH_SELECTION_EVENT_KEY) == selection_event_id
        ):
            selection_action = "none"

        if selection_action == "clear":
            if selection_event_id:
                st.session_state[LAST_GRAPH_SELECTION_EVENT_KEY] = selection_event_id
            previous_ip = st.session_state.get(SELECTED_COPILOT_IP_KEY) or ""
            if previous_ip:
                logger.info(
                    "event=ui_graph_selection_cleared session_id=%s previous_ip=%s source=graph_background_click",
                    st.session_state.get("session_id", ""),
                    previous_ip,
                )
                st.session_state[SELECTED_COPILOT_IP_KEY] = None
                st.rerun()
        elif selection_action == "select" and clicked_ip:
            if selection_event_id:
                st.session_state[LAST_GRAPH_SELECTION_EVENT_KEY] = selection_event_id
            if clicked_ip == st.session_state.get(SELECTED_COPILOT_IP_KEY):
                pass
            else:
                previous_ip = st.session_state.get(SELECTED_COPILOT_IP_KEY) or ""
                logger.info(
                    "event=ui_graph_node_clicked session_id=%s clicked_node=%s",
                    st.session_state.get("session_id", ""),
                    clicked_ip,
                )
                st.session_state[SELECTED_COPILOT_IP_KEY] = clicked_ip
                logger.info(
                    "event=ui_investigation_target_updated session_id=%s previous_ip=%s selected_ip=%s source=graph_node_click",
                    st.session_state.get("session_id", ""),
                    previous_ip,
                    clicked_ip,
                )
                st.rerun()

        # Legend
        st.divider()
        st.subheader("Legend")

        legend_cols = st.columns(4)
        subnet_info = get_subnet_list()
        for i, info in enumerate(subnet_info[:8]):
            col = legend_cols[i % 4]
            subnet = info["subnet"]
            count = info["count"]
            try:
                sample_network = ip_network(str(subnet).strip(), strict=False)
                sample_ip = str(
                    sample_network.network_address + 1
                    if sample_network.num_addresses > 1
                    else sample_network.network_address
                )
            except (ValueError, TypeError):
                sample_ip = subnet
            color = get_color(sample_ip)
            col.markdown(
                f'<span style="display:inline-block;width:12px;height:12px;'
                f'background:{color};border-radius:2px;margin-right:6px;"></span>'
                f'<b>{subnet}</b> ({count} IPs)',
                unsafe_allow_html=True,
            )

        st.divider()
        stats = get_stats()

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total IPs", stats["total_nodes"])
        with col2:
            st.metric("Total Connections", stats["total_edges"])
        with col3:
            st.metric("Avg Degree", stats["avg_degree"])
        with col4:
            st.metric("Subnets", len(get_subnet_list()))

    # ================================================================
    # TAB 2: EXPLORE IP
    # ================================================================
    with tab_explore:
        st.subheader("Explore an IP")

        ip_input = st.text_input(
            "Enter IP address",
            placeholder="e.g., 192.168.0.149",
            key="explore_ip",
        )

        if ip_input:
            started = time.perf_counter()
            neighbors = get_neighbors(ip_input)
            latency_ms = int((time.perf_counter() - started) * 1000)

            if "error" in neighbors:
                st.error(neighbors["error"])
                logger.info(
                    "event=ui_graph_node_inspection target_ip=%s node_found=false neighbor_count=0 latency_ms=%s",
                    ip_input.strip(),
                    latency_ms,
                )
            else:
                logger.info(
                    "event=ui_graph_node_inspection target_ip=%s node_found=true neighbor_count=%s latency_ms=%s",
                    ip_input.strip(),
                    neighbors["total_degree"],
                    latency_ms,
                )
                if st.button("Use as Copilot target", width="stretch"):
                    previous_ip = st.session_state.get(SELECTED_COPILOT_IP_KEY) or ""
                    st.session_state[SELECTED_COPILOT_IP_KEY] = ip_input.strip()
                    logger.info(
                        "event=ui_investigation_target_updated session_id=%s previous_ip=%s selected_ip=%s source=topology_explore",
                        st.session_state.get("session_id", ""),
                        previous_ip,
                        ip_input.strip(),
                    )
                    st.rerun()

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Total Degree", neighbors["total_degree"])
                with col2:
                    st.metric("Outgoing", neighbors["outgoing_count"])
                with col3:
                    st.metric("Incoming", neighbors["incoming_count"])

                col1, col2 = st.columns(2)
                with col1:
                    st.write("**Outgoing Connections**")
                    if neighbors["outgoing"]:
                        st.dataframe(
                            pd.DataFrame(neighbors["outgoing"], columns=["IP"]),
                            width="stretch",
                            height=300,
                        )
                    else:
                        st.caption("None")

                with col2:
                    st.write("**Incoming Connections**")
                    if neighbors["incoming"]:
                        st.dataframe(
                            pd.DataFrame(neighbors["incoming"], columns=["IP"]),
                            width="stretch",
                            height=300,
                        )
                    else:
                        st.caption("None")

    # ================================================================
    # TAB 3: FIND PATH
    # ================================================================
    with tab_path:
        st.subheader("Find Path Between IPs")

        col1, col2 = st.columns(2)
        with col1:
            src = st.text_input(
                "Source IP", placeholder="e.g., 192.168.30.115", key="path_src"
            )
        with col2:
            dst = st.text_input(
                "Destination IP", placeholder="e.g., 192.168.0.125", key="path_dst"
            )

        if src and dst and st.button("Find Shortest Path", width="stretch"):
            result = get_path(src, dst)

            if "error" in result:
                st.error(result["error"])
            else:
                st.success(f"Path found: {result['hops']} hops")
                st.write(" → ".join(result["path"]))

    # ================================================================
    # TAB 4: ALL NODES
    # ================================================================
    with tab_nodes:
        st.subheader("All Nodes")

        page = st.number_input("Page", min_value=1, value=1, step=1)
        node_list = get_node_list(page=page, page_size=50)

        st.caption(f"Showing {len(node_list['nodes'])} of {node_list['total']} nodes")

        df_nodes = pd.DataFrame(node_list["nodes"])
        df_nodes.index = range(
            (page - 1) * 50 + 1, (page - 1) * 50 + len(df_nodes) + 1
        )
        st.dataframe(df_nodes, width="stretch", height=500)
