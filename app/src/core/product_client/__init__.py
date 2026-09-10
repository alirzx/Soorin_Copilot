"""Shared authenticated client for Soorin product API endpoints."""

from src.core.product_client.auth import ProductAuthManager
from src.core.product_client.client import ProductApiClient
from src.core.product_client.memory_client import ProductMemoryClient
from src.core.product_client.errors import ProductApiConfigError, ProductApiError, ProductApiHTTPError
from src.core.product_client.schemas import (
    ProductAssetDetectionOverview,
    ProductAssetOverviewContractError,
    ProductAssetResponse,
    ProductTopologyResponse,
    TopologyConnectionRecord,
)

__all__ = [
    "ProductApiClient",
    "ProductMemoryClient",
    "ProductAuthManager",
    "ProductApiConfigError",
    "ProductApiError",
    "ProductApiHTTPError",
    "ProductAssetDetectionOverview",
    "ProductAssetOverviewContractError",
    "ProductAssetResponse",
    "ProductTopologyResponse",
    "TopologyConnectionRecord",
]
