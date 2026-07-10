"""Load and cache the network topology graph."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import networkx as nx

from src.config.settings import get_settings
from src.core.graph.storage import load_graph_pickle, resolve_path

_GRAPH_CACHE: Optional[nx.DiGraph] = None
_GRAPH_PATH: Optional[Path] = None
logger = logging.getLogger(__name__)


def set_graph_path(path: str | Path) -> None:
    """Set the path to the graph pickle file."""
    global _GRAPH_PATH
    _GRAPH_PATH = resolve_path(path)
    logger.info("event=graph_path_set path=%s", path)


def load_graph(force_reload: bool = False) -> nx.DiGraph:
    """Load the topology graph from pickle, with caching.

    Args:
        force_reload: If True, reload even if cached.

    Returns:
        The NetworkX directed graph.
    """
    global _GRAPH_CACHE

    if _GRAPH_CACHE is not None and not force_reload:
        logger.info(
            "event=graph_cache_hit nodes=%s edges=%s",
            _GRAPH_CACHE.number_of_nodes(),
            _GRAPH_CACHE.number_of_edges(),
        )
        return _GRAPH_CACHE

    graph_path = _GRAPH_PATH or resolve_path(get_settings().graph_pickle_path)
    logger.info("event=graph_load_started path=%s force_reload=%s", graph_path, force_reload)

    _GRAPH_CACHE = load_graph_pickle(graph_path)
    logger.info(
        "event=graph_load_completed nodes=%s edges=%s",
        _GRAPH_CACHE.number_of_nodes(),
        _GRAPH_CACHE.number_of_edges(),
    )

    return _GRAPH_CACHE


def get_graph() -> nx.DiGraph:
    """Get the cached graph or load it."""
    if _GRAPH_CACHE is not None:
        return _GRAPH_CACHE
    return load_graph()


def get_cached_graph() -> Optional[nx.DiGraph]:
    """Return the cached graph without loading the artifact."""
    return _GRAPH_CACHE


def is_graph_loaded() -> bool:
    """Check if the graph is loaded."""
    return _GRAPH_CACHE is not None
