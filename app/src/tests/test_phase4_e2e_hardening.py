from __future__ import annotations

import math
import logging
from types import MethodType, SimpleNamespace

import pytest

from src.config.settings import get_settings
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.context.models import EntityResolution, IntentDecision, ResolvedEntity
from src.core.context.structured_hardening import (
    StructuredAwareEntityResolver,
    StructuredAwareFallbackRouter,
    normalize_natural_score,
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
    structured_query_identity,
)
from src.core.memory.product_hardened import ProductLongTermMemoryStore
from src.core.memory.product import ProductMemoryContractError
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.structured_query import (
    StructuredAggregateGroupRef,
    StructuredQueryContext,
)
from src.core.identity import RequestIdentity
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


def test_multiple_score_comparators_each_preserve_their_strict_boundary():
    query = StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=AssetSearchFilters(
            model_confidence_min=0.9,
            mapping_confidence_min=0.8,
        ),
    )

    normalized = normalize_structured_query_for_language(
        query,
        "List assets with model confidence above 90% and mapping confidence over 80%.",
    )

    assert normalized.filters.model_confidence_min == math.nextafter(0.9, math.inf)
    assert normalized.filters.mapping_confidence_min == math.nextafter(0.8, math.inf)


def test_ranked_search_retains_three_rows_to_prove_tie_boundary():
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
    assert normalized.limit == 3


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


def test_contextual_set_reference_ignores_incidental_ui_selected_ip():
    prior = StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=AssetSearchFilters(role="Linux Server"),
    )
    state = SessionRoutingState(
        structured_query_context=StructuredQueryContext.create(
            query_identity=structured_query_identity(
                prior, active_graph_version="graph-v1"
            ),
            active_graph_version="graph-v1",
            query=prior,
            matched_total=3,
            returned_count=3,
            source_request_id="request-1",
            retrieved_at="2026-09-13T00:00:00+00:00",
            created_at="2026-09-13T00:00:01+00:00",
        )
    )

    result = StructuredAwareEntityResolver().resolve(
        "Which of them have model confidence under 90?",
        {"selected_ip": "192.168.30.1"},
        state,
    )

    assert result.status == "none"
    assert not result.entities


@pytest.mark.parametrize(
    ("raw", "percent_explicit", "expected"),
    [
        (0.9, False, 0.9),
        (90, False, 0.9),
        ("90%", False, 0.9),
        (90, True, 0.9),
        (100, False, 1.0),
        ("100%", False, 1.0),
    ],
)
def test_natural_score_units_are_normalized_without_weakening_contract(
    raw, percent_explicit, expected
):
    assert normalize_natural_score(raw, percent_explicit=percent_explicit) == expected


@pytest.mark.parametrize("raw", ["145%", "-20%", 2.4])
def test_ambiguous_or_out_of_range_natural_scores_fail_closed(raw):
    with pytest.raises(ValueError):
        normalize_natural_score(raw)


@pytest.mark.parametrize(
    ("message", "field", "strict_direction"),
    [
        ("List assets with confidence above 90", "model_confidence_min", "above"),
        ("List assets with classification certainty over 90%", "model_confidence_min", "above"),
        ("List assets with mapping certainty at least 90 percent", "mapping_confidence_min", "inclusive"),
        ("List assets with role-mapping confidence under 0.90", "mapping_confidence_max", "below"),
        ("List assets with unknown probability at most 90", "unknown_score_max", "inclusive"),
    ],
)
def test_router_failure_fallback_supports_score_aliases_units_and_comparators(
    message, field, strict_direction
):
    route = StructuredAwareFallbackRouter().route(message, EntityResolution(status="none"))

    assert route.intent == "asset_search"
    assert route.structured_query is not None
    value = getattr(route.structured_query.filters, field)
    assert value is not None
    if strict_direction == "above":
        assert value == math.nextafter(0.9, math.inf)
    elif strict_direction == "below":
        assert value == math.nextafter(0.9, -math.inf)
    else:
        assert value == 0.9


