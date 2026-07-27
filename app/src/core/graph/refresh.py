"""Safe product-topology refresh cycle for the active graph."""

from __future__ import annotations

import json
import logging
import pickle
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from src.config.settings import Settings
from src.core.graph.builder import build_graph_stats, build_topology_graph
from src.core.graph.loader import get_cached_graph, get_graph_metadata, load_graph, replace_active_graph, set_graph_path
from src.core.graph.storage import atomic_write_gexf, atomic_write_graphml, atomic_write_json, atomic_write_pickle, resolve_path
from src.core.product_client import ProductApiClient


logger = logging.getLogger(__name__)
_GLOBAL_REFRESH_SERVICE: GraphRefreshService | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class GraphRefreshStatus:
    enabled: bool
    running: bool
    interval_seconds: int
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_error_type: str | None = None
    last_error_message: str | None = None
    consecutive_failures: int = 0
    active_graph_loaded_at: str | None = None
    active_graph_source: str | None = None
    active_graph_nodes: int = 0
    active_graph_edges: int = 0
    active_graph_version: str | None = None
    raw_snapshot_path: str | None = None
    processed_snapshot_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class GraphRefreshResult:
    status: str
    activated: bool
    nodes: int
    edges: int
    raw_records: int
    processed_records: int
    snapshot_version: str
    raw_snapshot_path: str
    processed_snapshot_path: str
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class GraphRefreshError(RuntimeError):
    """Raised for refresh failures that must preserve last-known-good graph."""


