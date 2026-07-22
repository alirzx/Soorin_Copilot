"""Asset Profile and Detection specialist subgraph."""

from __future__ import annotations

from src.core.agent.contracts import AssetInvestigationResult
from src.core.agent.specialists.base import BoundedSpecialistSubgraph, SpecialistState


class AssetInvestigationSpecialist(BoundedSpecialistSubgraph):
    name = "asset_investigation"
    capability_prefixes = ("asset.",)

    def normalize_result(self, state: SpecialistState) -> AssetInvestigationResult:
        plan = state["execution_plan"]
        results = tuple(state.get("tool_results") or ())
        capabilities = tuple(dict.fromkeys(item.source_capability for item in results))
        missing = tuple(
            dict.fromkeys(
                [
                    step.capability
                    for step in state.get("selected_steps") or ()
                    if not any(result.step_id == step.id for result in results)
                ]
                + [
                    item.source_capability
                    for item in results
                    if item.status in {"unavailable", "not_configured", "invalid"}
                ]
            )
        )
        return AssetInvestigationResult(
            entities=plan.task.entities,
            executed_capabilities=capabilities,
            result_statuses=tuple((item.source_capability, item.status) for item in results),
            identity_role_fact_count=sum("identity_role" in item.selected_views for item in results),
            service_software_fact_count=sum(
                bool({"services", "software"}.intersection(item.selected_views)) for item in results
            ),
            risk_behavior_fact_count=sum(
                bool({"anomaly_risk", "behavior"}.intersection(item.selected_views)) for item in results
            ),
            conflicts=tuple(dict.fromkeys(value for item in results for value in item.contradictions)),
            missing_evidence=missing,
            freshness=tuple(dict.fromkeys(item.freshness for item in results)),
            truncated=any(item.truncated or item.projection_truncated for item in results),
            status=state.get("status", "skipped"),
        )
