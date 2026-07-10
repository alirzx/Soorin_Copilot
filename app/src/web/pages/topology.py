"""Network Topology page for Streamlit — with interactive PyVis graph."""

from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd

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


def show_topology_page() -> None:
    """Display the network topology analysis page."""
    st.title("🌐 Network Topology")
    st.write("Interactive visualization of the real network topology graph.")

    settings = get_settings()
    try:
        G = load_graph()
    except FileNotFoundError:
        st.warning("Graph is not available. Fetch topology data before opening this page.")
        return

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
            )

        # Display
        components.html(html_graph, height=680, scrolling=False)

        # Legend
        st.divider()
        st.subheader("Legend")

        legend_cols = st.columns(4)
        subnet_info = get_subnet_list()
        for i, info in enumerate(subnet_info[:8]):
            col = legend_cols[i % 4]
            subnet = info["subnet"]
            count = info["count"]
            sample_ip = next(
                (n for n in G.nodes() if n.startswith(subnet.rstrip("."))),
                subnet + "0",
            )
            color = get_color(sample_ip)
            col.markdown(
                f'<span style="display:inline-block;width:12px;height:12px;'
                f'background:{color};border-radius:2px;margin-right:6px;"></span>'
                f'<b>{subnet}</b> ({count} IPs)',
                unsafe_allow_html=True,
            )

    # ================================================================
    # TAB 1: OVERVIEW STATS
    # ================================================================
    # (Moved stats into the graph tab as metrics)
    with tab_graph:
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
            neighbors = get_neighbors(ip_input)

            if "error" in neighbors:
                st.error(neighbors["error"])
            else:
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
                            use_container_width=True,
                            height=300,
                        )
                    else:
                        st.caption("None")

                with col2:
                    st.write("**Incoming Connections**")
                    if neighbors["incoming"]:
                        st.dataframe(
                            pd.DataFrame(neighbors["incoming"], columns=["IP"]),
                            use_container_width=True,
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

        if src and dst and st.button("Find Shortest Path", use_container_width=True):
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
        st.dataframe(df_nodes, use_container_width=True, height=500)
