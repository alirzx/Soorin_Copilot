"""Deterministic Evidence Reviewer V1."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.core.agent.contracts import EvidencePack, ExecutionPlan, ReviewDecision, TaskSpec, ToolResult
from src.core.agent.context_identity import identity_for_tool_result


_NO_DEDICATED_ANOMALY_EVIDENCE = (
    "No dedicated anomaly provider evidence is available; only bounded graph structural analysis is supplied."
)


class EvidenceReviewer:
    def review(
        self,
        task: TaskSpec,
        results: list[ToolResult],
        *,
        allow_supplemental: bool = False,
    ) -> ReviewDecision:
        required_cardinality = {
            "asset.get_profile": (1, 2),
            "asset.get_detection": (1, 2),
            "graph.get_summary": (1, 1),
            "graph.get_neighbors": (1, 1),
            "graph.get_relationship": (2, 2),
            "graph.compare_assets": (2, 2),
            "graph.find_path": (2, 2),
            "knowledge.search": (0, 2),
        }
        invalid_bindings = tuple(
            capability
            for capability in task.required_capabilities
            if capability in required_cardinality
            and not required_cardinality[capability][0]
            <= len(task.entities)
            <= required_cardinality[capability][1]
        )
        if invalid_bindings:
            material = ("Required capability entity binding was not available.",)
            return ReviewDecision(
                outcome="missing_required_evidence",
                reasons=material,
                missing_capabilities=invalid_bindings,
                material_limitations=material,
            )
        by_capability: dict[str, list[ToolResult]] = {}
        for result in results:
            by_capability.setdefault(result.source_capability, []).append(result)
        missing = tuple(
            capability
            for capability in task.required_capabilities
            if capability not in by_capability
        )
        if missing:
            next_capability = missing[0] if allow_supplemental else None
            material = ("One or more required capabilities were not executed.",)
            return ReviewDecision(
                outcome="missing_required_evidence",
                reasons=material,
                missing_capabilities=missing,
                supplemental_allowed=bool(next_capability),
                next_capability=next_capability,
                next_arguments=self._arguments(task, next_capability) if next_capability else None,
                material_limitations=material,
            )
        for capability in ("asset.get_profile", "asset.get_detection"):
            if capability not in task.required_capabilities:
                continue
            covered = {entity for result in by_capability[capability] for entity in result.entities}
            missing_entities = [entity for entity in task.entities if entity not in covered]
            if missing_entities:
                material = ("Required capability did not cover every resolved entity.",)
                return ReviewDecision(
                    outcome="missing_required_evidence",
                    reasons=material,
                    missing_capabilities=(capability,),
                    supplemental_allowed=allow_supplemental,
                    next_capability=capability if allow_supplemental else None,
                    next_arguments={"entities": [missing_entities[0]]} if allow_supplemental else None,
                    material_limitations=material,
                )
        required = [result for name in task.required_capabilities for result in by_capability[name]]
        if required and all(
            result.status in {"unavailable", "not_configured", "invalid"}
            for result in required
        ):
            material = ("All required evidence capabilities were unavailable or invalid.",)
            return ReviewDecision(
                outcome="safe_failure",
                reasons=material,
                material_limitations=material,
            )
        failed = tuple(
            result.source_capability
            for result in required
            if result.status in {"unavailable", "not_configured", "invalid"}
        )
        if failed:
            material = tuple(
                f"{capability} was unavailable for this request." for capability in failed
            )
            return ReviewDecision(
                outcome="answer_with_limitations",
                reasons=("Required evidence was unavailable.",),
                missing_capabilities=failed,
                limitations=material,
                material_limitations=material,
            )
        for capability in ("asset.get_profile", "asset.get_detection"):
            if capability not in task.required_capabilities:
                continue
            for entity in task.entities:
                entity_results = [
                    result
                    for result in by_capability[capability]
                    if entity in result.entities and result.status in {"ok", "partial"}
                ]
                if not entity_results:
                    continue
                usable = any(
                    result.context_included
                    and result.context_representation in {"unreviewed", "projected", "full_minified", "memory_reuse"}
                    and result.source_payload_complete
                    and result.projection_usable
                    and not result.projection_truncated
                    for result in entity_results
                )
                if usable:
                    continue
                limitation = (
                    f"{capability} retrieval succeeded for {entity}, but a usable validated projection "
                    "was not available in model context."
                )
                return ReviewDecision(
                    outcome="answer_with_limitations",
                    reasons=(limitation,),
                    missing_capabilities=(capability,),
                    limitations=(limitation,),
                    material_limitations=(limitation,),
                )
        caveats: list[str] = []
        material_limitations: list[str] = []
        dedicated_anomaly_evidence = any(
            result.source_capability == "asset.get_detection"
            and "evidence" in result.selected_views
            and result.status in {"ok", "partial"}
            and result.context_included
            and result.context_representation in {"projected", "full_minified"}
            and result.source_payload_complete
            and result.projection_usable
            and not result.projection_truncated
            for result in required
        )
        for result in required:
            if result.status in {"partial", "empty", "not_found"}:
                material_limitations.append(f"{result.source_capability} status is {result.status}.")
            if result.freshness in {"stale", "unknown"}:
                material_limitations.append(f"{result.source_capability} freshness is {result.freshness}.")
            if result.completeness != "complete" or result.truncated:
                material_limitations.append(f"{result.source_capability} evidence is incomplete or truncated.")
            if not result.context_included:
                material_limitations.append(f"{result.source_capability} evidence was not included in model context.")
            if (
                result.source_capability.startswith("graph.")
                and result.context_included
                and result.context_token_cap > 0
                and result.context_token_estimate > result.context_token_cap
            ):
                material_limitations.append(
                    f"{result.source_capability} model context exceeded its scope-specific token cap."
                )
            if result.source_capability.startswith("graph."):
                graph_context = getattr(result.provider_result, "context", None)
                graph_context = graph_context if isinstance(graph_context, dict) else {}
                retrieved_nodes = int(graph_context.get("retrieved_node_count") or 0)
                retrieved_edges = int(graph_context.get("retrieved_edge_count") or 0)
                included_nodes = int(graph_context.get("included_node_count") or 0)
                included_edges = int(graph_context.get("included_edge_count") or 0)
                omitted_peers = int(graph_context.get("model_context_omitted_peer_count") or 0)
                serialization_truncated = bool(
                    graph_context.get("serialized_context_truncated", False)
                )
                if included_nodes > retrieved_nodes or included_edges > retrieved_edges:
                    material_limitations.append(
                        f"{result.source_capability} reported inconsistent retrieval and model-context counts."
                    )
                if omitted_peers > 0 and not serialization_truncated:
                    material_limitations.append(
                        f"{result.source_capability} reported omitted peers without truncation metadata."
                    )
                if serialization_truncated:
                    target = (
                        caveats
                        if bool(graph_context.get("complete_for_user_request", False))
                        else material_limitations
                    )
                    target.append(
                        f"{result.source_capability} model context contains a bounded subset of retrieved graph evidence."
                    )
            if result.contradictions:
                material_limitations.append(f"{result.source_capability} reported contradictions.")
            caveats.extend(
                item
                for item in result.limitations
                if not (dedicated_anomaly_evidence and item == _NO_DEDICATED_ANOMALY_EVIDENCE)
            )
        material = tuple(dict.fromkeys(material_limitations))
        normal = tuple(dict.fromkeys(caveats))
        all_limitations = tuple(dict.fromkeys((*material, *normal)))
        if material:
            return ReviewDecision(
                outcome="answer_with_limitations",
                reasons=material,
                conflicts=tuple(dict.fromkeys(item for result in required for item in result.contradictions)),
                limitations=all_limitations,
                caveats=normal,
                material_limitations=material,
            )
        return ReviewDecision(outcome="sufficient", limitations=normal, caveats=normal)

    def build_pack(
        self,
        task: TaskSpec,
        results: list[ToolResult],
        *,
        plan: ExecutionPlan | None = None,
        request_id: str = "",
        trace_id: str = "",
        supplemental_history: tuple[dict[str, Any], ...] = (),
        review: ReviewDecision | None = None,
    ) -> EvidencePack:
        coverage = {result.source_capability: result.status for result in results}
        missing = tuple(
            capability for capability in task.required_capabilities if capability not in coverage
        )
        graph_results = [result for result in results if result.source_capability.startswith("graph.")]
        graph_completeness = (
            "not_requested"
            if not any(capability.startswith("graph.") for capability in task.required_capabilities)
            else "complete"
            if graph_results and all(result.completeness == "complete" and not result.truncated for result in graph_results)
            else "partial"
            if graph_results
            else "missing"
        )
        dedicated_anomaly_evidence = any(
            result.source_capability == "asset.get_detection"
            and "evidence" in result.selected_views
            and result.status in {"ok", "partial"}
            and result.source_payload_complete
            and result.projection_usable
            for result in results
        )
        return EvidencePack(
            task=task,
            tool_results=tuple(results),
            facts=tuple(fact for result in results for fact in result.facts),
            limitations=tuple(
                dict.fromkeys(
                    limitation
                    for result in results
                    for limitation in result.limitations
                    if not (
                        dedicated_anomaly_evidence
                        and limitation == _NO_DEDICATED_ANOMALY_EVIDENCE
                    )
                )
            ),
            contradictions=tuple(
                dict.fromkeys(item for result in results for item in result.contradictions)
            ),
            request_id=request_id,
            trace_id=trace_id,
            plan_id=plan.plan_id if plan else "",
            goal=(plan.goal if plan else "") or task.request,
            resolved_entities=task.entities,
            plan_summary=tuple(
                {
                    "step_id": step.id,
                    "capability": step.capability,
                    "depends_on": list(step.depends_on),
                    "requirement": step.requirement,
                }
                for step in (plan.steps if plan else ())
            ),
            provider_coverage=coverage,
            result_coverage={
                result.context_identity or identity_for_tool_result(result): result.status
                for result in results
            },
            graph_completeness=graph_completeness,
            rag_citations=tuple(citation for result in results for citation in result.citations),
            missing_evidence=missing,
            supplemental_history=supplemental_history,
            review_outcome=review.outcome if review else None,
            structured_asset_sets=tuple(
                result.structured_asset_set
                for result in results
                if result.structured_asset_set is not None
            ),
        )

    @staticmethod
    def with_review(pack: EvidencePack, review: ReviewDecision) -> EvidencePack:
        return replace(
            pack,
            review_outcome=review.outcome,
            missing_evidence=review.missing_capabilities,
            limitations=tuple(dict.fromkeys((*pack.limitations, *review.limitations, *review.reasons))),
            contradictions=tuple(dict.fromkeys((*pack.contradictions, *review.conflicts))),
        )

    @staticmethod
    def _arguments(task: TaskSpec, capability: str | None) -> dict[str, Any] | None:
        if not capability:
            return None
        if capability == "knowledge.search":
            return {"query": task.request}
        minimum_entities = 2 if capability in {"graph.get_relationship", "graph.compare_assets", "graph.find_path"} else 1
        return {"entities": list(task.entities[:minimum_entities])}
