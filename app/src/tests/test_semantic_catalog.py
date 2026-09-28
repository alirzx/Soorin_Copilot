"""Focused regressions for open semantic routing and active graph vocabulary."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.config.settings import get_settings
from src.core.context import EntityResolver, SemanticIntentRouter
from src.core.context.intent import SemanticIntentRouter as BaseSemanticIntentRouter
from src.core.context.models import EntityResolution
from src.core.context.semantic_catalog import SemanticCatalogProvider
from src.core.graph.neo4j import Neo4jGraphRepository
from src.core.graph.structured import StructuredQuerySpec
from src.core.llm.providers.base import LLMProviderResult
from src.core.memory.routing_state import SessionRoutingState


class _GraphService:
    def __init__(self, version: str = "v1") -> None:
        self.version = version
        self.version_calls = 0
        self.value_calls = 0
        self.canonical_calls: list[tuple[str, dict[str, tuple[str, ...]]]] = []
        self.values = {
            "role": ("Domain Controller", "domain controller", "DNS", ""),
            "roles": ("Authentication", None, "DNS"),
            "product": tuple(f"Product {index}" for index in range(30)),
            "vendor": tuple(f"Vendor {index}" for index in range(30)),
        }

    def semantic_catalog_version(self) -> str:
        self.version_calls += 1
        return self.version

    def semantic_catalog_values(self, version: str, *, per_field_limit: int):
        assert version == self.version
        assert per_field_limit <= 64
        self.value_calls += 1
        return self.values

    def canonicalize_semantic_values(
        self,
        version: str,
        values: dict[str, tuple[str, ...]],
    ) -> dict[str, dict[str, str]]:
        self.canonical_calls.append((version, values))
        available = {
            "role": {"domain controller": "Domain Controller"},
            "product": {"rare appliance": "Rare Appliance"},
        }
        return {
            field: {
                value.casefold(): available[field][value.casefold()]
                for value in requested
                if value.casefold() in available.get(field, {})
            }
            for field, requested in values.items()
            if any(
                value.casefold() in available.get(field, {})
                for value in requested
            )
        }


class _FailingGraphService:
    def __init__(self) -> None:
        self.calls = 0

    def semantic_catalog_version(self) -> str:
        self.calls += 1
        raise RuntimeError("neo4j unavailable")


class _LLM:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, **kwargs) -> LLMProviderResult:
        self.calls.append({"messages": messages, **kwargs})
        return LLMProviderResult(
            text=json.dumps(self.payload),
            provider="fake",
            model="fake",
            finish_reason="stop",
            usage={},
            status_code=200,
        )


def _settings():
    return replace(
        get_settings(),
        intent_router_enabled=True,
        intent_router_retry_enabled=True,
        llm_usage_reporting_enabled=False,
    )


def _general_payload() -> dict[str, object]:
    return {
        "intent": "general_knowledge",
        "scope": "none",
        "direction": "none",
        "depth": 0,
        "requires_graph": False,
        "requires_detection": False,
        "requires_asset_profile": False,
        "requires_multiple_entities": False,
        "is_followup": False,
        "reason": "In-scope infrastructure question.",
    }


def _search_payload() -> dict[str, object]:
    return {
        "intent": "asset_search",
        "scope": "none",
        "direction": "none",
        "depth": 0,
        "requires_graph": True,
        "requires_detection": False,
        "requires_asset_profile": False,
        "requires_knowledge": False,
        "structured_query": {
            "mode": "search",
            "filters": {"role": "Domain Controller"},
        },
        "structured_result_reference": None,
        "entity_binding": "none",
        "requires_multiple_entities": False,
        "is_followup": False,
        "reason": "Semantic Asset-set request.",
    }


def _aggregate_payload() -> dict[str, object]:
    payload = _search_payload()
    payload["intent"] = "asset_aggregate"
    payload["structured_query"] = {
        "mode": "aggregate",
        "filters": {},
        "operation": "group_count",
        "group_by": "vendor",
    }
    payload["reason"] = "Semantic grouped Asset count."
    return payload


def test_catalog_is_bounded_deduplicated_cached_and_version_invalidated(caplog) -> None:
    graph = _GraphService()
    provider = SemanticCatalogProvider(
        graph,
        ttl_seconds=60,
        per_field_limit=8,
        product_limit=3,
        total_value_limit=10,
    )

    first = provider.get(request_id="first")
    second = provider.get(request_id="second")

    assert first == second
    assert graph.version_calls == 2
    assert graph.value_calls == 1
    assert first["fields"]["role"] == ["Domain Controller", "DNS"]
    assert len(first["fields"]["product"]) <= 3
    assert sum(len(values) for values in first["fields"].values()) <= 10

    graph.version = "v2"
    refreshed = provider.get(request_id="third")

    assert refreshed["active_graph_version"] == "v2"
    assert graph.value_calls == 2
    assert "Domain Controller" not in caplog.text
    assert "Product 0" not in caplog.text


def test_catalog_ttl_refresh_and_neo4j_failure_are_nonfatal() -> None:
    graph = _GraphService()
    provider = SemanticCatalogProvider(graph, ttl_seconds=0)

    assert provider.get()["available"] is True
    assert provider.get()["available"] is True
    assert graph.value_calls == 2
    failing = _FailingGraphService()
    unavailable = SemanticCatalogProvider(failing)
    assert unavailable.get() == {"available": False}
    assert unavailable.get() == {"available": False}
    assert failing.calls == 1


def test_catalog_bounds_fields_fairly_and_omits_overlong_categories() -> None:
    graph = _GraphService()
    graph.values = {
        field: (f"{field}-value",)
        for field in (
            "suggested_type",
            "role",
            "roles",
            "vendor",
            "product",
            "tag",
            "sub_tag",
            "status",
            "enrichment_status",
        )
    }
    graph.values["role"] = ("x" * 40, "valid-role")
    provider = SemanticCatalogProvider(
        graph,
        per_field_limit=4,
        total_value_limit=9,
        max_value_chars=32,
    )

    fields = provider.get()["fields"]

    assert fields["role"] == ["valid-role"]
    assert fields["sub_tag"] == ["sub_tag-value"]
    assert fields["status"] == ["status-value"]
    assert fields["enrichment_status"] == ["enrichment_status-value"]
    assert "x" * 20 not in fields["role"]


def test_active_graph_canonicalization_is_exact_and_not_limited_by_prompt_sample() -> None:
    graph = _GraphService(version="graph-v9")
    provider = SemanticCatalogProvider(graph)
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {
            "role": "domain controller",
            "product": "rare appliance",
            "vendor": "No Such Vendor",
        },
    })

    canonical = provider.canonicalize_query(query, request_id="canonical")

    assert canonical.filters.role == "Domain Controller"
    assert canonical.filters.product == "Rare Appliance"
    assert canonical.filters.vendor == "No Such Vendor"
    assert graph.canonical_calls == [(
        "graph-v9",
        {
            "role": ("domain controller",),
            "vendor": ("No Such Vendor",),
            "product": ("rare appliance",),
        },
    )]


def test_router_context_receives_catalog_without_making_it_an_input_allow_list() -> None:
    llm = _LLM(_general_payload())
    router = BaseSemanticIntentRouter(_settings(), llm)  # type: ignore[arg-type]
    router.semantic_catalog_provider = SemanticCatalogProvider(_GraphService())

    decision = router.classify(
        "Explain why a Kerberos service endpoint matters",
        EntityResolution(status="none"),
        SessionRoutingState(),
        request_id="catalog-router",
    )

    sent = json.loads(llm.calls[0]["messages"][1]["content"])
    assert decision.fallback_used is False
    assert sent["semantic_catalog"]["available"] is True
    assert "role" in sent["semantic_catalog"]["fields"]


def test_router_still_runs_when_catalog_is_unavailable() -> None:
    llm = _LLM(_general_payload())
    router = BaseSemanticIntentRouter(_settings(), llm)  # type: ignore[arg-type]
    router.semantic_catalog_provider = SemanticCatalogProvider(_FailingGraphService())

    decision = router.classify(
        "Explain a Kerberos service endpoint",
        EntityResolution(status="none"),
        SessionRoutingState(),
        request_id="catalog-unavailable-router",
    )

    sent = json.loads(llm.calls[0]["messages"][1]["content"])
    assert decision.fallback_used is False
    assert sent["semantic_catalog"] == {"available": False}


@pytest.mark.parametrize(
    "message",
    (
        "list DCs",
        "identify the domain controllers",
        "Enumerate every AD DC in the estate",
        "which assets are Domain Controllers?",
        "show our DC assets",
    ),
)
def test_semantically_equivalent_set_wording_defers_to_router_and_drops_ui(
    message: str,
) -> None:
    ui = {"selected_ip": "192.0.2.44"}
    entities = EntityResolver().resolve(message, ui, SessionRoutingState())
    assert entities.primary_entity is not None
    assert entities.primary_entity.source == "ui"

    llm = _LLM(_search_payload())
    router = SemanticIntentRouter(_settings(), llm)  # type: ignore[arg-type]
    assert "non-exhaustive hints" in router.system_prompt
    assert "Do not invent aliases outside" not in router.system_prompt
    decision = router.classify(
        message,
        entities,
        SessionRoutingState(),
        ui_context=ui,
        request_id="novel-set-wording",
    )

    assert decision.intent == "asset_search"
    assert decision.entity_binding == "none"
    assert decision.materialized_entities == ()
    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "message",
    (
        "count assets by vendor",
        "break down assets by vendor",
        "show the distribution by vendor",
        "number of assets per vendor",
    ),
)
def test_semantically_equivalent_aggregate_wording_has_one_typed_result(
    message: str,
) -> None:
    llm = _LLM(_aggregate_payload())
    router = SemanticIntentRouter(_settings(), llm)  # type: ignore[arg-type]

    decision = router.classify(
        message,
        EntityResolution(status="none"),
        SessionRoutingState(),
        request_id="aggregate-paraphrase",
    )

    assert decision.intent == "asset_aggregate"
    assert decision.structured_query is not None
    assert decision.structured_query.group_by is not None
    assert decision.structured_query.group_by.value == "vendor"
    assert decision.entity_binding == "none"


class _Session:
    def __init__(self) -> None:
        self.query = ""
        self.params: dict[str, object] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def run(self, query, **params):
        self.query = str(query)
        self.params = params
        return [{"field": "roles", "values": ["DNS", "Authentication"]}]


class _Driver:
    def __init__(self) -> None:
        self.last_session = _Session()

    def session(self):
        self.last_session = _Session()
        return self.last_session


def test_repository_catalog_query_is_bound_to_active_projection_and_flattens_roles() -> None:
    driver = _Driver()
    repository = Neo4jGraphRepository(driver, _settings())  # type: ignore[arg-type]

    values = repository.semantic_catalog_values("graph-v7", per_field_limit=12)

    assert values == {"roles": ("DNS", "Authentication")}
    assert driver.last_session.params["active_graph_version"] == "graph-v7"
    assert driver.last_session.params["per_field_limit"] == 12
    assert "GraphMetadata" in driver.last_session.query
    assert "a:Asset {graph_version: $active_graph_version}" in driver.last_session.query
    assert "coalesce(a.roles, [])" in driver.last_session.query


class _CanonicalSession(_Session):
    def run(self, query, **params):
        self.query = str(query)
        self.params = params
        return [
            {
                "field": "role",
                "requested": "domain controller",
                "canonical": "Domain Controller",
            }
        ]


class _CanonicalDriver(_Driver):
    def session(self):
        self.last_session = _CanonicalSession()
        return self.last_session


def test_repository_canonicalization_is_batched_case_insensitive_and_version_bound() -> None:
    driver = _CanonicalDriver()
    repository = Neo4jGraphRepository(driver, _settings())  # type: ignore[arg-type]

    resolved = repository.canonicalize_semantic_values(
        "graph-v11",
        (("role", "domain controller"), ("vendor", "VMware")),
    )

    assert resolved == {"role": {"domain controller": "Domain Controller"}}
    assert driver.last_session.params["active_graph_version"] == "graph-v11"
    assert driver.last_session.params["lookups"] == [
        {"field": "role", "value": "domain controller"},
        {"field": "vendor", "value": "VMware"},
    ]
    assert "UNWIND $lookups" in driver.last_session.query
    assert "toLower(value) = toLower(requested)" in driver.last_session.query
    assert "graph_version: $active_graph_version" in driver.last_session.query
