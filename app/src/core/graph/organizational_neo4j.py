"""Organizational-source Neo4j projection and normalized structured selectors."""

from __future__ import annotations

import ipaddress
from typing import Any

from src.core.graph.neo4j import (
    GraphProjectionStatus,
    GraphSyncValidationError,
    Neo4jGraphRepository,
    _compile_structured_predicate,
    _serialized_graph_mutation,
    _utc_now,
)
from src.core.graph.structured import AssetAggregateRequest, AssetGroupField, AssetSearchRequest
from src.core.product_client.schemas import TopologyConnectionRecord


ORGANIZATIONAL_PROJECTION_SCHEMA_VERSION = 3
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
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


def _organizational_endpoint(value: object) -> bool:
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        return False
    return address.version == 4 and any(address in network for network in _RFC1918)


class OrganizationalNeo4jGraphRepository(Neo4jGraphRepository):
    """Keep Asset nodes RFC1918-authoritative and text selectors case-insensitive.

    Every RFC1918 IPv4 endpoint in Product topology is promoted to an
    organizational Asset node. Public/external endpoints remain excluded, and
    edges are retained only when both endpoints are organizational Assets.
    """

    @classmethod
    def _projection(
        cls,
        records: list[TopologyConnectionRecord],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        normalized = Neo4jGraphRepository._normalize(records)
        nodes = sorted(
            {
                str(endpoint)
                for pair in normalized
                for endpoint in (pair["source"], pair["target"])
                if _organizational_endpoint(endpoint)
            }
        )
        admitted = set(nodes)
        pairs = [
            pair
            for pair in normalized
            if str(pair["source"]) in admitted and str(pair["target"]) in admitted
        ]
        return nodes, pairs

    @classmethod
    def _normalize(cls, records: list[TopologyConnectionRecord]) -> list[dict[str, Any]]:
        return cls._projection(records)[1]

    @_serialized_graph_mutation
    def sync_snapshot(
        self,
        records: list[TopologyConnectionRecord],
        version: str,
    ) -> GraphProjectionStatus:
        nodes, pairs = self._projection(records)
        if not nodes:
            raise GraphSyncValidationError(
                "Product topology response contained no organizational RFC1918 endpoints."
            )
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

    def projection_schema_version(self) -> int:
        row = self._single(
            "MATCH (m:GraphMetadata {id: 'active'}) RETURN m.schema_version AS schema_version"
        )
        if row is None:
            return 0
        try:
            return int(row.get("schema_version") or 0)
        except (TypeError, ValueError):
            return 0

    def _publish(self, version: str) -> None:
        query = """
        MERGE (m:GraphMetadata {id: 'active'})
        SET m.active_graph_version = $version,
            m.last_successful_sync = $now,
            m.sync_status = 'published',
            m.schema_version = $schema_version
        """
        with self.driver.session() as session:
            session.execute_write(
                lambda tx: tx.run(
                    query,
                    version=version,
                    now=_utc_now(),
                    schema_version=ORGANIZATIONAL_PROJECTION_SCHEMA_VERSION,
                ).consume()
            )

    @staticmethod
    def _structured_filter_clauses(
        request: AssetSearchRequest | AssetAggregateRequest,
    ) -> tuple[list[str], dict[str, object]]:
        values = request.filters.query_values()
        values.pop("predicate", None)
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
        if request.filters.predicate is not None:
            predicate_clause, predicate_values = _compile_structured_predicate(
                request.filters.predicate,
                case_insensitive_text=True,
            )
            clauses.append(predicate_clause)
            values.update(predicate_values)
        return clauses, values

    @staticmethod
    def _asset_group_property(group: AssetGroupField) -> str:
        """Map every Product-derived Exact Search property to its stored key."""
        return {
            AssetGroupField.IP: "ip",
            AssetGroupField.ASSET_NAME: "asset_name",
            AssetGroupField.STATUS: "status",
            AssetGroupField.SUGGESTED_TYPE: "suggested_type",
            AssetGroupField.MODEL_CONFIDENCE: "model_confidence",
            AssetGroupField.MAPPING_CONFIDENCE: "mapping_confidence",
            AssetGroupField.UNKNOWN_SCORE: "unknown_score",
            AssetGroupField.CLASSIFICATION_SUMMARY: "classification_summary",
            AssetGroupField.VENDOR: "vendor",
            AssetGroupField.PRODUCT: "product",
            AssetGroupField.ROLE: "role",
            AssetGroupField.ROLES: "roles",
            AssetGroupField.TAG: "tag",
            AssetGroupField.SUB_TAG: "sub_tag",
            AssetGroupField.LAST_DETECTION_AT: "last_detection_at",
            AssetGroupField.ENRICHMENT_STATUS: "enrichment_status",
        }[group]
