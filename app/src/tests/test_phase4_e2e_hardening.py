from __future__ import annotations

import math
from types import MethodType, SimpleNamespace

from src.core.context.models import EntityResolution
from src.core.context.structured_hardening import (
    StructuredAwareEntityResolver,
    StructuredAwareFallbackRouter,
    normalize_structured_query_for_language,
)
from src.core.graph.organizational_neo4j import OrganizationalNeo4jGraphRepository
from src.core.graph.structured import (
    AssetSearchFilters,
    AssetSearchRequest,
    AssetSortField,
    SortDirection,
    StructuredQueryMode,
    StructuredQuerySpec,
)
from src.core.memory.product_hardened import ProductLongTermMemoryStore
from src.core.product_client.schemas import TopologyConnectionRecord


def test_structured_text_filters_are_casefolded_and_strict_above_is_preserved():
    query = StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=AssetSearchFilters(
            role="Database Server",
            status="CONFIRMED",
            classification_summary="High Confidence",
            model_confidence_min=0.95,
        ),
    )
    normalized = normalize_structured_query_for_language(
        query,
        "List all confirmed Database Server assets with model confidence above 0.95.",
    )
    assert normalized.filters.role == "database server"
    assert normalized.filters.status == "confirmed"
    assert normalized.filters.classification_summary == "high confidence"
    assert normalized.filters.model_confidence_min is not None
    assert normalized.filters.model_confidence_min > 0.95
    assert normalized.filters.model_confidence_min == math.nextafter(0.95, math.inf)


def test_ranked_search_retains_two_rows_for_tie_check():
    query = StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=AssetSearchFilters(role="Firewall"),
        sort=AssetSortField.MODEL_CONFIDENCE,
        direction=SortDirection.DESC,
        limit=1,
    )
    normalized = normalize_structured_query_for_language(
        query,
        "Find the Firewall with the highest model confidence and analyze it.",
    )
    assert normalized.limit == 2


def test_structured_set_request_ignores_incidental_ui_selected_ip():
    resolver = StructuredAwareEntityResolver()
    result = resolver.resolve(
        "Group all assets by role and show me the count for each role.",
        {"selected_ip": "192.168.30.115"},
    )
    assert result.status == "none"
    assert not result.entities


def test_normal_ui_focal_question_preserves_selected_ip():
    resolver = StructuredAwareEntityResolver()
    result = resolver.resolve(
        "Analyze this asset.",
        {"selected_ip": "192.168.30.115"},
    )
    assert result.status == "resolved"
    assert result.primary_entity is not None
    assert result.primary_entity.value == "192.168.30.115"
    assert result.primary_entity.source == "ui"


def test_router_failure_fallback_understands_structured_asset_search():
    router = StructuredAwareFallbackRouter()
    route = router.route(
        "List all confirmed Database Server assets with model confidence above 0.95 for me.",
        EntityResolution(status="none"),
    )
    assert route.intent == "asset_search"
    assert route.structured_query is not None
    assert route.entity_binding == "none"
    assert route.target_entity is None
    assert route.structured_query.filters.role == "database server"
    assert route.structured_query.filters.status == "confirmed"
    assert route.structured_query.filters.model_confidence_min > 0.95


def test_router_failure_fallback_understands_grouped_aggregate():
    router = StructuredAwareFallbackRouter()
    route = router.route(
        "Group all assets by role and show me the count for each role.",
        EntityResolution(status="none"),
    )
    assert route.intent == "asset_aggregate"
    assert route.structured_query is not None
    assert route.structured_query.group_by.value == "role"


def test_organizational_projection_excludes_public_and_destination_only_peers():
    records = [
        TopologyConnectionRecord("192.168.0.10", "192.168.0.20"),
        TopologyConnectionRecord("192.168.0.20", "192.168.0.10"),
        TopologyConnectionRecord("192.168.0.10", "8.8.8.8"),
        TopologyConnectionRecord("1.1.1.1", "192.168.0.10"),
        TopologyConnectionRecord("192.168.0.30", "203.0.113.5"),
    ]
    all_source_pairs = OrganizationalNeo4jGraphRepository._all_pairs(records)
    source_nodes = {pair["source"] for pair in all_source_pairs}
    internal_edges = OrganizationalNeo4jGraphRepository._normalize(records)
    assert source_nodes == {"192.168.0.10", "192.168.0.20", "192.168.0.30"}
    assert {(row["source"], row["target"]) for row in internal_edges} == {
        ("192.168.0.10", "192.168.0.20"),
        ("192.168.0.20", "192.168.0.10"),
    }


def test_structured_predicates_are_case_insensitive_across_text_selectors():
    request = AssetSearchRequest(
        filters=AssetSearchFilters(
            asset_name="SOORIN-DC",
            status="CONFIRMED",
            suggested_type="Domain Controller",
            classification_summary="Primary DC",
            role="Domain Controller",
            roles="LDAP Server",
            vendor="VMware",
            product="Active Directory",
            tag="Services",
            sub_tag="Identity",
            enrichment_status="SUCCESS",
        )
    )
    clauses, params = OrganizationalNeo4jGraphRepository._structured_filter_clauses(request)
    rendered = " ".join(clauses)
    assert "toLower(coalesce(a.asset_name" in rendered
    assert "toLower(coalesce(a.classification_summary" in rendered
    assert "any(role_value" in rendered
    assert params["status"] == "confirmed"
    assert params["role"] == "domain controller"
    assert params["roles"] == "ldap server"
    assert params["vendor"] == "vmware"


def test_product_search_summary_hydrates_through_canonical_get():
    class Client:
        def search_ltm(self, **_kwargs):
            return {"records": [{"memoryId": "m-1", "userId": "user-1"}]}

    store = ProductLongTermMemoryStore(Client())
    sentinel = object()

    def hydrate(self, user_id, memory_id, *, request_id="", purpose=""):
        assert user_id == "user-1"
        assert memory_id == "m-1"
        assert purpose.endswith("canonical_hydration")
        return sentinel

    store._hydrate = MethodType(hydrate, store)
    result = store.list(user_id="user-1", purpose="active_inventory")
    assert result == (sentinel,)