@pytest.mark.parametrize(
    ("message", "field", "expected"),
    [
        ("List assets with primary role Database Server.", "role", "database server"),
        ("List assets with device function Firewall.", "role", "firewall"),
        ("List assets that include role SSH Server.", "roles", "ssh server"),
        ("List assets made by VMware.", "vendor", "vmware"),
        ("List assets with asset type Linux Server.", "suggested_type", "linux server"),
        ("List assets with kind of device Firewall.", "suggested_type", "firewall"),
        ("List assets with platform/product Active Directory.", "product", "active directory"),
        ("List assets with asset label Services.", "tag", "services"),
        ("List assets with secondary tag Identity.", "sub_tag", "identity"),
        ("List assets with hostname/name DC-01.", "asset_name", "dc-01"),
        (
            "List assets with classifier summary Primary Identity Server.",
            "classification_summary",
            "primary identity server",
        ),
        ("List assets with enrichment status SUCCESS.", "enrichment_status", "success"),
    ],
)
def test_router_failure_fallback_supports_bounded_filter_aliases(
    message, field, expected
):
    route = StructuredAwareFallbackRouter().route(message, EntityResolution(status="none"))

    assert route.intent == "asset_search"
    assert route.structured_query is not None
    assert getattr(route.structured_query.filters, field) == expected


def test_router_failure_fallback_supports_detection_timestamp_alias():
    route = StructuredAwareFallbackRouter().route(
        "List assets last detected after 2026-09-10T08:00:00+00:00.",
        EntityResolution(status="none"),
    )

    assert route.intent == "asset_search"
    assert route.structured_query is not None
    assert route.structured_query.filters.last_detection_at_from.isoformat() == (
        "2026-09-10T08:00:00+00:00"
    )


@pytest.mark.parametrize(
    "message",
    [
        "List assets with confidence above 145%",
        "List assets with mapping confidence below -20%",
        "List assets with unknown score over 2.4",
        "List assets with model confidence above 100%",
    ],
)
def test_router_failure_invalid_score_query_fails_closed(message):
    route = StructuredAwareFallbackRouter().route(message, EntityResolution(status="none"))

    assert route.intent == "unclear"
    assert route.reason == "deterministic_structured_parse_unavailable"
    assert route.target_entity is None


def test_invalid_structured_followup_clarifies_before_any_state_or_memory_mutation():
    active = SessionRoutingState(
        active_ip="192.168.30.1",
        active_entities=("192.168.30.1",),
        last_resolved_entities=("192.168.30.1",),
    )
    ui_entity = ResolvedEntity(
        type="ip",
        value="192.168.30.1",
        source="ui",
    )
    resolution = EntityResolution(
        status="resolved",
        entities=[ui_entity],
        primary_entity=ui_entity,
        entity_mode="single",
        valid_entity_count=1,
    )
    failed_decision = IntentDecision(
        intent="unclear",
        scope="none",
        direction="none",
        depth=0,
        requires_graph=False,
        classification_confidence=0.0,
        reason="invalid structured score",
        decision_source="deterministic_fallback",
        router_called=True,
        error_reason="schema_validation_failed:structured_query",
        fallback_used=True,
        fallback_reason="schema_validation_failed:structured_query",
    )
    service = SimpleNamespace(
        settings=get_settings(),
        intent_router=SimpleNamespace(
            classify=lambda *_args, **_kwargs: failed_decision
        ),
        fallback_router=StructuredAwareFallbackRouter(),
    )

    routed = CopilotWorkflowNodes(service).route({
        "message": "Show assets with model confidence above 145%.",
        "resolved_entities": resolution,
        "active_entity_state": active,
        "recent_messages": [],
        "request_id": "invalid-structured-safe",
        "trace_id": "trace-invalid-structured-safe",
        "ui_context": {"selected_ip": "192.168.30.1"},
    })

    assert routed["workflow_status"] == "clarification_required"
    assert routed["clarification"]["code"] == "structured_query_clarification_required"
    assert "routing_result" not in routed
    assert active.active_ip == "192.168.30.1"
    assert active.active_entities == ("192.168.30.1",)
    assert active.last_resolved_entities == ("192.168.30.1",)


