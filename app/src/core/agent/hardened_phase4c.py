"""Phase 4C runtime hardening discovered by live E2E acceptance tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.core.agent.contracts import InvestigationState
from src.core.agent.phase4c_nodes import Phase4CWorkflowNodes as BasePhase4CWorkflowNodes
from src.core.context.models import EntityResolution, ResolvedEntity


_FOCAL_CAPABILITIES = {
    "asset.get_profile",
    "asset.get_detection",
    "graph.get_summary",
    "graph.compare_assets",
}


class Phase4CWorkflowNodes(BasePhase4CWorkflowNodes):
    """Promote only an actually deepened structured result to focal continuity."""

    def join_specialist_results(self, state: InvestigationState) -> dict[str, Any]:
        update = super().join_specialist_results(state)
        focal: list[str] = []
        for result in update.get("tool_results") or ():
            if result.source_capability not in _FOCAL_CAPABILITIES:
                continue
            for entity in result.entities:
                if entity not in focal:
                    focal.append(entity)
        if 1 <= len(focal) <= 2:
            update["phase4c_focal_entities"] = tuple(focal)
        return update

    def update_memory(self, state: InvestigationState) -> dict[str, Any]:
        focal = tuple(state.get("phase4c_focal_entities") or ())
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
            structured_query=state["routing_result"].structured_query,
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
            scope="multi_entity_comparison" if pair else "node_summary",
            direction="both" if bool({"graph.get_summary", "graph.compare_assets"} & capabilities) else "none",
            depth=1 if pair else 0,
            requires_multiple_entities=pair,
            relationship_mode="compare" if pair else "none",
            route_normalized=True,
            route_normalization_reason="phase4c_focal_deepening_persisted",
        )
        patched: InvestigationState = dict(state)  # type: ignore[assignment]
        patched["resolved_entities"] = resolution
        patched["routing_result"] = route
        return super().update_memory(patched)
