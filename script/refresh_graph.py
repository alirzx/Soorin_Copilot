#!/usr/bin/env python3
"""Run one safe graph refresh cycle from the configured product endpoint."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from src.config.settings import get_settings
from src.core.graph.refresh import GraphRefreshService
from src.core.product_client import ProductApiClient


def main() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )

    print("=" * 60)
    print("REFRESHING SOORIN GRAPH TOPOLOGY")
    print("=" * 60)
    print(f"Product base URL configured: {bool(settings.product_api_base_url)}")
    print(f"Topology endpoint path: {settings.product_topology_path}")
    print(f"Token configured: {bool(settings.product_api_token)}")
    print(f"HWID configured: {bool(settings.product_hwid)}")

    service = GraphRefreshService(settings, ProductApiClient(settings))
    service.load_last_known_good()
    result = service.refresh_once()

    print("\nREFRESH RESULT")
    print(f"Status: {result.status}")
    print(f"Activated: {result.activated}")
    print(f"Nodes: {result.nodes:,}")
    print(f"Edges: {result.edges:,}")
    print(f"Raw records: {result.raw_records:,}")
    print(f"Processed records: {result.processed_records:,}")
    print(f"Snapshot version: {result.snapshot_version}")
    print(f"Raw snapshot path: {result.raw_snapshot_path}")
    print(f"Processed snapshot path: {result.processed_snapshot_path}")
    if result.message:
        print(f"Message: {result.message}")
    return 0 if result.status == "ok" and result.activated else 1


if __name__ == "__main__":
    raise SystemExit(main())
