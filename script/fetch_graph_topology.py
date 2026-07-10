#!/usr/bin/env python3
"""Fetch product topology JSON and build local graph artifacts."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from src.config.settings import get_settings
from src.core.graph.build_service import GraphBuildService
from src.core.product_client import ProductApiClient, ProductApiError


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    settings = get_settings()

    print("=" * 60)
    print("FETCHING NETWORK TOPOLOGY")
    print("=" * 60)
    print(f"Endpoint: {settings.product_api_base_url}{settings.product_topology_path}")
    print("Auth: Bearer token from SOORIN_PRODUCT_API_TOKEN")
    print(f"HWID configured: {bool(settings.product_hwid)}")

    service = GraphBuildService(settings, ProductApiClient(settings))
    try:
        result = service.rebuild_from_product()
    except ProductApiError as exc:
        print(f"\nERROR: {exc}")
        return 1

    print("\nFETCH COMPLETE")
    print(f"Status: {result.status}")
    print(f"Raw records: {result.raw_records:,}")
    print(f"Processed records: {result.processed_records:,}")
    print(f"Graph nodes: {result.nodes:,}")
    print(f"Graph edges: {result.edges:,}")
    print(f"Artifacts updated: {result.artifact_updated}")
    print(f"Graph cache reloaded: {result.graph_reloaded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
