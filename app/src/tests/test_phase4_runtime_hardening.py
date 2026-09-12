from __future__ import annotations

from types import MethodType

from src.core.agent.contracts import (
    StructuredAssetSearchEvidence,
    TaskSpec,
    ToolResult,
)
from src.core.agent.hardened_phase4c import Phase4CWorkflowNodes, _deepened_entities
from src.core.agent.hardened_reviewer import EvidenceReviewer
from src.core.agent.phase4c_nodes import Phase4CWorkflowNodes as BasePhase4CWorkflowNodes
from src.core.context.models import RouteDecision
from src.core.copilot.hardened_service import CopilotService
from src.core.copilot.service import CopilotService as BaseCopilotService
from src.core.graph.structured import (
    AssetSearchFilters,
    AssetSortField,
    SortDirection,
    StructuredQueryMode,
    StructuredQuerySpec,
    structured_query_identity,
)
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent


def _ranked_query() -> StructuredQuerySpec:
    return StructuredQuerySpec(
        mode=StructuredQueryMode.SEARCH,
        filters=AssetSearchFilters(role="firewall"),
        sort=AssetSortField.MODEL_CONFIDENCE,
        direction=SortDirection.DESC,
        limit=2,
    )


def test_ranked_two_row_tie_check_is_not_materially_incomplete():
    query = _ranked_query()
    query_identity = structured_query_identity(query, active_graph_version="v1")
    task = TaskSpec(
        request="Find the Firewall with the highest model confidence.",
        intent="asset_search",
        scope="none",
        direction="none",
        entities=(),
        required_capabilities=("graph.search_assets",),
        structured_query=query,
    )
    evidence = StructuredAssetSearchEvidence(
        capability="graph.search_assets",
        query_identity=query_identity,
        normalized_filters={"role": "firewall"},
        active_graph_version="v1",
        sort="model_confidence",
        direction="desc",
        matched_total=9,
        returned_count=2,
        truncated=True,
        rows=(
            {"ip": "192.168.8.1", "model_confidence": 0.96},
            {"ip": "192.168.20.1", "model_confidence": 0.95},
        ),
        retrieved_at="now",
    )
    result = ToolResult(
        status="ok",
        entities=(),
        source_capability="graph.search_assets",
        retrieved_at="now",
        freshness="current",
        completeness="partial",
        total_count=9,
        included_count=2,
        omitted_count=7,
        truncated=True,
        normalized_query_hash=query_identity,
        context_identity=query_identity,
        structured_asset_set=evidence,
    )
    decision = EvidenceReviewer().review(task, [result])
    assert decision.outcome == "sufficient"
    assert not decision.material_limitations


def test_phase4c_derives_only_actually_deepened_assets_as_focal():
    focal_result = ToolResult(
        status="ok",
        entities=("192.168.8.1",),
        source_capability="asset.get_profile",
        retrieved_at="now",
        freshness="current",
        completeness="complete",
    )
    search_result = ToolResult(
        status="ok",
        entities=(),
        source_capability="graph.search_assets",
        retrieved_at="now",
        freshness="current",
        completeness="complete",
    )

    state = {"tool_results": [search_result, focal_result]}
    assert _deepened_entities(state) == ("192.168.8.1",)


def test_phase4c_deepened_asset_becomes_active_in_memory_transition(monkeypatch):
    profile = ToolResult(
        status="ok",
        entities=("192.168.8.1",),
        source_capability="asset.get_profile",
        retrieved_at="now",
        freshness="current",
        completeness="complete",
    )

    def capture(_self, state):
        return {
            "entities": state["resolved_entities"],
            "route": state["routing_result"],
        }

    monkeypatch.setattr(BasePhase4CWorkflowNodes, "update_memory", capture)
    node = object.__new__(Phase4CWorkflowNodes)

    state = {
        "tool_results": [profile],
        "routing_result": RouteDecision(
            use_graph=True,
            reason="ranked search",
            intent="asset_search",
            structured_query=_ranked_query(),
        ),
    }

    update = node.update_memory(state)

    assert update["entities"].primary_entity.value == "192.168.8.1"
    assert update["route"].intent == "asset_investigation"
    assert update["route"].materialized_entities == ("192.168.8.1",)


def test_length_truncated_stream_is_not_emitted_before_recovery(monkeypatch):
    service = object.__new__(CopilotService)

    def base_stream(
        _self,
        messages,
        *,
        request_id,
        max_tokens,
        temperature,
        top_p,
        timeout_seconds,
        sink,
        metrics,
        trace_id="",
    ):
        sink(LLMStreamEvent("answer_delta", text="partial sentence"))
        return LLMProviderResult(
            text="partial sentence",
            provider="test",
            model="test",
            finish_reason="length",
        )

    monkeypatch.setattr(BaseCopilotService, "_stream_final_model", base_stream)

    def recovery(
        _self,
        messages,
        *,
        request_id,
        max_tokens,
        temperature,
        top_p,
        timeout_seconds,
        sink,
        metrics,
        reason,
        trace_id="",
    ):
        sink(LLMStreamEvent("answer_delta", text="complete recovered answer"))
        return LLMProviderResult(
            text="complete recovered answer",
            provider="test",
            model="test",
            finish_reason="stop",
        )

    service._synthesis_non_stream_fallback = MethodType(recovery, service)
    public_events = []
    result = CopilotService._stream_final_model(
        service,
        [{"role": "user", "content": "test"}],
        request_id="r1",
        max_tokens=4096,
        temperature=None,
        top_p=None,
        timeout_seconds=30,
        sink=public_events.append,
        metrics={},
    )
    assert result.text == "complete recovered answer"
    assert [event.text for event in public_events if event.type == "answer_delta"] == [
        "complete recovered answer"
    ]
