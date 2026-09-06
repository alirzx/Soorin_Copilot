"""Focused Streamlit topology contracts for the API-only Neo4j cutover."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.web.pages import topology


def _settings() -> SimpleNamespace:
    return SimpleNamespace(api_base_url="http://api.example", api_timeout_seconds=15, copilot_api_key="key")


def _topology() -> dict[str, object]:
    return {
        "nodes": [{"ip": "10.0.0.1", "degree": 3}, {"ip": "10.0.0.2", "degree": 1}],
        "edges": [{"source": "10.0.0.1", "target": "10.0.0.2", "weight": 2}],
        "max_nodes": 100,
        "min_degree": 1,
        "subnet": "",
    }


def test_graph_status_and_topology_use_authenticated_api(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object] | None, dict[str, str]]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, *, headers, params, timeout):
        calls.append((url, params, headers))
        return Response(
            {"loaded": True, "active_graph_version": "published-v1", "nodes": 2, "edges": 1}
            if url.endswith("/graph/status")
            else _topology()
        )

    monkeypatch.setattr(topology.requests, "get", fake_get)
    monkeypatch.setattr(topology, "copilot_auth_headers", lambda _: {"X-Copilot-Key": "key"})

    assert topology.fetch_graph_status(_settings())["active_graph_version"] == "published-v1"
    assert topology._fetch_topology(_settings(), max_nodes=100, min_degree=1, subnet="")["nodes"]
    assert calls[0][0].endswith("/graph/status")
    assert calls[1][0].endswith("/graph/topology")
    assert calls[1][1] == {"max_nodes": 100, "min_degree": 1, "subnet": ""}
    assert calls[0][2] == {"X-Copilot-Key": "key"}


def test_published_version_change_preserves_or_clears_selection_only_via_api_node_check(monkeypatch) -> None:
    monkeypatch.setattr(topology, "_fetch_node", lambda _settings, _ip: {"found": True})
    assert topology._should_reload_graph_snapshot("v1", "v2") is True
    assert topology._clear_missing_selected_ip(_settings(), "10.0.0.1", version_changed=True) is False
    monkeypatch.setattr(topology, "_fetch_node", lambda _settings, _ip: {"found": False})
    assert topology._clear_missing_selected_ip(_settings(), "10.0.0.1", version_changed=True) is True


def test_selection_accepts_only_nodes_returned_by_bounded_topology() -> None:
    nodes = topology._topology_node_ids(_topology())
    assert topology._resolve_graph_selection_event({"action": "select", "node": "10.0.0.1", "event_id": "1"}, nodes) == ("select", "10.0.0.1", "1")
    assert topology._resolve_graph_selection_event({"action": "select", "node": "10.0.0.3", "event_id": "2"}, nodes) == ("none", None, "2")


def test_api_topology_html_has_no_local_graph_dependency() -> None:
    html = topology.build_topology_html(_topology())
    assert "10.0.0.1" in html
    assert "10.0.0.2" in html
    assert "soorin_graph_selection" in html
    assert topology._subnet_summaries(_topology()) == [{"subnet": "10.0.0.0/24", "count": 2}]


def test_graph_api_error_never_exposes_driver_details(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise topology.requests.ConnectionError("bolt://secret-host")

    monkeypatch.setattr(topology.requests, "get", fail)
    with pytest.raises(topology.GraphApiError, match="not reachable"):
        topology.fetch_graph_status(_settings())
