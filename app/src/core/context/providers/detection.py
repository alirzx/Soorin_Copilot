"""Lossless full-JSON asset-detection context provider."""

from __future__ import annotations

from src.config.settings import Settings
from src.core.context.models import DetectionProviderResult
from src.core.context.providers.product_json import FullJsonContextProvider
from src.core.product_client import ProductApiClient


class DetectionContextProvider(FullJsonContextProvider):
    """Fetch and cache complete detection payloads with no detail variants."""

    def __init__(self, settings: Settings, product_client: ProductApiClient | None = None) -> None:
        client = product_client or ProductApiClient(settings)
        super().__init__(
            settings,
            provider_name="detection",
            source="product_asset_detection",
            fetcher=client.get_asset_detection,
            result_type=DetectionProviderResult,
        )
