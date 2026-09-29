"""Offline tests for the optional Soorin observability foundation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families

from src.api.auth import verify_api_key
from src.api.main import create_app
from src.config.settings import get_settings
from src.core.observability.metrics import (
    FORBIDDEN_LABEL_NAMES,
    HTTP_DURATION_BUCKETS,
    LLM_DURATION_BUCKETS,
    WORKFLOW_DURATION_BUCKETS,
    SoorinMetrics,
    _error_class,
)


ROOT = Path(__file__).resolve().parents[3]


def metric_names(metrics: SoorinMetrics) -> set[str]:
    return {family.name for family in text_string_to_metric_families(metrics.render().decode("utf-8"))}


def test_metrics_registry_exposes_expected_prometheus_families() -> None:
    metrics = SoorinMetrics(registry=CollectorRegistry())
    metrics.observe_http("/health", "GET", 200, 0.01)
    metrics.observe_copilot("completed", "direct", 0.2)
    metrics.observe_stage("route", 0.03)
    metrics.observe_tool("asset.get_profile", ("overview",), "ok", 0.04)
    metrics.observe_memory_retrieval("exact", "hit")
    metrics.observe_memory_policy("reuse", "skip", "asset.get_profile")
    metrics.observe_context("profile", 150, 100)
    metrics.observe_stream(duration_seconds=0.5, first_output_seconds=0.1, status="completed")
    metrics.observe_product("memory_search", duration_seconds=0.2, status_code=200)
    metrics.observe_memory_lifecycle("promoted", "success")
    metrics.observe_memory_revision_conflict("memory_transition")
    metrics.observe_memory_canonical_reload("product", "success")
    metrics.observe_memory_vector("search", "failure")
    metrics.observe_workflow_fallback("routing")
    metrics.observe_investigator_context(3200, 2100)
    metrics.observe_agent_equivalent_action("equivalent_evidence_already_available")
    metrics.observe_agent_evidence_references(created=2, changed=1)
    metrics.observe_agent_material_progress(True)
    metrics.observe_agent_request_calls(llm_calls=4, capability_calls=2)
    metrics.observe_agent_premature_finish_rejection()
    metrics.observe_agent_authority_invalid("entity")
    metrics.observe_agent_malformed_decision("schema")
    metrics.observe_evidence_review("adaptive", "sufficient")
    metrics.graph_enrichment_cycle_started("scheduled")
    metrics.observe_graph_enrichment_assets(
        "scheduled", attempted=2, succeeded=1, unavailable=1, updated=1
    )
    metrics.observe_graph_enrichment_product(
        "scheduled", outcome="success", duration_seconds=0.04
    )
    metrics.set_graph_enrichment_lease_owned(True)
    metrics.set_graph_enrichment_backlog(
        {"pending": 1, "stale": 2, "error": 3, "unavailable": 4, "backlog": 10}
    )
    metrics.observe_graph_enrichment_cycle(
        "scheduled", outcome="completed", duration_seconds=0.2, timestamp=1.0
    )
    metrics.observe_view("asset.get_profile", ("overview",))
    metrics.observe_llm(
        SimpleNamespace(
            purpose="intent_router",
            provider="arvan",
            model="test-model",
            status="success",
            latency_ms=25,
            input_tokens=40,
            output_tokens=10,
        )
    )

    names = metric_names(metrics)
    assert {
        "soorin_http_requests",
        "soorin_copilot_requests",
        "soorin_copilot_request_duration_by_mode_seconds",
        "soorin_workflow_stage_duration_seconds",
        "soorin_llm_requests",
        "soorin_llm_input_tokens",
        "soorin_tool_calls",
        "soorin_memory_retrieval",
        "soorin_memory_sufficiency",
        "soorin_context_estimated_tokens",
        "soorin_context_token_savings",
        "soorin_context_estimated_tokens_per_request",
        "soorin_context_compacted_tokens_per_request",
        "soorin_product_view_selected",
        "soorin_product_requests",
        "soorin_stream_completions",
        "soorin_memory_lifecycle_events",
        "soorin_memory_revision_conflicts",
        "soorin_memory_canonical_reload",
        "soorin_memory_vector_operations",
        "soorin_workflow_fallbacks",
        "soorin_investigator_context_tokens",
        "soorin_investigator_context_tokens_before_compaction",
        "soorin_investigator_context_token_savings",
        "soorin_agent_equivalent_actions_blocked",
        "soorin_agent_evidence_references",
        "soorin_agent_material_progress_turns",
        "soorin_agent_llm_calls_per_request",
        "soorin_agent_capability_calls_per_request",
        "soorin_agent_premature_finish_rejections",
        "soorin_agent_authority_invalid_proposals",
        "soorin_agent_malformed_decisions",
        "soorin_evidence_review_outcomes",
        "soorin_graph_enrichment_scheduler_cycles",
        "soorin_graph_enrichment_assets",
        "soorin_graph_enrichment_product_overview_requests",
        "soorin_graph_enrichment_scheduler_cycle_duration_seconds",
        "soorin_graph_enrichment_product_overview_duration_seconds",
        "soorin_graph_enrichment_scheduler_running",
        "soorin_graph_enrichment_scheduler_owns_lease",
        "soorin_graph_enrichment_backlog_assets",
        "soorin_graph_enrichment_last_run_timestamp_seconds",
        "soorin_graph_enrichment_last_success_timestamp_seconds",
    } <= names


def test_histograms_use_explicit_workload_specific_buckets() -> None:
    metrics = SoorinMetrics(registry=CollectorRegistry())
    assert tuple(metrics.http_duration._upper_bounds[:-1]) == HTTP_DURATION_BUCKETS
    assert tuple(metrics.llm_duration._upper_bounds[:-1]) == LLM_DURATION_BUCKETS
    assert tuple(metrics.copilot_duration._upper_bounds[:-1]) == WORKFLOW_DURATION_BUCKETS
    assert tuple(metrics.copilot_duration_by_mode._upper_bounds[:-1]) == WORKFLOW_DURATION_BUCKETS
    assert metrics.http_duration._upper_bounds != metrics.llm_duration._upper_bounds
    assert metrics.context_estimated_tokens_per_request._upper_bounds[-2] == 65536


def test_product_and_stream_label_values_are_bounded() -> None:
    metrics = SoorinMetrics(registry=CollectorRegistry())
    metrics.observe_product(
        "https://product.invalid/users/individual-id",
        duration_seconds=0.2,
        status_code=503,
    )
    metrics.observe_stream(duration_seconds=0.3, status="arbitrary-client-value")
    metrics.observe_graph_enrichment_product(
        "192.0.2.1", outcome="request-123", duration_seconds=0.1
    )
    rendered = metrics.render().decode("utf-8")
    assert 'soorin_product_requests_total{operation="other",status_class="5xx"} 1.0' in rendered
    assert 'soorin_errors_total{error_class="unavailable",subsystem="product"} 1.0' in rendered
    assert 'soorin_stream_completions_total{status="error"} 1.0' in rendered
    assert (
        'soorin_graph_enrichment_product_overview_requests_total{outcome="error",trigger="other"} 1.0'
        in rendered
    )
    assert "192.0.2.1" not in rendered
    assert "request-123" not in rendered


def test_adaptive_equivalence_and_progress_labels_are_bounded() -> None:
    metrics = SoorinMetrics(registry=CollectorRegistry())
    metrics.observe_agent_equivalent_action("192.0.2.1")
    metrics.observe_agent_material_progress(False)
    metrics.observe_agent_evidence_references(created=1, changed=1)
    metrics.observe_evidence_review("request-123", "arbitrary-result")
    metrics.observe_agent_authority_invalid("192.0.2.1")
    metrics.observe_agent_malformed_decision("raw-model-output")
    metrics.observe_copilot("completed", "planner", 0.1)
    rendered = metrics.render().decode("utf-8")
    assert 'soorin_agent_equivalent_actions_blocked_total{reason="other"} 1.0' in rendered
    assert 'soorin_agent_material_progress_turns_total{progress="no"} 1.0' in rendered
    assert 'soorin_agent_evidence_references_total{change="created"} 1.0' in rendered
    assert 'soorin_agent_evidence_references_total{change="changed"} 1.0' in rendered
    assert (
        'soorin_evidence_review_outcomes_total{mode="unknown",outcome="safe_failure"} 1.0'
        in rendered
    )
    assert 'soorin_agent_authority_invalid_proposals_total{reason="other"} 1.0' in rendered
    assert 'soorin_agent_malformed_decisions_total{reason="other"} 1.0' in rendered
    assert 'workflow_mode="unknown"' in rendered
    assert "192.0.2.1" not in rendered


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (400, "validation"),
        (422, "validation"),
        (401, "authentication"),
        (403, "authentication"),
        (408, "timeout"),
        (504, "timeout"),
        (409, "conflict"),
        (429, "rate_limit"),
        (502, "unavailable"),
        (503, "unavailable"),
        (500, "upstream"),
        ("ConnectionError", "connection"),
    ],
)
def test_error_classification_is_bounded_and_http_aware(value, expected) -> None:
    assert _error_class(value) == expected


def test_metric_definitions_reject_high_cardinality_labels() -> None:
    metrics = SoorinMetrics(registry=CollectorRegistry())
    for collector in metrics.registry._collector_to_names:
        labels = set(getattr(collector, "_labelnames", ()))
        assert labels.isdisjoint(FORBIDDEN_LABEL_NAMES)


def test_disabled_metrics_are_no_op() -> None:
    metrics = SoorinMetrics(enabled=False, registry=CollectorRegistry())
    before = metrics.render()
    metrics.observe_http("/health", "GET", 200, 0.01)
    metrics.observe_copilot("completed", "direct", 0.2)
    metrics.observe_error("http", "timeout")
    assert metrics.render() == before


def test_metrics_endpoint_is_authenticated_and_makes_no_external_call() -> None:
    app = create_app()
    route = next(item for item in app.routes if getattr(item, "path", "") == "/metrics")
    dependencies = [item.call for item in route.dependant.dependencies]
    assert verify_api_key in dependencies
    with patch(
        "src.api.dependencies.ProductApiClient._request_json",
        side_effect=AssertionError("external call"),
    ):
        response = route.endpoint(_auth=None)
    assert response.status_code == 200
    assert response.media_type.startswith("text/plain")
    text = response.body.decode("utf-8")
    assert "soorin_http_requests_total" in text
    list(text_string_to_metric_families(text))


def test_metrics_endpoint_is_absent_when_disabled() -> None:
    disabled = replace(get_settings(), metrics_enabled=False)
    isolated_metrics = SoorinMetrics(enabled=False, registry=CollectorRegistry())
    with (
        patch("src.api.main.get_settings", return_value=disabled),
        patch("src.api.main.configure_metrics", return_value=isolated_metrics),
    ):
        app = create_app()
    assert "/metrics" not in {getattr(route, "path", "") for route in app.routes}


def test_observability_profile_and_provisioning_are_static_and_optional() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    for name in ("prometheus", "loki", "alloy", "grafana"):
        assert services[name]["profiles"] == ["observability"]
    assert "profiles" not in services["api"]
    assert "profiles" not in services["ui"]
    assert set(services["ui"]["depends_on"]) == {"api"}

    prometheus = ROOT / "observability/prometheus/prometheus.yml"
    loki = ROOT / "observability/loki/loki-config.yml"
    alloy = ROOT / "observability/alloy/config.alloy"
    datasources = ROOT / "observability/grafana/provisioning/datasources/datasources.yml"
    provider = ROOT / "observability/grafana/provisioning/dashboards/dashboards.yml"
    dashboard = ROOT / "observability/grafana/dashboards/soorin-copilot-overview.json"
    assert all(path.exists() for path in (prometheus, loki, alloy, datasources, provider, dashboard))
    assert "api:6998" in prometheus.read_text(encoding="utf-8")
    assert "credentials_file" in prometheus.read_text(encoding="utf-8")
    prometheus_config = yaml.safe_load(prometheus.read_text(encoding="utf-8"))
    target_labels = prometheus_config["scrape_configs"][0]["static_configs"][0]["labels"]
    assert target_labels["environment"] == "__SOORIN_OBSERVABILITY_ENVIRONMENT__"
    rendered_prometheus = yaml.safe_load(
        prometheus.read_text(encoding="utf-8").replace(
            "__SOORIN_OBSERVABILITY_ENVIRONMENT__", "production"
        )
    )
    assert rendered_prometheus["global"]["external_labels"]["environment"] == "production"
    assert (
        rendered_prometheus["scrape_configs"][0]["static_configs"][0]["labels"][
            "environment"
        ]
        == "production"
    )
    prometheus_service = services["prometheus"]
    assert prometheus_service["entrypoint"] == ["/bin/sh", "-ec"]
    assert "invalid SOORIN_OBSERVABILITY_ENVIRONMENT" in prometheus_service["command"][0]
    assert "--config.file=/tmp/prometheus.yml" in prometheus_service["command"][0]
    alloy_text = alloy.read_text(encoding="utf-8")
    assert 'regex         = "api|ui"' in alloy_text
    assert "stage.json" in alloy_text
    assert "stage.structured_metadata" in alloy_text
    labels_block = alloy_text.split("stage.labels", 1)[1].split("}", 2)[0]
    assert "request_id" not in labels_block
    assert "trace_id" not in labels_block
    assert json.loads(dashboard.read_text(encoding="utf-8"))["uid"] == "soorin-copilot-operations"


def test_dashboard_queries_are_range_correct_and_use_provisioned_datasources() -> None:
    dashboard = json.loads(
        (ROOT / "observability/grafana/dashboards/soorin-copilot-overview.json").read_text(
            encoding="utf-8"
        )
    )
    panels = [panel for panel in dashboard["panels"] if panel["type"] != "row"]
    assert len({panel["id"] for panel in dashboard["panels"]}) == len(dashboard["panels"])

    environment = next(
        item for item in dashboard["templating"]["list"] if item["name"] == "environment"
    )
    assert environment["query"]["query"] == (
        'label_values(up{job="soorin-copilot-api"}, environment)'
    )

    valid_uids = {"soorin-prometheus", "soorin-loki"}
    all_targets = [target for panel in panels for target in panel.get("targets", [])]
    assert all(
        (target.get("datasource") or panel["datasource"])["uid"] in valid_uids
        for panel in panels
        for target in panel.get("targets", [])
    )
    expressions = [target.get("expr", "") for target in all_targets]
    assert not any("[5m]" in expression for expression in expressions)
    assert not any(
        address in json.dumps(dashboard)
        for address in ("localhost", "127.0.0.1", ":9090", ":3100")
    )
    combined_expressions = "\n".join(expressions)
    for required_metric in (
        "soorin_stream_time_to_first_output_seconds_bucket",
        "soorin_product_requests_total",
        "soorin_memory_lifecycle_events_total",
        "soorin_memory_revision_conflicts_total",
        "soorin_memory_canonical_reload_total",
        "soorin_memory_vector_operations_total",
        "soorin_context_estimated_tokens_per_request_bucket",
        "soorin_context_compacted_tokens_per_request_bucket",
    ):
        assert required_metric in combined_expressions

    selected_range_panels = [
        panel for panel in panels if "selected range" in panel["title"].lower()
    ]
    assert selected_range_panels
    assert all(
        target.get("instant") is True
        for panel in selected_range_panels
        for target in panel.get("targets", [])
    )
    assert all(
        "$__range" in target.get("expr", "")
        for panel in selected_range_panels
        for target in panel.get("targets", [])
    )


def test_dashboard_token_panels_distinguish_rates_totals_and_averages() -> None:
    dashboard = json.loads(
        (ROOT / "observability/grafana/dashboards/soorin-copilot-overview.json").read_text(
            encoding="utf-8"
        )
    )
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    throughput = panels["LLM token throughput — tokens/min"]
    assert throughput["fieldConfig"]["defaults"]["unit"] == "suffix: tokens/min"
    assert all("rate(" in target["expr"] and "* 60" in target["expr"] for target in throughput["targets"])

    totals = panels["LLM tokens — selected range"]
    assert totals["fieldConfig"]["defaults"]["unit"] == "short"
    assert all(target.get("instant") is True for target in totals["targets"])
    assert all("increase(" in target["expr"] and "$__range" in target["expr"] for target in totals["targets"])

    per_call = panels["Average tokens / LLM call"]
    assert per_call["fieldConfig"]["defaults"]["unit"] == "suffix: tokens/call"
    per_request = panels["Average LLM tokens / completed Copilot request"]
    assert per_request["fieldConfig"]["defaults"]["unit"] == "suffix: tokens/request"
