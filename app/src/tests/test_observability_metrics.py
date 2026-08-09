"""Offline tests for the optional Soorin observability foundation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families

from src.api.auth import verify_api_key
from src.api.main import create_app
from src.config.settings import get_settings
from src.core.observability.metrics import FORBIDDEN_LABEL_NAMES, SoorinMetrics


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
        "soorin_workflow_stage_duration_seconds",
        "soorin_llm_requests",
        "soorin_llm_input_tokens",
        "soorin_tool_calls",
        "soorin_memory_retrieval",
        "soorin_memory_sufficiency",
        "soorin_context_estimated_tokens",
        "soorin_context_token_savings",
        "soorin_product_view_selected",
    } <= names


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
    alloy_text = alloy.read_text(encoding="utf-8")
    assert 'regex         = "api|ui"' in alloy_text
    assert "request_id" not in alloy_text
    assert "trace_id" not in alloy_text
    assert json.loads(dashboard.read_text(encoding="utf-8"))["uid"] == "soorin-copilot-operations"
