"""Neo4j-backed graph query service for topology analysis."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.config.settings import Settings, get_settings
from src.core.graph.neo4j import Neo4jDriver, Neo4jGraphRepository, Neo4jUnavailable
from src.core.graph.refresh import get_refresh_status
from src.core.graph.retrieval import GraphRetrievalSpec
from src.core.graph.structured import (
    AssetAggregateRequest,
    AssetAggregateResult,
    AssetSearchRequest,
    AssetSearchResult,
)


@dataclass(frozen=True)
class GraphStatus:
    loaded: bool
    nodes: int
    edges: int
    directed: bool
    artifact_available: bool
    active_graph_loaded_at: str | None = None
    active_graph_source: str | None = None
    active_graph_version: str | None = None
    refresh_enabled: bool = False
    refresh_running: bool = False
    refresh_interval_seconds: int = 0
    refresh_last_attempt_at: str | None = None
    refresh_last_success_at: str | None = None
    refresh_last_failure_at: str | None = None
    refresh_last_error_type: str | None = None
    refresh_last_error_message: str | None = None
    refresh_consecutive_failures: int = 0
    raw_snapshot_path: str | None = None
    last_known_good: bool = False


class GraphService:
    """Controlled graph facade over the active Neo4j projection."""

    _STRUCTURED_CURSOR_VERSION = 1

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.driver = Neo4jDriver(self.settings)
        self.repository = Neo4jGraphRepository(self.driver, self.settings)

    def status(self) -> GraphStatus:
        refresh = get_refresh_status()
        try:
            projection = self.repository.status()
        except Neo4jUnavailable:
            projection = None
        common = {
            "active_graph_loaded_at": projection.last_successful_sync if projection else None,
            "active_graph_source": "neo4j_projection" if projection and projection.active_graph_version else None,
            "active_graph_version": projection.active_graph_version if projection else None,
            "refresh_enabled": bool(refresh.get("enabled", False)),
            "refresh_running": bool(refresh.get("running", False)),
            "refresh_interval_seconds": int(refresh.get("interval_seconds", 0) or 0),
            "refresh_last_attempt_at": refresh.get("last_attempt_at"),
            "refresh_last_success_at": refresh.get("last_success_at"),
            "refresh_last_failure_at": refresh.get("last_failure_at"),
            "refresh_last_error_type": refresh.get("last_error_type"),
            "refresh_last_error_message": refresh.get("last_error_message"),
            "refresh_consecutive_failures": int(refresh.get("consecutive_failures", 0) or 0),
            "raw_snapshot_path": refresh.get("raw_snapshot_path"),
        }
        if projection is None or projection.active_graph_version is None:
            return GraphStatus(False, 0, 0, True, False, last_known_good=False, **common)
        return GraphStatus(True, projection.nodes, projection.edges, True, False, last_known_good=True, **common)

    def stats(self) -> dict[str, Any]:
        return self.repository.stats()

    def semantic_catalog_version(self) -> str | None:
        return self.repository.semantic_catalog_version()

    def semantic_catalog_values(
        self,
        active_graph_version: str,
        *,
        per_field_limit: int,
    ) -> dict[str, tuple[str, ...]]:
        return self.repository.semantic_catalog_values(
            active_graph_version,
            per_field_limit=per_field_limit,
        )

    def context(self, spec: GraphRetrievalSpec) -> dict[str, object]:
        context = self.repository.get_context(spec)
        projection = self.repository.status()
        return {**context, "graph_provider": "neo4j_projection", "active_graph_version": projection.active_graph_version, "graph_last_successful_sync": projection.last_successful_sync}

    def search_assets(self, request: AssetSearchRequest) -> AssetSearchResult:
        """Validate bounds and expose a cursor bound to query identity and graph version.

        The repository cursor remains an internal keyset position.  The service
        envelope prevents callers from replaying that position with different
        filters/sort semantics or after the active topology projection changes.
        """
        limit = self.repository.policy.structured_limit(request.limit)
        repository_cursor: str | None = None
        expected_graph_version: str | None = None
        if request.cursor is not None:
            repository_cursor, expected_graph_version = self._decode_structured_cursor(
                request.cursor,
                request,
            )

        repository_request = request.model_copy(
            update={"limit": limit, "cursor": repository_cursor}
        )
        result = self.repository.search_assets(repository_request)

        if (
            expected_graph_version is not None
            and result.active_graph_version != expected_graph_version
        ):
            raise ValueError(
                "Structured Asset search cursor was created for a different active graph version."
            )

        if result.next_cursor is None:
            return result
        if result.active_graph_version is None:
            raise ValueError(
                "Structured Asset search returned a cursor without an active graph version."
            )
        return result.model_copy(
            update={
                "next_cursor": self._encode_structured_cursor(
                    request,
                    graph_version=result.active_graph_version,
                    repository_cursor=result.next_cursor,
                )
            }
        )

    def aggregate_assets(self, request: AssetAggregateRequest) -> AssetAggregateResult:
        """Validate aggregate bounds before entering the Neo4j repository."""
        if request.operation.value == "group_count":
            limit = self.repository.policy.structured_limit(request.limit)
            request = request.model_copy(update={"limit": limit})
        return self.repository.aggregate_assets(request)

    @staticmethod
    def _structured_request_fingerprint(request: AssetSearchRequest) -> str:
        payload = {
            "filters": request.filters.query_values(),
            "sort": request.sort.value,
            "direction": request.direction.value,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def _encode_structured_cursor(
        cls,
        request: AssetSearchRequest,
        *,
        graph_version: str,
        repository_cursor: str,
    ) -> str:
        payload = {
            "v": cls._STRUCTURED_CURSOR_VERSION,
            "request": cls._structured_request_fingerprint(request),
            "graph_version": graph_version,
            "repository_cursor": repository_cursor,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    @classmethod
    def _decode_structured_cursor(
        cls,
        cursor: str,
        request: AssetSearchRequest,
    ) -> tuple[str, str]:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(
                base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
            )
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid structured Asset search cursor.") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("v") != cls._STRUCTURED_CURSOR_VERSION
            or payload.get("request") != cls._structured_request_fingerprint(request)
            or not isinstance(payload.get("graph_version"), str)
            or not payload["graph_version"]
            or not isinstance(payload.get("repository_cursor"), str)
            or not payload["repository_cursor"]
        ):
            raise ValueError("Structured Asset search cursor does not match the request.")
        return payload["repository_cursor"], payload["graph_version"]

    def node(self, ip: str) -> dict[str, Any]:
        target_ip = ip.strip()
        summary = self.repository.get_summary(target_ip)
        if not summary["found"]:
            return {"ip": target_ip, "found": False, "degree": {"in": 0, "out": 0, "total": 0}}
        return {"ip": target_ip, "found": True, "degree": {"in": summary["in_degree"], "out": summary["out_degree"], "total": summary["degree"]}}

    def neighbors(self, ip: str, *, direction: str = "both", limit: int = 20) -> dict[str, Any]:
        return self.repository.get_neighbors(ip.strip(), direction.strip().lower(), limit)

    def path(self, source: str, target: str) -> dict[str, Any]:
        source_ip, target_ip = source.strip(), target.strip()
        semantics = "Observed communication-graph path, not proof of routed network path."
        if source_ip == target_ip:
            return {"source": source_ip, "target": target_ip, "found": True, "path": [source_ip], "edge_count": 0, "semantics": semantics}
        result = self.repository.find_path(source_ip, target_ip)
        if not result["source_present"]:
            result["reason"] = "source_not_found"
        elif not result["target_present"]:
            result["reason"] = "target_not_found"
        elif not result["found"]:
            result["reason"] = "no_observed_communication_graph_path"
        return {**result, "semantics": semantics}

    def relationship(self, source: str, target: str) -> dict[str, Any]:
        return self.repository.get_relationship(source.strip(), target.strip())

    def comparison(self, entity_a: str, entity_b: str) -> dict[str, Any]:
        return self.repository.compare_assets(entity_a.strip(), entity_b.strip())

    def topology(self, *, max_nodes: int | None = None, min_degree: int | None = None, subnet: str = "") -> dict[str, Any]:
        requested_nodes = self.settings.graph_max_ui_nodes if max_nodes is None else max_nodes
        requested_degree = self.settings.graph_default_min_degree if min_degree is None else min_degree
        return self.repository.topology(max_nodes=requested_nodes, min_degree=requested_degree, subnet=subnet.strip())
