"""Minimal Streamlit bridge for click-enabled topology graphs."""

from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components


_COMPONENT = components.declare_component(
    "soorin_topology_graph",
    path=str(Path(__file__).parent),
)


def topology_graph_component(
    *,
    html: str,
    height: int = 680,
    key: str | None = None,
) -> str | None:
    """Render graph HTML and return a clicked node ID when the browser emits one."""
    return _COMPONENT(html=html, height=height, default=None, key=key)
