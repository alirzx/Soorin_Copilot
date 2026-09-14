"""Phase 4C runtime hardening discovered by live E2E acceptance tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.core.agent.contracts import InvestigationState
from src.core.agent.phase4c_nodes import Phase4CWorkflowNodes as BasePhase4CWorkflowNodes
from src.core.agent.structured_continuity import structured_query_context_from_state
from src.core.context.models import (
    EntityResolution,
    ResolvedEntity,
    StructuredResultReferenceDecision,
)
from src.core.memory.episodes import MemoryContextKey


_FOCAL_CAPABILITIES = {
    "asset.get_profile",
    "asset.get_detection",
    "graph.get_summary",
    "graph.compare_assets",
}
_FOCAL_CAPABILITY_ORDER = (
    "asset.get_profile",
    "asset.get_detection",
    "graph.get_summary",
    "graph.compare_assets",
)


def _deepened_entities(state: InvestigationState) -> tuple[str, ...]:
    focal: list[str] = []
    for result in state.get("tool_results") or ():
        if result.source_capability not in _FOCAL_CAPABILITIES:
            continue
        for entity in result.entities:
            if entity not in focal:
                focal.append(entity)
    return tuple(focal[:2])


class Phase4CWorkflowNodes(BasePhase4CWorkflowNodes):
    """Promote only actually deepened structured results to focal continuity."""

    def update_memory(self, state: InvestigationState) -> dict[str, Any]:
        focal = _deepened_entities(state)
        if not focal:
            return super().update_memory(state)

        resolved = [
            ResolvedEntity(type="ip", value=value, source="conversation")
            for value in focal
        ]
        resolution = EntityResolution(
            status="resolved",
            entities=resolved,
            primary_entity=resolved[0] if len(resolved) == 1 else None,
            entity_mode="single" if len(resolved) == 1 else "multiple",
            candidate_count=len(resolved),
            explicit_candidate_count=0,
            valid_entity_count=len(resolved),
            reference_detected=True,
            reference_type="phase4c_focal_deepening",
        )
        capabilities = {
            result.source_capability for result in state.get("tool_results") or ()
        }
        pair = len(focal) == 2
        route = replace(
            state["routing_result"],
            use_graph=bool({"graph.get_summary", "graph.compare_assets"} & capabilities),
            use_detection="asset.get_detection" in capabilities,
            use_asset_profile="asset.get_profile" in capabilities,
            entity_binding="active_pair" if pair else "active_single",
            resolved_entity_binding="active_pair" if pair else "active_single",
            binding_source="conversation",
            binding_available=True,
            binding_normalized=True,
            binding_normalization_reason="phase4c_focal_deepening_persisted",
            materialized_entity_count=len(focal),
            materialized_entities=focal,
            target_entity=resolved[0] if len(resolved) == 1 else None,
            target_entities=resolved,
            asset_investigation_detected=True,
            followup_detected=True,
            intent="asset_investigation",
            structured_query=None,
            structured_result_reference=StructuredResultReferenceDecision(),
            scope="multi_entity_comparison" if pair else "node_summary",
            direction=(
                "both"
                if bool({"graph.get_summary", "graph.compare_assets"} & capabilities)
                else "none"
            ),
            depth=1 if pair else 0,
            requires_multiple_entities=pair,
            relationship_mode="compare" if pair else "none",
            route_normalized=True,
            route_normalization_reason="phase4c_focal_deepening_persisted",
        )
        original_task = state["task"]
        required_capabilities = tuple(
            capability
            for capability in _FOCAL_CAPABILITY_ORDER
            if capability in capabilities
        )
        focal_task = replace(
            original_task,
            intent="asset_investigation",
            scope="multi_entity_comparison" if pair else "node_summary",
            direction=(
                "both"
                if bool({"graph.get_summary", "graph.compare_assets"} & capabilities)
                else "none"
            ),
            entities=focal,
            required_capabilities=required_capabilities,
            optional_capabilities=(),
            structured_query=None,
            requires_multiple_entities=pair,
            is_followup=True,
            graph_depth=1 if pair else 0,
            relationship_mode="compare" if pair else "none",
        )
        patched: InvestigationState = dict(state)  # type: ignore[assignment]
        patched["resolved_entities"] = resolution
        patched["routing_result"] = route
        patched["task"] = focal_task
        patched["memory_context_key"] = MemoryContextKey.from_task(focal_task)
        patched["baseline_results"] = [
            result
            for result in state.get("tool_results") or ()
            if result.source_capability in _FOCAL_CAPABILITIES
            and bool(set(result.entities).intersection(focal))
        ]
        patched["require_baseline_for_operational_mutation"] = True
        structured_context = structured_query_context_from_state(state)
        previous = state.get("active_entity_state")
        if structured_context is not None and previous is not None:
            reference_kind = getattr(
                getattr(state.get("routing_result"), "structured_result_reference", None),
                "kind",
                "none",
            )
            previous_lineage = tuple(
                getattr(previous, "structured_query_lineage", ()) or ()
            )
            lineage = (
                (*previous_lineage, structured_context)
                if reference_kind == "set_query"
                else (structured_context,)
            )
            if len(lineage) > 3:
                lineage = (lineage[0], lineage[-2], lineage[-1])
            patched["active_entity_state"] = replace(
                previous,
                structured_query_context=structured_context,
                structured_query_lineage=lineage,
            )
        return super().update_memory(patched)
