"""Regression coverage for post-Codex structured graph/context hardening."""

from __future__ import annotations

from src.config.settings import get_settings
from src.core.context.providers.graph import GraphContextProvider
from src.core.copilot.hardened_service import (
    HardenedContextComposer,
    _bind_organizational_graph_repository,
)
from src.core.graph.organizational_neo4j import OrganizationalNeo4jGraphRepository
from src.core.graph.structured import (
    MAX_AGGREGATE_GROUP_MEMBER_IPS,
    AssetSearchFilters,
    AssetSearchRequest,
)


def test_hardened_context_preserves_bounded_aggregate_member_ips() -> None:
    member_ips = [f"192.0.2.{index}" for index in range(1, 13)]

    serialized = HardenedContextComposer._bounded_structured_value(member_ips)

    assert serialized == member_ips
    assert len(serialized) > 5
    assert len(serialized) <= MAX_AGGREGATE_GROUP_MEMBER_IPS


def test_chat_graph_provider_can_bind_organizational_exact_search_semantics() -> None:
    settings = get_settings()
    provider = GraphContextProvider(settings)
    driver = provider.graph_service.driver
    try:
        _bind_organizational_graph_repository(provider, settings)
        repository = provider.graph_service.repository
        assert isinstance(repository, OrganizationalNeo4jGraphRepository)
        assert repository.driver is driver

        request = AssetSearchRequest(
            filters=AssetSearchFilters(
                role="Linux Server",
                roles="SSH Server",
                classification_summary="High Confidence",
            )
        )
        clauses, params = repository._structured_filter_clauses(request)
        query = " AND ".join(clauses)

        assert "toLower(coalesce(a.role, '')) = $role" in query
        assert "toLower(coalesce(a.classification_summary, '')) = $classification_summary" in query
        assert "any(role_value IN coalesce(a.roles, [])" in query
        assert params["role"] == "linux server"
        assert params["roles"] == "ssh server"
        assert params["classification_summary"] == "high confidence"
    finally:
        driver.close()
