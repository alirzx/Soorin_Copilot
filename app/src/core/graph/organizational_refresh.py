"""Refresh adapter that migrates legacy organizational projections once."""

from __future__ import annotations

import logging

from src.core.graph.organizational_neo4j import (
    ORGANIZATIONAL_PROJECTION_SCHEMA_VERSION,
    OrganizationalNeo4jGraphRepository,
)
from src.core.graph.refresh import GraphRefreshResult, GraphRefreshService as BaseGraphRefreshService


logger = logging.getLogger(__name__)


class GraphRefreshService(BaseGraphRefreshService):
    """Force one startup refresh when the active projection predates RFC1918 endpoints."""

    def refresh_once(
        self,
        *,
        force: bool = False,
        reason: str | None = None,
    ) -> GraphRefreshResult:
        repository = self.repository
        if (
            not force
            and reason == "startup"
            and isinstance(repository, OrganizationalNeo4jGraphRepository)
        ):
            schema_version = repository.projection_schema_version()
            if schema_version < ORGANIZATIONAL_PROJECTION_SCHEMA_VERSION:
                logger.info(
                    "event=graph_projection_schema_migration previous_schema=%s target_schema=%s",
                    schema_version,
                    ORGANIZATIONAL_PROJECTION_SCHEMA_VERSION,
                )
                force = True
                reason = "organizational_projection_schema_migration"
        return super().refresh_once(force=force, reason=reason)
