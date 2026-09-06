"""Reusable API service dependencies."""

from __future__ import annotations

from functools import lru_cache

from src.config.settings import Settings, get_settings
from src.core.graph.refresh import GraphRefreshService
from src.core.graph.service import GraphService
from src.core.memory.factory import LocalPersistenceAdapters, build_local_persistence
from src.core.product_client import ProductApiClient
from src.core.product_client.memory_client import ProductMemoryClient


@lru_cache(maxsize=1)
def get_graph_service() -> GraphService:
    """Share the graph query service and loader cache across requests."""
    return GraphService(get_settings())


@lru_cache(maxsize=1)
def get_product_api_client() -> ProductApiClient:
    return ProductApiClient(get_settings())


@lru_cache(maxsize=1)
def get_product_memory_client() -> ProductMemoryClient:
    return ProductMemoryClient(get_product_api_client())


@lru_cache(maxsize=1)
def get_graph_refresh_service() -> GraphRefreshService:
    settings: Settings = get_settings()
    return GraphRefreshService(settings, get_product_api_client())


@lru_cache(maxsize=1)
def get_local_persistence() -> LocalPersistenceAdapters:
    """Build disabled-by-default local simulation adapters once per process."""
    return build_local_persistence(
        get_settings(),
        product_memory_client=get_product_memory_client(),
        product_client=get_product_api_client(),
    )
