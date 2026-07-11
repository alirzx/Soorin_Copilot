"""Reusable API service dependencies."""

from __future__ import annotations

from functools import lru_cache

from src.config.settings import Settings, get_settings
from src.core.graph.build_service import GraphBuildService
from src.core.graph.refresh import GraphRefreshService
from src.core.graph.service import GraphService
from src.core.product_client import ProductApiClient


@lru_cache(maxsize=1)
def get_graph_service() -> GraphService:
    """Share the graph query service and loader cache across requests."""
    return GraphService(get_settings())


@lru_cache(maxsize=1)
def get_product_api_client() -> ProductApiClient:
    return ProductApiClient(get_settings())


@lru_cache(maxsize=1)
def get_graph_build_service() -> GraphBuildService:
    settings: Settings = get_settings()
    return GraphBuildService(settings, get_product_api_client())


@lru_cache(maxsize=1)
def get_graph_refresh_service() -> GraphRefreshService:
    settings: Settings = get_settings()
    return GraphRefreshService(settings, get_product_api_client())
