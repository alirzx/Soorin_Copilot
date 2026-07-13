"""Minimal Streamlit bridge for click-enabled topology graphs."""

from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components


_COMPONENT = components.declare_component(
    "soorin_topology_graph",
    path=str(Path(__file__).parent),
)

NO_GRAPH_SELECTION_EVENT = "__soorin_graph_selection_no_event__"


def topology_graph_component(
    *,
    html: str,
    height: int = 680,
    key: str | None = None,
) -> object:
    """Render graph HTML and return a graph selection event when one is emitted."""
    return _COMPONENT(html=html, height=height, default=NO_GRAPH_SELECTION_EVENT, key=key)
