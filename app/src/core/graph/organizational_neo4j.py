"""Organizational-source Neo4j projection and normalized structured selectors."""

from __future__ import annotations

from typing import Any

from src.core.graph.neo4j import (
    GraphProjectionStatus,
    GraphSyncValidationError,
    Neo4jGraphRepository,
    _serialized_graph_mutation,
    _utc_now,
)
from src.core.graph.structured import AssetAggregateRequest, AssetSearchRequest
from src.core.product_client.schemas import TopologyConnectionRecord


_TEXT_PROPERTIES = {
    "asset_name": "asset_name",
    "status": "status",
    "suggested_type": "suggested_type",
    "classification_summary": "classification_summary",
    "role": "role",
    "vendor": "vendor",
    "product": "product",
    "tag": "tag",
    "sub_tag": "sub_tag",
    "enrichment_status": "enrichment_status",
}


class OrganizationalNeo4jGraphRepository(Neo4jGraphRepository):
    """Keep Asset nodes source-authoritative and text selectors case-insensitive.

    Product topology source IPs are the only addresses promoted to organizational
    Asset nodes. Destination-only peers are deliberately excluded from the Asset
    projection until a separate peer/external-endpoint schema exists. Edges are
    retained only when both endpoints are organizational source Assets.
    """

    @staticmethod
    def _all_pairs(records: list[TopologyConnectionRecord]) -> list[dict[str, Any]]:
        return Neo4jGraphRepository._normalize(records)

    @staticmethod
    def _normalize(records: list[TopologyConnectionRecord]) -> list[dict[str, Any]]:
        pairs = Neo4jGraphRepository._normalize(records)
        source_assets = {str(pair["source"]) for pair in pairs}
        return [pair for pair in pairs if str(pair["target"]) in source_assets]

    @_serialized_graph_mutation
    def sync_snapshot(
        self,
        records: list[TopologyConnectionRecord],
        version: str,
    ) -> GraphProjectionStatus:
        all_pairs = self._all_pairs(records)
        if not all_pairs:
            raise GraphSyncValidationError(
                "Product topology response contained no valid graph records."
            )
        nodes = sorted({str(pair["source"]) for pair in all_pairs})
        source_assets = set(nodes)
        pairs = [pair for pair in all_pairs if str(pair["target"]) in source_assets]
        new_pending_assets = self._write_staging_nodes(nodes, version)
        self._write_staging_edges(pairs, version)
        self._validate_staging(version, len(nodes), len(pairs))
        self._publish(version)
        self._delete_inactive_versions(version)
        return GraphProjectionStatus(
            version,
            len(nodes),
            len(pairs),
            _utc_now(),
            int(new_pending_assets or 0),
        )

    @staticmethod
    def _structured_filter_clauses(
        request: AssetSearchRequest | AssetAggregateRequest,
    ) -> tuple[list[str], dict[str, object]]:
        values = request.filters.query_values()
        clauses: list[str] = []

        if "ip" in values:
            clauses.append("a.ip = $ip")

        for parameter, property_name in _TEXT_PROPERTIES.items():
            if parameter in values:
                values[parameter] = str(values[parameter]).strip().casefold()
                clauses.append(
                    f"toLower(coalesce(a.{property_name}, '')) = ${parameter}"
                )

        if "roles" in values:
            values["roles"] = str(values["roles"]).strip().casefold()
            clauses.append(
                "any(role_value IN coalesce(a.roles, []) "
                "WHERE toLower(role_value) = $roles)"
            )

        range_properties = {
            "model_confidence_min": ("model_confidence", ">="),
            "model_confidence_max": ("model_confidence", "<="),
            "mapping_confidence_min": ("mapping_confidence", ">="),
            "mapping_confidence_max": ("mapping_confidence", "<="),
            "unknown_score_min": ("unknown_score", ">="),
            "unknown_score_max": ("unknown_score", "<="),
            "last_detection_at_from": ("last_detection_at", ">="),
            "last_detection_at_to": ("last_detection_at", "<="),
        }
        for parameter, (property_name, operator) in range_properties.items():
            if parameter in values:
                clauses.append(f"a.{property_name} {operator} ${parameter}")
        return clauses, values
