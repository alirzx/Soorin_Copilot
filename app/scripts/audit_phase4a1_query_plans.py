"""Read-only Phase 4A.1 Neo4j query-plan and matched-total audit.

This helper never bootstraps schema, writes nodes, changes indexes, or publishes a
projection. It is therefore safe to run against the configured active Neo4j
instance when the operator wants production-representative EXPLAIN/PROFILE data.

Usage:
    PYTHONPATH=app .venv/bin/python app/scripts/audit_phase4a1_query_plans.py

Set SOORIN_PHASE4A1_PROFILE=1 to execute PROFILE in addition to EXPLAIN. PROFILE
executes the read query but still performs no writes.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any

from src.config.settings import get_settings
from src.core.graph.neo4j import Neo4jDriver, Neo4jGraphRepository


@dataclass(frozen=True)
class PlanObservation:
    name: str
    mode: str
    result_available_after_ms: float
    records: int
    result_consumed_after_ms: float
    db_hits: int | None
    rows: int | None
    root_operator: str | None
    indexes: tuple[str, ...]


def _walk_profile(node: Any) -> tuple[int, int, list[str]]:
    if node is None:
        return 0, 0, []
    db_hits = int(getattr(node, "db_hits", 0) or 0)
    rows = int(getattr(node, "rows", 0) or 0)
    indexes: list[str] = []
    arguments = getattr(node, "arguments", {}) or {}
    index_name = arguments.get("index") or arguments.get("indexName")
    if index_name:
        indexes.append(str(index_name))
    for child in getattr(node, "children", ()) or ():
        child_hits, child_rows, child_indexes = _walk_profile(child)
        db_hits += child_hits
        rows += child_rows
        indexes.extend(child_indexes)
    return db_hits, rows, indexes


def _observe(session: Any, name: str, query: str, params: dict[str, object], mode: str) -> PlanObservation:
    started = time.perf_counter()
    result = session.run(f"{mode} {query}", **params)
    available_ms = (time.perf_counter() - started) * 1000
    records = list(result)
    consumed_started = time.perf_counter()
    summary = result.consume()
    consumed_ms = available_ms + (time.perf_counter() - consumed_started) * 1000
    profile = getattr(summary, "profile", None)
    db_hits = rows = None
    indexes: tuple[str, ...] = ()
    root_operator = None
    if profile is not None:
        hits, profile_rows, found_indexes = _walk_profile(profile)
        db_hits, rows = hits, profile_rows
        indexes = tuple(dict.fromkeys(found_indexes))
        root_operator = str(getattr(profile, "operator_type", "") or "") or None
    return PlanObservation(
        name=name,
        mode=mode,
        result_available_after_ms=round(available_ms, 3),
        records=len(records),
        result_consumed_after_ms=round(consumed_ms, 3),
        db_hits=db_hits,
        rows=rows,
        root_operator=root_operator,
        indexes=indexes,
    )


def main() -> int:
    settings = get_settings()
    driver = Neo4jDriver(settings)
    repository = Neo4jGraphRepository(driver, settings)
    profile_enabled = os.getenv("SOORIN_PHASE4A1_PROFILE") == "1"
    modes = ("EXPLAIN", "PROFILE") if profile_enabled else ("EXPLAIN",)

    try:
        with driver.session() as session:
            active = session.run(
                "MATCH (m:GraphMetadata {id:'active'}) RETURN m.active_graph_version AS version"
            ).single()
            active_version = str(active["version"]) if active and active["version"] else ""
            if not active_version:
                raise RuntimeError("No active graph projection is published.")

            sample = session.run(
                "MATCH (a:Asset {graph_version:$version}) "
                "RETURN a.ip AS ip, a.role AS role, a.product AS product, a.vendor AS vendor, "
                "a.status AS status, a.model_confidence AS confidence, "
                "a.enrichment_status AS enrichment_status "
                "ORDER BY a.graph_key ASC LIMIT 1",
                version=active_version,
            ).single()
            values = dict(sample or {})
            params = {
                "version": active_version,
                "ip": values.get("ip") or "0.0.0.0",
                "role": values.get("role") or "__missing__",
                "product": values.get("product") or "__missing__",
                "vendor": values.get("vendor") or "__missing__",
                "status": values.get("status") or "__missing__",
                "confidence": float(values.get("confidence") or 0.5),
                "enrichment_status": values.get("enrichment_status") or "pending",
                "limit": 51,
            }

            cases = (
                ("ip", "MATCH (a:Asset) WHERE a.graph_version=$version AND a.ip=$ip RETURN a.graph_key LIMIT $limit"),
                ("role", "MATCH (a:Asset) WHERE a.graph_version=$version AND a.role=$role RETURN a.graph_key LIMIT $limit"),
                ("product", "MATCH (a:Asset) WHERE a.graph_version=$version AND a.product=$product RETURN a.graph_key LIMIT $limit"),
                ("vendor", "MATCH (a:Asset) WHERE a.graph_version=$version AND a.vendor=$vendor RETURN a.graph_key LIMIT $limit"),
                ("status", "MATCH (a:Asset) WHERE a.graph_version=$version AND a.status=$status RETURN a.graph_key LIMIT $limit"),
                ("confidence", "MATCH (a:Asset) WHERE a.graph_version=$version AND a.model_confidence <= $confidence RETURN a.graph_key LIMIT $limit"),
                ("enrichment", "MATCH (a:Asset) WHERE a.graph_version=$version AND a.enrichment_status=$enrichment_status RETURN a.graph_key LIMIT $limit"),
            )
            observations = [
                _observe(session, name, query, params, mode)
                for mode in modes
                for name, query in cases
            ]

            count_query = "MATCH (a:Asset) WHERE a.graph_version=$version AND a.role=$role RETURN count(a) AS matched_total"
            count_timings: list[float] = []
            if profile_enabled:
                for _ in range(5):
                    started = time.perf_counter()
                    session.run(count_query, **params).consume()
                    count_timings.append((time.perf_counter() - started) * 1000)
                observations.append(_observe(session, "matched_total_role", count_query, params, "PROFILE"))

            stats_source_has_collect = "collect(a.ip)" in repository.stats.__doc__ if repository.stats.__doc__ else False
            # Source-level scale debt is reported explicitly below; runtime timing
            # of /graph/stats is intentionally not performed by this focused audit.
            report = {
                "active_graph_version": active_version,
                "profile_enabled": profile_enabled,
                "observations": [asdict(item) for item in observations],
                "matched_total_ms": {
                    "samples": [round(value, 3) for value in count_timings],
                    "median": round(statistics.median(count_timings), 3) if count_timings else None,
                    "max": round(max(count_timings), 3) if count_timings else None,
                },
                "index_policy": "No index is created by this audit. Add one only after measured plans justify it.",
                "stats_scale_note": "Neo4jGraphRepository.stats currently materializes collect(a.ip); refactor separately after measuring the public stats path.",
                "writes_performed": False,
            }
            del stats_source_has_collect
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
