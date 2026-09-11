"""Graph context provider using the source-only organizational projection."""

from __future__ import annotations

from src.config.settings import Settings
from src.core.context.providers.graph import GraphContextProvider as BaseGraphContextProvider
from src.core.graph.organizational_service import OrganizationalGraphService


class GraphContextProvider(BaseGraphContextProvider):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.graph_service = OrganizationalGraphService(settings)
