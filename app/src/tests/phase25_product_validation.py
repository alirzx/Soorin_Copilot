"""Opt-in, sanitized Phase 2.5 Product-to-isolated-Neo4j validation.

Run from the repository root with the explicit isolated target variables shown
in docs/NEO4J_GRAPH_ENRICHMENT_GRAPHRAG.md. The configured application Neo4j
projection is used read-only to select a bounded set of real Product assets.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from src.config.settings import get_settings
from src.core.graph.enrichment import AssetEnrichmentService
from src.core.graph.neo4j import Neo4jDriver, Neo4jGraphRepository
from src.core.product_client import (
    ProductApiClient,
    ProductAssetDetectionOverview,
    ProductAssetResponse,
    TopologyConnectionRecord,
)


PROPERTY_NAMES = tuple(
    ProductAssetDetectionOverview.from_payload({"ip": "192.0.2.1"})
    .__dataclass_fields__
)
METADATA_NAMES = (
    "enrichment_status",
    "enrichment_updated_at",
    "enrichment_last_attempt_at",
    "enrichment_last_success_at",
    "enrichment_next_due_at",
    "enrichment_source",
    "enrichment_version",
)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _duration_projection(asset_count: int, assets_per_second: float) -> dict[str, float]:
    seconds = asset_count / assets_per_second
    return {
        "seconds": round(seconds, 3),
        "hours": round(seconds / 3600, 3),
        "days": round(seconds / 86400, 3),
    }


class _TimedProductClient:
    def __init__(self, client: ProductApiClient) -> None:
        self.client = client
        self.latencies: list[float] = []
        self.active = 0
        self.max_active = 0
        self.expected: dict[str, dict[str, Any]] = {}
        self.categories: dict[str, dict[str, str | None]] = {}

    def get_asset_detection_overview(
        self,
        ip: str,
        *,
        request_id: str = "",
    ) -> ProductAssetResponse:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        started = time.perf_counter()
        try:
            response = self.client.get_asset_detection_overview(
                ip,
                request_id=request_id,
            )
            if response.found is not False and response.raw_payload is not None:
                overview = ProductAssetDetectionOverview.from_payload(
                    response.raw_payload,
                    expected_ip=ip,
                )
                self.expected[ip] = overview.property_values()
                self.categories[ip] = {
                    "status": overview.status,
                    "suggested_type": overview.suggested_type,
                    "role": overview.role,
                }
            return response
        finally:
            self.latencies.append(time.perf_counter() - started)
            self.active -= 1


def _require_isolated_target() -> tuple[str, str, str]:
    if os.getenv("SOORIN_PHASE25_ALLOW_ISOLATED_MUTATION") != "1":
        raise RuntimeError(
            "SOORIN_PHASE25_ALLOW_ISOLATED_MUTATION=1 is required."
        )
    uri = os.getenv("SOORIN_PHASE25_NEO4J_URI", "").strip()
    user = os.getenv("SOORIN_PHASE25_NEO4J_USER", "").strip()
    password = os.getenv("SOORIN_PHASE25_NEO4J_PASSWORD", "").strip()
    if not uri or not user or not password:
        raise RuntimeError(
            "SOORIN_PHASE25_NEO4J_URI/USER/PASSWORD are required."
        )
    if urlparse(uri).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Phase 2.5 mutation target must be loopback.")
    return uri, user, password


def main() -> int:
    logging.disable(logging.CRITICAL)
    base = get_settings()
    sample_size = int(os.getenv("SOORIN_PHASE25_SAMPLE_SIZE", "8"))
    if not 5 <= sample_size <= 10:
        raise RuntimeError("SOORIN_PHASE25_SAMPLE_SIZE must be between 5 and 10.")
    target_uri, target_user, target_password = _require_isolated_target()
    if target_uri == base.neo4j_uri:
        raise RuntimeError(
            "Isolated mutation target must differ from configured source Neo4j."
        )

    source_driver = Neo4jDriver(base)
    try:
        source = Neo4jGraphRepository(source_driver, base)
        topology = source.topology(max_nodes=sample_size, min_degree=0)
        candidates = [str(node["ip"]) for node in topology["nodes"]]
    finally:
        source_driver.close()
    if len(candidates) < 5:
        raise RuntimeError(
            f"Bounded source topology returned only {len(candidates)} assets."
        )
    candidates = candidates[:sample_size]

    target = replace(
        base,
        neo4j_uri=target_uri,
        neo4j_user=target_user,
        neo4j_password=target_password,
        graph_enrichment_enabled=True,
        graph_enrichment_concurrency=1,
        graph_enrichment_page_size=len(candidates),
        graph_enrichment_batch_size=len(candidates),
    )
    target_driver = Neo4jDriver(target)
    product_client = ProductApiClient(base)
    timed_client = _TimedProductClient(product_client)
    try:
        repository = Neo4jGraphRepository(target_driver, target)
        with target_driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
        repository.bootstrap_schema()
        records = [
            TopologyConnectionRecord(
                candidates[index],
                candidates[(index + 1) % len(candidates)],
            )
            for index in range(len(candidates))
        ]
        version = "phase25-product-" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        repository.sync_snapshot(records, version)
        started = time.perf_counter()
        result = AssetEnrichmentService(
            target,
            timed_client,  # type: ignore[arg-type]
            repository,
        ).run_page(request_id="phase25-product-validation")
        total_seconds = time.perf_counter() - started
        with target_driver.session() as session:
            rows = list(
                session.run(
                    "MATCH (m:GraphMetadata {id: 'active'}) "
                    "MATCH (a:Asset {graph_version: m.active_graph_version}) "
                    "WHERE a.graph_key IN $keys "
                    "RETURN a.graph_key AS graph_key, properties(a) AS properties",
                    keys=candidates,
                )
            )
    finally:
        target_driver.close()
        product_client.session.close()

    persisted = {str(row["graph_key"]): dict(row["properties"]) for row in rows}
    matching = 0
    metadata_complete = 0
    property_presence = {name: 0 for name in PROPERTY_NAMES}
    aliases: list[dict[str, Any]] = []
    for index, ip in enumerate(candidates, start=1):
        properties = persisted.get(ip, {})
        expected = timed_client.expected.get(ip)
        if expected is not None and all(
            properties.get(name) == value for name, value in expected.items()
        ):
            matching += 1
        if all(properties.get(name) is not None for name in METADATA_NAMES):
            metadata_complete += 1
        for name in PROPERTY_NAMES:
            if properties.get(name) is not None:
                property_presence[name] += 1
        aliases.append(
            {
                "asset": f"asset_{index}",
                "category": timed_client.categories.get(ip),
                "persisted_property_count": sum(
                    name in properties for name in PROPERTY_NAMES
                ),
            }
        )

    latencies = timed_client.latencies
    assets_per_second = len(latencies) / total_seconds if total_seconds else 0.0
    report = {
        "sample_size": len(candidates),
        "requests_strictly_sequential": timed_client.max_active == 1,
        "maximum_observed_concurrency": timed_client.max_active,
        "result": {
            "attempted": result.attempted,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "unavailable": result.unavailable,
            "updated": result.updated,
        },
        "direct_property_matches": matching,
        "metadata_complete": metadata_complete,
        "property_presence_by_field": property_presence,
        "assets": aliases,
        "latency_seconds": {
            "per_request": [
                {"asset": f"asset_{index}", "seconds": round(value, 3)}
                for index, value in enumerate(latencies, start=1)
            ],
            "min": round(min(latencies), 3),
            "average": round(statistics.fmean(latencies), 3),
            "max": round(max(latencies), 3),
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "total": round(total_seconds, 3),
        },
        "throughput": {
            "assets_per_second": round(assets_per_second, 6),
            "assets_per_minute": round(assets_per_second * 60, 3),
            "assets_per_hour": round(assets_per_second * 3600, 3),
        },
        "projections": {
            str(count): _duration_projection(count, assets_per_second)
            for count in (400, 10_000, 1_000_000)
        },
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    passed = (
        result.attempted == len(candidates)
        and result.succeeded == len(candidates)
        and result.updated == len(candidates)
        and matching == len(candidates)
        and metadata_complete == len(candidates)
        and timed_client.max_active == 1
    )
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - sanitized CLI boundary.
        print(
            json.dumps(
                {"phase25_product_validation_blocker": type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