def test_router_failure_set_followup_merges_prior_query_without_ui_binding():
    prior = StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=AssetSearchFilters(role="Linux Server"),
    )
    state = SessionRoutingState(
        structured_query_context=StructuredQueryContext.create(
            query_identity=structured_query_identity(
                prior, active_graph_version="graph-v1"
            ),
            active_graph_version="graph-v1",
            query=prior,
            matched_total=14,
            returned_count=8,
            source_request_id="request-1",
            retrieved_at="2026-09-13T00:00:00+00:00",
            created_at="2026-09-13T00:00:01+00:00",
        )
    )

    route = StructuredAwareFallbackRouter().route(
        "Which of them have model confidence under 90?",
        EntityResolution(status="resolved"),
        state,
    )

    assert route.intent == "asset_search"
    assert route.structured_result_reference.kind == "set_query"
    assert route.structured_query is not None
    assert route.structured_query.filters.role == "linux server"
    assert route.structured_query.filters.model_confidence_max == math.nextafter(
        0.9, -math.inf
    )
    assert route.entity_binding == "none"
    assert route.target_entity is None


def test_contradictory_set_refinement_fails_closed_instead_of_raising():
    prior = StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=AssetSearchFilters(
            role="Linux Server",
            model_confidence_min=0.95,
        ),
    )
    state = SessionRoutingState(
        structured_query_context=StructuredQueryContext.create(
            query_identity=structured_query_identity(
                prior, active_graph_version="graph-v1"
            ),
            active_graph_version="graph-v1",
            query=prior,
            matched_total=2,
            returned_count=2,
            source_request_id="request-1",
            retrieved_at="2026-09-13T00:00:00+00:00",
            created_at="2026-09-13T00:00:01+00:00",
        )
    )

    route = StructuredAwareFallbackRouter().route(
        "Which of them have model confidence under 90%?",
        EntityResolution(status="none"),
        state,
    )

    assert route.intent == "unclear"
    assert route.reason == "deterministic_structured_parse_unavailable"


def test_group_them_followup_reuses_prior_set_and_groups_by_vendor():
    prior = StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=AssetSearchFilters(role="Linux Server"),
    )
    state = SessionRoutingState(
        structured_query_context=StructuredQueryContext.create(
            query_identity=structured_query_identity(
                prior, active_graph_version="graph-v1"
            ),
            active_graph_version="graph-v1",
            query=prior,
            matched_total=14,
            returned_count=8,
            source_request_id="request-1",
            retrieved_at="2026-09-13T00:00:00+00:00",
            created_at="2026-09-13T00:00:01+00:00",
        )
    )

    route = StructuredAwareFallbackRouter().route(
        "Group them by vendor.",
        EntityResolution(status="none"),
        state,
    )

    assert route.intent == "asset_aggregate"
    assert route.structured_result_reference.kind == "set_query"
    assert route.structured_query is not None
    assert route.structured_query.filters.role == "Linux Server"
    assert route.structured_query.group_by.value == "vendor"


def test_prior_aggregate_group_membership_becomes_current_bounded_ip_search():
    prior = StructuredQuerySpec(
        mode=StructuredQueryMode.AGGREGATE,
        filters=AssetSearchFilters(status="CONFIRMED"),
        operation="group_count",
        group_by="vendor",
    )
    state = SessionRoutingState(
        structured_query_context=StructuredQueryContext.create(
            query_identity=structured_query_identity(
                prior, active_graph_version="graph-v1"
            ),
            active_graph_version="graph-v1",
            query=prior,
            count=12,
            aggregate_groups=(
                StructuredAggregateGroupRef(value="VMware", count=9),
                StructuredAggregateGroupRef(value="Fortinet", count=3),
            ),
            source_request_id="request-1",
            retrieved_at="2026-09-13T00:00:00+00:00",
            created_at="2026-09-13T00:00:01+00:00",
        )
    )

    route = StructuredAwareFallbackRouter().route(
        "Which IPs are in the VMware group?",
        EntityResolution(status="resolved"),
        state,
    )

    assert route.intent == "asset_search"
    assert route.structured_result_reference.kind == "set_query"
    assert route.structured_query is not None
    assert route.structured_query.filters.status == "confirmed"
    assert route.structured_query.filters.vendor == "vmware"
    assert route.structured_query.sort.value == "ip"
    assert route.entity_binding == "none"


