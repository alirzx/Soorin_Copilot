"""Lossless full-JSON asset-detection context provider."""

from __future__ import annotations

from src.config.settings import Settings
from src.core.context.models import DetectionProviderResult
from src.core.context.providers.product_json import FullJsonContextProvider
from src.core.product_client import ProductApiClient


class DetectionContextProvider(FullJsonContextProvider):
    """Fetch and cache approved Detection views through one Product client."""

    def __init__(self, settings: Settings, product_client: ProductApiClient | None = None) -> None:
        client = product_client or ProductApiClient(settings)

        def fetch_detection(ip: str, *, request_id: str = "", view: str = "full"):
            try:
                return client.get_asset_detection(ip, request_id=request_id, view=view)
            except TypeError as exc:
                # Compatibility for established offline fakes that predate view support.
                if "view" not in str(exc):
                    raise
                return client.get_asset_detection(ip, request_id=request_id)

        super().__init__(
            settings,
            provider_name="detection",
            source="product_asset_detection",
            fetcher=fetch_detection,
            result_type=DetectionProviderResult,
        )