class GraphRefreshService:
    """Fetch, validate, persist, and atomically activate product topology graphs."""

    def __init__(self, settings: Settings, product_client: ProductApiClient) -> None:
        self.settings = settings
        self.product_client = product_client
        self._lock = threading.Lock()
        self._status_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = GraphRefreshStatus(
            enabled=settings.graph_auto_refresh_enabled,
            running=False,
            interval_seconds=settings.graph_refresh_interval_seconds,
        )
        self._sync_active_metadata()

    def load_last_known_good(self) -> bool:
        """Load existing processed graph if available, without requiring product API."""
        if get_cached_graph() is not None:
            self._sync_active_metadata()
            return True
        graph_path = resolve_path(self.settings.graph_pickle_path)
        if not graph_path.exists():
            logger.warning("event=graph_last_known_good_missing path=%s", self.settings.graph_pickle_path)
            self._sync_active_metadata()
            return False
        try:
            set_graph_path(self.settings.graph_pickle_path)
            graph = load_graph(force_reload=True)
        except Exception as exc:
            logger.exception("event=graph_last_known_good_load_failed path=%s", self.settings.graph_pickle_path)
            self._record_failure(type(exc).__name__, "Unable to load last-known-good graph.")
            return False
        logger.info(
            "event=graph_last_known_good_loaded nodes=%s edges=%s path=%s",
            graph.number_of_nodes(),
            graph.number_of_edges(),
            self.settings.graph_pickle_path,
        )
        self._sync_active_metadata()
        return True

    def start_background(self) -> None:
        if not self.settings.graph_auto_refresh_enabled:
            logger.info("event=graph_refresh_disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="soorin-graph-refresh", daemon=True)
        self._thread.start()
        logger.info(
            "event=graph_refresh_scheduler_started interval_seconds=%s startup_delay_seconds=%s jitter_seconds=%s",
            self.settings.graph_refresh_interval_seconds,
            self.settings.graph_refresh_startup_delay_seconds,
            self.settings.graph_refresh_jitter_seconds,
        )

    def stop_background(self, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_seconds)
        logger.info("event=graph_refresh_scheduler_stopped")

    def refresh_once(self, *, force: bool = False, reason: str | None = None) -> GraphRefreshResult:
        metadata = get_graph_metadata()
        snapshot_age_seconds = self._snapshot_age_seconds(metadata.get("active_graph_loaded_at"))
        refresh_reason = "forced" if force else reason or ("no_active_snapshot" if get_cached_graph() is None else "manual_requested")
        logger.info(
            "event=graph_refresh_decision refresh_reason=%s snapshot_age_seconds=%s refresh_interval_seconds=%s active_snapshot_version=%s",
            refresh_reason,
            snapshot_age_seconds if snapshot_age_seconds is not None else "",
            self.settings.graph_refresh_interval_seconds,
            metadata.get("active_graph_version", ""),
        )
        active_graph = get_cached_graph()
        scheduled_check = reason in {"startup", "interval_elapsed"}
        if (
            not force
            and scheduled_check
            and active_graph is not None
            and snapshot_age_seconds is not None
            and snapshot_age_seconds < self.settings.graph_refresh_interval_seconds
        ):
            logger.info(
                "event=graph_refresh_reused refresh_reason=snapshot_fresh snapshot_age_seconds=%s refresh_interval_seconds=%s active_snapshot_version=%s",
                snapshot_age_seconds,
                self.settings.graph_refresh_interval_seconds,
                metadata.get("active_graph_version", ""),
            )
            return GraphRefreshResult(
                status="skipped",
                activated=False,
                nodes=active_graph.number_of_nodes(),
                edges=active_graph.number_of_edges(),
                raw_records=0,
                processed_records=0,
                snapshot_version=str(metadata.get("active_graph_version") or ""),
                raw_snapshot_path=str(metadata.get("raw_snapshot_path") or ""),
                processed_snapshot_path=str(metadata.get("processed_snapshot_path") or ""),
                message="active_snapshot_fresh",
            )
        if not self._lock.acquire(timeout=self.settings.graph_refresh_lock_timeout_seconds):
            logger.warning("event=graph_refresh_skipped_already_running")
            return GraphRefreshResult(
                status="skipped",
                activated=False,
                nodes=0,
                edges=0,
                raw_records=0,
                processed_records=0,
                snapshot_version="",
                raw_snapshot_path="",
                processed_snapshot_path="",
                message="refresh_already_running",
            )

        started = time.perf_counter()
        snapshot_version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self._record_attempt()
        try:
            logger.info("event=graph_refresh_started snapshot_version=%s force=%s", snapshot_version, force)
            topology = self.product_client.fetch_topology_unique_ip_pairs()
            logger.info(
                "event=graph_fetch_completed snapshot_version=%s raw_records=%s valid_records=%s latency_ms=%s",
                snapshot_version,
                topology.raw_record_count,
                len(topology.records),
                int(topology.elapsed_seconds * 1000),
            )
            if not topology.records:
                raise GraphRefreshError("Product topology response contained no valid graph records.")

            graph, processed_records = build_topology_graph(topology.records)
            stats = build_graph_stats(
                graph,
                raw_records_count=topology.raw_record_count,
                processed_edges=processed_records,
                fetch_duration_seconds=topology.elapsed_seconds,
                source_endpoint_path=topology.endpoint_path,
            )
            stats["snapshot_version"] = snapshot_version
            self._validate_graph(graph, force=force)
            logger.info(
                "event=graph_validation_completed snapshot_version=%s nodes=%s edges=%s",
                snapshot_version,
                graph.number_of_nodes(),
                graph.number_of_edges(),
            )

            raw_path, pickle_path = self._write_required_artifacts(topology.raw_payload, graph, stats)
            optional_failures = (
                self._write_optional_exports(graph)
                if self.settings.graph_optional_exports_enabled
                else []
            )
            if optional_failures:
                logger.warning(
                    "event=graph_optional_exports_failed snapshot_version=%s failures=%s",
                    snapshot_version,
                    ",".join(optional_failures),
                )
            raw_snapshot_path, processed_snapshot_path = self._write_snapshots(topology.raw_payload, graph, snapshot_version)
            self._prune_snapshots(
                resolve_path(self.settings.graph_raw_path),
                "*.snapshot.*.json",
                self.settings.graph_refresh_keep_raw_snapshots,
                self.settings.graph_snapshot_ttl_hours,
            )
            self._prune_snapshots(
                resolve_path(self.settings.graph_pickle_path),
                "*.snapshot.*.pkl",
                self.settings.graph_refresh_keep_processed_snapshots,
                self.settings.graph_snapshot_ttl_hours,
            )

            metadata = replace_active_graph(
                graph,
                {
                    "active_graph_source": "product_refresh",
                    "active_graph_version": snapshot_version,
                    "raw_snapshot_path": str(raw_snapshot_path),
                    "processed_snapshot_path": str(processed_snapshot_path),
                },
            )
            self._record_success(metadata, raw_snapshot_path, processed_snapshot_path)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "event=graph_activated snapshot_version=%s nodes=%s edges=%s elapsed_ms=%s raw_path=%s pickle_path=%s",
                snapshot_version,
                graph.number_of_nodes(),
                graph.number_of_edges(),
                elapsed_ms,
                raw_path,
                pickle_path,
            )
            return GraphRefreshResult(
                status="ok",
                activated=True,
                nodes=graph.number_of_nodes(),
                edges=graph.number_of_edges(),
                raw_records=topology.raw_record_count,
                processed_records=processed_records,
                snapshot_version=snapshot_version,
                raw_snapshot_path=str(raw_snapshot_path),
                processed_snapshot_path=str(processed_snapshot_path),
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            error_type = type(exc).__name__
            self._record_failure(error_type, str(exc)[:220])
            logger.warning(
                "event=graph_refresh_failed snapshot_version=%s error_type=%s elapsed_ms=%s active_snapshot_preserved=%s message=%s",
                snapshot_version,
                error_type,
                elapsed_ms,
                get_cached_graph() is not None,
                str(exc)[:160],
            )
            return GraphRefreshResult(
                status="error",
                activated=False,
                nodes=0,
                edges=0,
                raw_records=0,
                processed_records=0,
                snapshot_version=snapshot_version,
                raw_snapshot_path="",
                processed_snapshot_path="",
                message=error_type,
            )
        finally:
            with self._status_lock:
                self._status.running = False
            self._lock.release()

    def status(self) -> dict[str, object]:
        self._sync_active_metadata()
        with self._status_lock:
            return self._status.to_dict()

    def _run_loop(self) -> None:
        initial_delay = self.settings.graph_refresh_startup_delay_seconds
        if self.settings.graph_refresh_jitter_seconds > 0:
            initial_delay += random.uniform(0, self.settings.graph_refresh_jitter_seconds)
        if self.settings.graph_refresh_on_startup and self._stop_event.wait(initial_delay):
            return
        if self.settings.graph_refresh_on_startup and not self._stop_event.is_set():
            self.refresh_once(reason="startup")
        while not self._stop_event.wait(self.settings.graph_refresh_interval_seconds):
            self.refresh_once(reason="interval_elapsed")

    @staticmethod
    def _snapshot_age_seconds(loaded_at: object) -> int | None:
        if not loaded_at:
            return None
        try:
            loaded = datetime.fromisoformat(str(loaded_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if loaded.tzinfo is None:
            loaded = loaded.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - loaded).total_seconds()))

    def _validate_graph(self, graph: nx.DiGraph, *, force: bool) -> None:
        if not isinstance(graph, nx.DiGraph):
            raise GraphRefreshError("Graph builder returned an invalid graph type.")
        if graph.number_of_nodes() < self.settings.graph_refresh_min_nodes:
            raise GraphRefreshError("Graph node count is below configured minimum.")
        if graph.number_of_edges() < self.settings.graph_refresh_min_edges:
            raise GraphRefreshError("Graph edge count is below configured minimum.")
        for node in graph.nodes:
            if not isinstance(node, str) or not node.strip():
                raise GraphRefreshError("Graph contains an invalid node identifier.")
        node_set = set(graph.nodes)
        for source, target in graph.edges:
            if source not in node_set or target not in node_set:
                raise GraphRefreshError("Graph contains an edge with invalid endpoints.")

        previous = get_cached_graph()
        if force or previous is None:
            return
        previous_nodes = previous.number_of_nodes()
        previous_edges = previous.number_of_edges()
        if previous_nodes and graph.number_of_nodes() < previous_nodes * (1 - self.settings.graph_refresh_max_node_drop_ratio):
            raise GraphRefreshError("Graph node count dropped suspiciously compared with last-known-good graph.")
        if previous_edges and graph.number_of_edges() < previous_edges * (1 - self.settings.graph_refresh_max_edge_drop_ratio):
            raise GraphRefreshError("Graph edge count dropped suspiciously compared with last-known-good graph.")

    def _write_required_artifacts(self, raw_payload: Any, graph: nx.DiGraph, stats: dict[str, object]) -> tuple[Path, Path]:
        raw_path = resolve_path(self.settings.graph_raw_path)
        stats_path = resolve_path(self.settings.graph_stats_path)
        pickle_path = resolve_path(self.settings.graph_pickle_path)
        for path in (raw_path, stats_path, pickle_path):
            path.parent.mkdir(parents=True, exist_ok=True)

        temp_paths = [
            raw_path.with_name(f"{raw_path.name}.{uuid.uuid4().hex}.tmp"),
            stats_path.with_name(f"{stats_path.name}.{uuid.uuid4().hex}.tmp"),
            pickle_path.with_name(f"{pickle_path.name}.{uuid.uuid4().hex}.tmp"),
        ]
        temp_raw, temp_stats, temp_pickle = temp_paths
        try:
            temp_raw.write_text(json.dumps(raw_payload, indent=2), encoding="utf-8")
            temp_stats.write_text(json.dumps(stats, indent=2), encoding="utf-8")
            with temp_pickle.open("wb") as handle:
                pickle.dump(graph, handle)
            for temp_path, final_path in ((temp_raw, raw_path), (temp_stats, stats_path), (temp_pickle, pickle_path)):
                temp_path.replace(final_path)
        except Exception:
            for temp_path in temp_paths:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)
            raise
        logger.info(
            "event=graph_artifacts_written nodes=%s edges=%s raw_path=%s stats_path=%s pickle_path=%s",
            graph.number_of_nodes(),
            graph.number_of_edges(),
            self.settings.graph_raw_path,
            self.settings.graph_stats_path,
            self.settings.graph_pickle_path,
        )
        return raw_path, pickle_path

    def _write_optional_exports(self, graph: nx.DiGraph) -> list[str]:
        failures: list[str] = []
        for name, writer, path in (
            ("graphml", atomic_write_graphml, self.settings.graph_graphml_path),
            ("gexf", atomic_write_gexf, self.settings.graph_gexf_path),
        ):
            try:
                writer(graph, path)
            except Exception:
                logger.exception("event=graph_optional_export_failed export=%s path=%s", name, path)
                failures.append(name)
        return failures

    def _write_snapshots(self, raw_payload: Any, graph: nx.DiGraph, snapshot_version: str) -> tuple[Path, Path]:
        raw_path = resolve_path(self.settings.graph_raw_path)
        pickle_path = resolve_path(self.settings.graph_pickle_path)
        raw_snapshot = raw_path.with_name(f"{raw_path.stem}.snapshot.{snapshot_version}{raw_path.suffix}")
        pickle_snapshot = pickle_path.with_name(f"{pickle_path.stem}.snapshot.{snapshot_version}{pickle_path.suffix}")
        if self.settings.graph_refresh_keep_raw_snapshots > 0:
            atomic_write_json(raw_payload, raw_snapshot)
        if self.settings.graph_refresh_keep_processed_snapshots > 0:
            atomic_write_pickle(graph, pickle_snapshot)
        return raw_snapshot, pickle_snapshot

    @staticmethod
    def _prune_snapshots(
        anchor_path: Path,
        pattern: str,
        keep: int,
        ttl_hours: int,
    ) -> None:
        """Prune only regular snapshot files beside the configured active artifact."""
        try:
            snapshots = [
                path
                for path in anchor_path.parent.glob(pattern)
                if path.is_file() and not path.is_symlink()
            ]
            snapshots.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            cutoff = time.time() - ttl_hours * 3600 if ttl_hours > 0 else None
            retained = 0
            for snapshot in snapshots:
                expired = cutoff is not None and snapshot.stat().st_mtime < cutoff
                over_count = keep <= 0 or retained >= keep
                if expired or over_count:
                    snapshot.unlink(missing_ok=True)
                    logger.info("event=graph_snapshot_pruned path_kind=graph_snapshot")
                else:
                    retained += 1
        except OSError as exc:
            logger.warning(
                "event=graph_snapshot_prune_failed path_kind=graph_snapshot error_type=%s",
                type(exc).__name__,
            )

    def _record_attempt(self) -> None:
        with self._status_lock:
            self._status.running = True
            self._status.last_attempt_at = _utc_now()
            self._status.enabled = self.settings.graph_auto_refresh_enabled
            self._status.interval_seconds = self.settings.graph_refresh_interval_seconds

    def _record_success(self, metadata: dict[str, Any], raw_snapshot_path: Path, processed_snapshot_path: Path) -> None:
        with self._status_lock:
            self._status.running = False
            self._status.last_success_at = _utc_now()
            self._status.last_error_type = None
            self._status.last_error_message = None
            self._status.consecutive_failures = 0
            self._status.raw_snapshot_path = str(raw_snapshot_path)
            self._status.processed_snapshot_path = str(processed_snapshot_path)
            self._status.active_graph_loaded_at = str(metadata.get("active_graph_loaded_at") or "")
            self._status.active_graph_source = str(metadata.get("active_graph_source") or "")
            self._status.active_graph_nodes = int(metadata.get("active_graph_nodes") or 0)
            self._status.active_graph_edges = int(metadata.get("active_graph_edges") or 0)
            self._status.active_graph_version = str(metadata.get("active_graph_version") or "")

    def _record_failure(self, error_type: str, message: str) -> None:
        with self._status_lock:
            self._status.running = False
            self._status.last_failure_at = _utc_now()
            self._status.last_error_type = error_type
            self._status.last_error_message = message[:220]
            self._status.consecutive_failures += 1

    def _sync_active_metadata(self) -> None:
        metadata = get_graph_metadata()
        cached = get_cached_graph()
        with self._status_lock:
            self._status.active_graph_loaded_at = metadata.get("active_graph_loaded_at") if metadata else None
            self._status.active_graph_source = metadata.get("active_graph_source") if metadata else None
            self._status.active_graph_version = str(metadata.get("active_graph_version") or "") if metadata else None
            self._status.raw_snapshot_path = metadata.get("raw_snapshot_path") if metadata else self._status.raw_snapshot_path
            self._status.processed_snapshot_path = metadata.get("processed_snapshot_path") if metadata else self._status.processed_snapshot_path
            self._status.active_graph_nodes = cached.number_of_nodes() if cached else 0
            self._status.active_graph_edges = cached.number_of_edges() if cached else 0


def set_graph_refresh_service(service: GraphRefreshService | None) -> None:
    global _GLOBAL_REFRESH_SERVICE
    _GLOBAL_REFRESH_SERVICE = service


def get_graph_refresh_service() -> GraphRefreshService | None:
    return _GLOBAL_REFRESH_SERVICE


def get_refresh_status() -> dict[str, object]:
    service = get_graph_refresh_service()
    if service is None:
        return {
            "enabled": False,
            "running": False,
            "interval_seconds": 0,
        }
    return service.status()
