"""Lossless full-JSON Product Asset Profile context provider."""

from __future__ import annotations

from src.config.settings import Settings
from src.core.context.models import AssetProfileProviderResult
from src.core.context.providers.product_json import FullJsonContextProvider
from src.core.product_client import ProductApiClient


class AssetProfileContextProvider(FullJsonContextProvider):
    """Fetch and cache complete profile payloads in an independent namespace."""

    def __init__(self, settings: Settings, product_client: ProductApiClient | None = None) -> None:
        client = product_client or ProductApiClient(settings)
        super().__init__(
            settings,
            provider_name="asset_profile",
            source="product_asset_profile",
            fetcher=client.get_asset_profile,
            result_type=AssetProfileProviderResult,
        )
