"""Reusable API service dependencies."""

from __future__ import annotations

from functools import lru_cache

from src.config.settings import Settings, get_settings
from src.core.graph.enrichment import AssetEnrichmentService
from src.core.graph.enrichment_runtime import GraphEnrichmentRuntimeService
from src.core.graph.neo4j import Neo4jDriver
from src.core.graph.organizational_neo4j import OrganizationalNeo4jGraphRepository
from src.core.graph.organizational_refresh import GraphRefreshService
from src.core.graph.organizational_service import OrganizationalGraphService
from src.core.memory.factory import LocalPersistenceAdapters, build_local_persistence
from src.core.product_client import ProductApiClient
from src.core.product_client.memory_client import ProductMemoryClient


@lru_cache(maxsize=1)
def get_graph_service() -> OrganizationalGraphService:
    """Share the organizational graph query service across requests."""
    return OrganizationalGraphService(get_settings())


@lru_cache(maxsize=1)
def get_product_api_client() -> ProductApiClient:
    return ProductApiClient(get_settings())


@lru_cache(maxsize=1)
def get_product_memory_client() -> ProductMemoryClient:
    return ProductMemoryClient(get_product_api_client())


@lru_cache(maxsize=1)
def get_graph_repository() -> OrganizationalNeo4jGraphRepository:
    settings = get_settings()
    return OrganizationalNeo4jGraphRepository(Neo4jDriver(settings), settings)


@lru_cache(maxsize=1)
def get_asset_enrichment_service() -> AssetEnrichmentService:
    return AssetEnrichmentService(
        get_settings(),
        get_product_api_client(),
        get_graph_repository(),
    )


@lru_cache(maxsize=1)
def get_graph_enrichment_runtime_service() -> GraphEnrichmentRuntimeService:
    return GraphEnrichmentRuntimeService(
        get_settings(),
        get_asset_enrichment_service(),
        get_graph_repository(),
    )


@lru_cache(maxsize=1)
def get_graph_refresh_service() -> GraphRefreshService:
    settings: Settings = get_settings()
    runtime = get_graph_enrichment_runtime_service()
    return GraphRefreshService(
        settings,
        get_product_api_client(),
        get_graph_repository(),
        on_new_pending_assets=lambda _count, _version: runtime.wake(
            reason="new_assets"
        ),
    )


@lru_cache(maxsize=1)
def get_local_persistence() -> LocalPersistenceAdapters:
    """Build disabled-by-default local simulation adapters once per process."""
    return build_local_persistence(
        get_settings(),
        product_memory_client=get_product_memory_client(),
        product_client=get_product_api_client(),
    )
