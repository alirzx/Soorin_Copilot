"""Load and cache the network topology graph."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import networkx as nx

from src.config.settings import get_settings
from src.core.graph.storage import load_graph_pickle, resolve_path

_GRAPH_CACHE: Optional[nx.DiGraph] = None
_GRAPH_PATH: Optional[Path] = None
_GRAPH_METADATA: dict[str, Any] = {}
_GRAPH_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


def set_graph_path(path: str | Path) -> None:
    """Set the path to the graph pickle file."""
    global _GRAPH_PATH
    with _GRAPH_LOCK:
        _GRAPH_PATH = resolve_path(path)
    logger.info("event=graph_path_set path=%s", path)


def load_graph(force_reload: bool = False) -> nx.DiGraph:
    """Load the topology graph from pickle, with caching.

    Args:
        force_reload: If True, reload even if cached.

    Returns:
        The NetworkX directed graph.
    """
    global _GRAPH_CACHE, _GRAPH_METADATA

    with _GRAPH_LOCK:
        if _GRAPH_CACHE is not None and not force_reload:
            logger.info(
                "event=graph_cache_hit nodes=%s edges=%s",
                _GRAPH_CACHE.number_of_nodes(),
                _GRAPH_CACHE.number_of_edges(),
            )
            return _GRAPH_CACHE

        graph_path = _GRAPH_PATH or resolve_path(get_settings().graph_pickle_path)
        logger.info("event=graph_load_started path=%s force_reload=%s", graph_path, force_reload)

        graph = load_graph_pickle(graph_path)
        _GRAPH_CACHE = graph
        _GRAPH_METADATA = {
            "active_graph_loaded_at": datetime.now(timezone.utc).isoformat(),
            "active_graph_source": "artifact",
            "active_graph_version": str(graph_path.stat().st_mtime_ns) if graph_path.exists() else "",
            "active_graph_nodes": graph.number_of_nodes(),
            "active_graph_edges": graph.number_of_edges(),
            "processed_snapshot_path": str(graph_path),
        }
        logger.info(
            "event=graph_load_completed nodes=%s edges=%s source=artifact",
            _GRAPH_CACHE.number_of_nodes(),
            _GRAPH_CACHE.number_of_edges(),
        )

        return _GRAPH_CACHE


def get_graph() -> nx.DiGraph:
    """Get the cached graph or load it."""
    with _GRAPH_LOCK:
        if _GRAPH_CACHE is not None:
            return _GRAPH_CACHE
    return load_graph()


def get_cached_graph() -> Optional[nx.DiGraph]:
    """Return the cached graph without loading the artifact."""
    with _GRAPH_LOCK:
        return _GRAPH_CACHE


def is_graph_loaded() -> bool:
    """Check if the graph is loaded."""
    with _GRAPH_LOCK:
        return _GRAPH_CACHE is not None


def replace_active_graph(graph: nx.DiGraph, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Atomically swap the in-process active graph after external validation."""
    global _GRAPH_CACHE, _GRAPH_METADATA
    with _GRAPH_LOCK:
        _GRAPH_CACHE = graph
        _GRAPH_METADATA = {
            "active_graph_loaded_at": datetime.now(timezone.utc).isoformat(),
            "active_graph_source": "product_refresh",
            "active_graph_nodes": graph.number_of_nodes(),
            "active_graph_edges": graph.number_of_edges(),
            **(metadata or {}),
        }
        logger.info(
            "event=graph_active_replaced nodes=%s edges=%s source=%s version=%s",
            graph.number_of_nodes(),
            graph.number_of_edges(),
            _GRAPH_METADATA.get("active_graph_source", ""),
            _GRAPH_METADATA.get("active_graph_version", ""),
        )
        return dict(_GRAPH_METADATA)


def get_graph_metadata() -> dict[str, Any]:
    """Return safe metadata for the currently active graph."""
    with _GRAPH_LOCK:
        return dict(_GRAPH_METADATA)