def test_unknown_prior_aggregate_group_membership_fails_closed():
    prior = StructuredQuerySpec(
        mode=StructuredQueryMode.AGGREGATE,
        operation="group_count",
        group_by="vendor",
    )
    state = SessionRoutingState(
        structured_query_context=StructuredQueryContext.create(
            query_identity=structured_query_identity(
                prior, active_graph_version="graph-v1"
            ),
            active_graph_version="graph-v1",
            query=prior,
            count=9,
            aggregate_groups=(StructuredAggregateGroupRef(value="VMware", count=9),),
            source_request_id="request-1",
            retrieved_at="2026-09-13T00:00:00+00:00",
            created_at="2026-09-13T00:00:01+00:00",
        )
    )

    route = StructuredAwareFallbackRouter().route(
        "Which IPs are in the UnknownVendor group?",
        EntityResolution(status="resolved"),
        state,
    )

    assert route.intent == "unclear"
    assert route.reason == "deterministic_structured_parse_unavailable"
    assert route.target_entity is None


def test_structured_set_continuation_bypasses_pre_router_long_term_memory_call():
    prior = StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=AssetSearchFilters(role="Linux Server"),
    )
    routing_state = SessionRoutingState(
        structured_query_context=StructuredQueryContext.create(
            query_identity=structured_query_identity(
                prior, active_graph_version="graph-v1"
            ),
            active_graph_version="graph-v1",
            query=prior,
            matched_total=14,
            returned_count=8,
            source_request_id="request-1",
            retrieved_at="2026-09-13T00:00:00+00:00",
            created_at="2026-09-13T00:00:01+00:00",
        )
    )
    calls = []
    service = SimpleNamespace(
        settings=get_settings(),
        routing_state_store=SimpleNamespace(get=lambda _session: routing_state),
        memory_store=SimpleNamespace(recent_for_routing=lambda *_args: []),
        entity_resolver=StructuredAwareEntityResolver(),
        retrieve_long_term_memory=lambda **kwargs: calls.append(kwargs),
    )
    identity = RequestIdentity.resolve(
        user_id="user-1",
        conversation_id="conversation-1",
        session_id="session-1",
        request_id="request-2",
    )

    update = CopilotWorkflowNodes(service).resolve_entities({
        "session_id": identity.session_id,
        "request_id": identity.request_id,
        "request_identity": identity,
        "message": "Group them by vendor.",
        "ui_context": {"selected_ip": "192.168.30.1"},
    })

    assert calls == []
    assert update["long_term_memory_selection"] is None
    assert update["resolved_entities"].status == "none"


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


def test_product_summary_hydration_failure_logs_safe_contract_stage(caplog):
    class Client:
        def search_ltm(self, **_kwargs):
            return {"records": [{"memoryId": "m-1", "userId": "user-1"}]}

        def get_ltm(self, **_kwargs):
            return {"memory": {"memoryId": "m-1", "userId": "user-1"}}

    store = ProductLongTermMemoryStore(Client())
    with caplog.at_level(logging.ERROR):
        records = store.list(
            user_id="user-1",
            request_id="safe-diagnostics",
            purpose="active_inventory",
        )

    assert records == ()
    assert "malformed_product_memory_skipped" in store.consume_read_limitations(
        request_id="safe-diagnostics",
        purpose="active_inventory",
    )

    text = caplog.text
    assert "event=product_ltm_contract_failed" in text
    assert "phase=canonical_hydration" in text
    assert "reason=missing_fields:" in text
    assert "memory_id=m-1" in text
    assert "hydration_attempted=true" in text
    assert "payload_logged=false" in text
    assert "statement" in text
