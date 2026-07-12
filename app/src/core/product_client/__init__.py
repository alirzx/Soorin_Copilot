"""Shared authenticated client for Soorin product API endpoints."""

from src.core.product_client.client import ProductApiClient
from src.core.detection.models import RawAssetDetectionResponse
from src.core.product_client.errors import ProductApiConfigError, ProductApiError, ProductApiHTTPError
from src.core.product_client.schemas import ProductTopologyResponse, TopologyConnectionRecord

__all__ = [
    "ProductApiClient",
    "ProductApiConfigError",
    "ProductApiError",
    "ProductApiHTTPError",
    "ProductTopologyResponse",
    "RawAssetDetectionResponse",
    "TopologyConnectionRecord",
]
