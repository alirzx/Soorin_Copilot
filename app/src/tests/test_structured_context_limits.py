"""Focused configuration regressions for structured Asset model context."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from unittest.mock import patch

import src.config.settings as settings_module
from src.config.settings import get_settings
from src.core.agent.contracts import (
    StructuredAssetAggregateEvidence,
    StructuredAssetSearchEvidence,
)
from src.core.context.composer import ContextComposer
from src.core.context.models import (
    CopilotContextPackage,
    EntityResolution,
    GraphProviderResult,
    ProviderProvenance,
    approx_tokens,
)


def _settings(**overrides: object):
    return replace(get_settings(), **overrides)


def _search_graph(row_count: int, *, verbose: bool) -> GraphProviderResult:
    rows = tuple(
        {
            "graph_key": f"linux-{index}",
            "ip": f"198.51.100.{index + 1}",
            "asset_name": (
                f"linux-server-{index}-" + ("a" * 140 if verbose else "")
            ),
            "status": "CONFIRMED",
            "role": "Linux Server" + (" role-detail" * 10 if verbose else ""),
            "vendor": "Example Vendor" + (" vendor-detail" * 9 if verbose else ""),
            "product": "Enterprise Linux" + (" product-detail" * 9 if verbose else ""),
        }
        for index in range(row_count)
    )
    evidence = StructuredAssetSearchEvidence(
        capability="graph.search_assets",
        query_identity="search-linux-servers",
        normalized_filters={"role": "Linux Server"},
        active_graph_version="graph-v1",
        sort="graph_key",
        direction="asc",
        matched_total=row_count,
        returned_count=row_count,
        truncated=False,
        rows=rows,
        retrieved_at="2026-09-17T00:00:00+00:00",
    )
    return _graph_result(evidence, "asset_search")


def _aggregate_graph(group_count: int) -> GraphProviderResult:
    groups = tuple(
        {
            "value": f"Vendor {index} / Linux Server, SSH Server",
            "count": index + 1,
            "group_values": {
                "vendor": f"Vendor {index}",
                "roles": ["Linux Server", "SSH Server"],
            },
            "member_ips": [f"203.0.113.{(index % 200) + 1}"],
            "member_ips_truncated": False,
            "percentage_of_total": round((index + 1) / group_count * 100, 2),
        }
        for index in range(group_count)
    )
    evidence = StructuredAssetAggregateEvidence(
        capability="graph.aggregate_assets",
        query_identity="aggregate-vendor-roles",
        normalized_filters={},
        active_graph_version="graph-v1",
        operation="group_count",
        group_by="vendor",
        group_by_fields=("vendor", "roles"),
        count=sum(group["count"] for group in groups),
        groups=groups,
        truncated=False,
        retrieved_at="2026-09-17T00:00:00+00:00",
    )
    return _graph_result(evidence, "asset_aggregate")


def _graph_result(evidence: object, scope: str) -> GraphProviderResult:
    return GraphProviderResult(
        provider="graph",
        status="available",
        context={
            "source_capability": evidence.capability,
            "requested_scope": scope,
            "context_identity": evidence.query_identity,
            "structured_asset_set": evidence,
            "retrieval_complete": True,
            "retrieval_truncated": False,
            "requested_scope_complete": True,
            "complete_for_user_request": True,
        },
        provenance=ProviderProvenance(
            source="neo4j_active_organizational_projection",
            status="available",
        ),
    )


def _structured_payload(text: str) -> dict[str, object]:
    return json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])


def _compose_graph(graph: GraphProviderResult, **settings_overrides: object):
    composer = ContextComposer(_settings(**settings_overrides))
    package = CopilotContextPackage(
        entities=EntityResolution(status="none"),
        graph=graph,
    )
    text = composer._compose_graph(package, request_id="structured-limits")
    return composer, text, _structured_payload(text)


def test_search_token_budget_6000_includes_all_19_production_like_rows() -> None:
    low_graph = _search_graph(19, verbose=True)
    _low, low_text, low_payload = _compose_graph(
        low_graph,
        context_structured_asset_search_max_tokens=1800,
        context_structured_asset_search_max_rows=200,
    )
    high_graph = _search_graph(19, verbose=True)
    _high, high_text, high_payload = _compose_graph(
        high_graph,
        context_structured_asset_search_max_tokens=6000,
        context_structured_asset_search_max_rows=200,
    )

    assert low_payload["coverage"]["rows_in_model_context"] < 19
    assert high_payload["coverage"] == {
        "matched_total": 19,
        "rows_retrieved": 19,
        "rows_in_model_context": 19,
        "rows_omitted_from_model_context": 0,
        "retrieval_truncated": False,
        "context_truncated": False,
    }
    assert approx_tokens(low_text) <= 1800
    assert approx_tokens(high_text) <= 6000


def test_search_max_rows_replaces_old_twenty_row_ceiling() -> None:
    limited_graph = _search_graph(30, verbose=False)
    _limited, _limited_text, limited_payload = _compose_graph(
        limited_graph,
        context_structured_asset_search_max_tokens=10000,
        context_structured_asset_search_max_rows=10,
    )
    expanded_graph = _search_graph(30, verbose=False)
    _expanded, _expanded_text, expanded_payload = _compose_graph(
        expanded_graph,
        context_structured_asset_search_max_tokens=10000,
        context_structured_asset_search_max_rows=30,
    )

    assert limited_payload["coverage"]["rows_in_model_context"] == 10
    assert limited_payload["coverage"]["rows_omitted_from_model_context"] == 20
    assert limited_payload["coverage"]["context_truncated"] is True
    assert expanded_payload["coverage"]["rows_in_model_context"] == 30
    assert expanded_payload["coverage"]["rows_omitted_from_model_context"] == 0
    assert expanded_payload["coverage"]["context_truncated"] is False


def test_aggregate_budget_8000_includes_all_50_compact_groups() -> None:
    low_graph = _aggregate_graph(50)
    _low, low_text, low_payload = _compose_graph(
        low_graph,
        context_structured_asset_aggregate_max_tokens=700,
    )
    high_graph = _aggregate_graph(50)
    _high, high_text, high_payload = _compose_graph(
        high_graph,
        context_structured_asset_aggregate_max_tokens=8000,
    )

    assert low_payload["result"]["groups_in_model_context"] < 50
    assert high_payload["result"]["groups_retrieved"] == 50
    assert high_payload["result"]["groups_in_model_context"] == 50
    assert high_payload["result"]["groups_omitted_from_model_context"] == 0
    assert high_payload["result"]["context_truncated"] is False
    assert approx_tokens(low_text) <= 700
    assert approx_tokens(high_text) <= 8000


def test_global_context_budget_still_caps_large_structured_allowance() -> None:
    graph = _aggregate_graph(50)
    composer = ContextComposer(
        _settings(
            context_structured_asset_aggregate_max_tokens=8000,
            llm_context_window_tokens=4200,
            llm_reserved_output_tokens=1000,
            llm_context_safety_margin_tokens=500,
            llm_token_estimate_multiplier=1.0,
        )
    )
    package = CopilotContextPackage(
        entities=EntityResolution(status="none"),
        graph=graph,
    )

    text = composer.compose(
        package,
        base_input_tokens=700,
        reserved_output_tokens=1000,
        request_id="structured-global-cap",
    )

    assert graph.context["model_context_token_cap"] < 8000
    assert graph.context["model_context_token_cap"] <= composer.last_budget["max_dynamic_tokens"]
    assert graph.context["model_context_token_estimate"] <= graph.context["model_context_token_cap"]
    assert approx_tokens(text) <= composer.last_budget["max_dynamic_tokens"]


def test_structured_context_environment_values_and_source_fallbacks(tmp_path: Path) -> None:
    missing_env = tmp_path / "missing.env"
    configured = {
        "SOORIN_CONTEXT_STRUCTURED_ASSET_SEARCH_MAX_TOKENS": "6000",
        "SOORIN_CONTEXT_STRUCTURED_ASSET_AGGREGATE_MAX_TOKENS": "8000",
        "SOORIN_CONTEXT_STRUCTURED_ASSET_SEARCH_MAX_ROWS": "200",
    }

    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(settings_module, "ENV_PATH", missing_env),
        patch.object(settings_module, "LEGACY_ENV_PATH", missing_env),
    ):
        get_settings.cache_clear()
        fallback = get_settings()
    get_settings.cache_clear()

    with (
        patch.dict(os.environ, configured, clear=True),
        patch.object(settings_module, "ENV_PATH", missing_env),
        patch.object(settings_module, "LEGACY_ENV_PATH", missing_env),
    ):
        get_settings.cache_clear()
        deployed = get_settings()
    get_settings.cache_clear()

    assert (
        fallback.context_structured_asset_search_max_tokens,
        fallback.context_structured_asset_aggregate_max_tokens,
        fallback.context_structured_asset_search_max_rows,
    ) == (1800, 700, 20)
    assert (
        deployed.context_structured_asset_search_max_tokens,
        deployed.context_structured_asset_aggregate_max_tokens,
        deployed.context_structured_asset_search_max_rows,
    ) == (6000, 8000, 200)
