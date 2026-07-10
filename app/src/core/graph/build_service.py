"""Application-level graph rebuild orchestration."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from src.config.settings import Settings
from src.core.graph.builder import build_graph_stats, build_topology_graph
from src.core.graph.loader import load_graph, set_graph_path
from src.core.graph.storage import save_graph_artifacts, save_json
from src.core.product_client import ProductApiClient


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphBuildResult:
    status: str
    nodes: int
    edges: int
    processed_records: int
    raw_records: int
    artifact_updated: bool
    graph_reloaded: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class GraphBuildService:
    """Coordinate the build plane without owning HTTP, transform, or storage rules."""

    def __init__(self, settings: Settings, product_client: ProductApiClient) -> None:
        self.settings = settings
        self.product_client = product_client
        logger.info(
            "event=graph_build_service_initialized raw_path=%s pickle_path=%s",
            settings.graph_raw_path,
            settings.graph_pickle_path,
        )

    def rebuild_from_product(self) -> GraphBuildResult:
        logger.info("event=graph_rebuild_started")
        topology = self.product_client.fetch_topology_unique_ip_pairs()
        logger.info(
            "event=graph_rebuild_product_fetch_completed raw_records=%s valid_records=%s status_code=%s",
            topology.raw_record_count,
            len(topology.records),
            topology.status_code,
        )

        graph, processed_records = build_topology_graph(topology.records)
        stats = build_graph_stats(
            graph,
            raw_records_count=topology.raw_record_count,
            processed_edges=processed_records,
            fetch_duration_seconds=topology.elapsed_seconds,
            source_endpoint_path=topology.endpoint_path,
        )
        logger.info(
            "event=graph_rebuild_build_completed nodes=%s edges=%s processed_records=%s",
            graph.number_of_nodes(),
            graph.number_of_edges(),
            processed_records,
        )

        save_json(topology.raw_payload, self.settings.graph_raw_path)
        save_json(stats, self.settings.graph_stats_path)
        save_graph_artifacts(graph, self.settings)
        logger.info(
            "event=graph_rebuild_artifact_saved nodes=%s edges=%s",
            graph.number_of_nodes(),
            graph.number_of_edges(),
        )

        set_graph_path(self.settings.graph_pickle_path)
        reloaded = load_graph(force_reload=True)
        logger.info(
            "event=graph_rebuild_cache_reloaded nodes=%s edges=%s",
            reloaded.number_of_nodes(),
            reloaded.number_of_edges(),
        )

        result = GraphBuildResult(
            status="ok",
            nodes=graph.number_of_nodes(),
            edges=graph.number_of_edges(),
            processed_records=processed_records,
            raw_records=topology.raw_record_count,
            artifact_updated=True,
            graph_reloaded=True,
        )
        logger.info(
            "event=graph_rebuild_completed status=%s nodes=%s edges=%s processed_records=%s",
            result.status,
            result.nodes,
            result.edges,
            result.processed_records,
        )
        return result
