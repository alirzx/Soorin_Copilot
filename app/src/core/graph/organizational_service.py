"""Graph service bound to the organizational RFC1918 endpoint repository."""

from __future__ import annotations

from src.config.settings import Settings, get_settings
from src.core.graph.neo4j import Neo4jDriver
from src.core.graph.organizational_neo4j import OrganizationalNeo4jGraphRepository
from src.core.graph.service import GraphService


class OrganizationalGraphService(GraphService):
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.driver = Neo4jDriver(self.settings)
        self.repository = OrganizationalNeo4jGraphRepository(
            self.driver,
            self.settings,
        )
