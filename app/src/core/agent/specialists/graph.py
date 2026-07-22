"""NetworkX graph evidence specialist subgraph."""

from __future__ import annotations

from src.core.agent.contracts import GraphAnalysisResult
from src.core.agent.specialists.base import BoundedSpecialistSubgraph, SpecialistState


class GraphAnalysisSpecialist(BoundedSpecialistSubgraph):
    name = "graph_analysis"
    capability_prefixes = ("graph.",)

    def normalize_result(self, state: SpecialistState) -> GraphAnalysisResult:
        plan = state["execution_plan"]
        results = tuple(state.get("tool_results") or ())
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
        contexts = [
            item.provider_result.context
            for item in results
            if isinstance(getattr(item.provider_result, "context", None), dict)
        ]
        candidate_count = sum(int(item.get("candidate_node_count") or 0) for item in contexts)
        retrieved_count = sum(int(item.get("retrieved_node_count") or 0) for item in contexts)
        complete = bool(results) and all(item.completeness == "complete" and not item.truncated for item in results)
        direct_relationship = None
        for context in contexts:
            if isinstance(context.get("forward_edge"), bool) or isinstance(context.get("reverse_edge"), bool):
                direct_relationship = bool(context.get("forward_edge") or context.get("reverse_edge"))
                break
            relationship = context.get("direct_relationship")
            if isinstance(relationship, dict) and (
                isinstance(relationship.get("a_to_b"), bool)
                or isinstance(relationship.get("b_to_a"), bool)
            ):
                direct_relationship = bool(relationship.get("a_to_b") or relationship.get("b_to_a"))
                break
        return GraphAnalysisResult(
            entities=plan.task.entities,
            executed_capabilities=tuple(dict.fromkeys(item.source_capability for item in results)),
            result_statuses=tuple((item.source_capability, item.status) for item in results),
            scope=plan.task.scope,
            direction=plan.task.direction,
            depth=plan.task.graph_depth,
            candidate_count=candidate_count,
            retrieved_count=retrieved_count,
            complete_for_request=complete,
            direct_relationship=direct_relationship,
            missing_evidence=missing,
            truncated=any(item.truncated for item in results),
            status=state.get("status", "skipped"),
        )
