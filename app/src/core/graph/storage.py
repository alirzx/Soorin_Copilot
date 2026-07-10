"""Storage helpers for topology graph artifacts."""

from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path
from typing import Any

import networkx as nx

from src.config.settings import APP_DIR, Settings


PROJECT_ROOT = APP_DIR.parent
logger = logging.getLogger(__name__)


def resolve_path(path: str | Path) -> Path:
    path_obj = Path(path)
    if path_obj.is_absolute():
        return path_obj
    return PROJECT_ROOT / path_obj


def save_json(payload: Any, path: str | Path) -> Path:
    output_path = resolve_path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("event=graph_json_saved path=%s bytes=%s", path, output_path.stat().st_size)
    return output_path


def save_graph_artifacts(graph: nx.DiGraph, settings: Settings) -> dict[str, Path]:
    pickle_path = resolve_path(settings.graph_pickle_path)
    graphml_path = resolve_path(settings.graph_graphml_path)
    gexf_path = resolve_path(settings.graph_gexf_path)

    pickle_path.parent.mkdir(parents=True, exist_ok=True)
    graphml_path.parent.mkdir(parents=True, exist_ok=True)
    gexf_path.parent.mkdir(parents=True, exist_ok=True)

    with pickle_path.open("wb") as handle:
        pickle.dump(graph, handle)
    nx.write_graphml(graph, graphml_path)
    nx.write_gexf(graph, gexf_path)
    logger.info(
        "event=graph_artifacts_saved nodes=%s edges=%s pickle=%s graphml=%s gexf=%s",
        graph.number_of_nodes(),
        graph.number_of_edges(),
        settings.graph_pickle_path,
        settings.graph_graphml_path,
        settings.graph_gexf_path,
    )

    return {
        "pickle": pickle_path,
        "graphml": graphml_path,
        "gexf": gexf_path,
    }


def load_graph_pickle(path: str | Path) -> nx.DiGraph:
    """Load a trusted local graph pickle artifact.

    Pickle is intentionally only used for local artifacts produced by our fetch
    CLI. Do not load untrusted pickle files from users or external services.
    """
    graph_path = resolve_path(path)
    if not graph_path.exists():
        raise FileNotFoundError(f"Graph file not found at {graph_path}")
    logger.info("event=graph_pickle_load_started path=%s", path)
    with graph_path.open("rb") as handle:
        graph = pickle.load(handle)
    logger.info(
        "event=graph_pickle_load_completed path=%s nodes=%s edges=%s",
        path,
        graph.number_of_nodes(),
        graph.number_of_edges(),
    )
    return graph
