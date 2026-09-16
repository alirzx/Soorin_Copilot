"""Deterministic Evidence Reviewer V1."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.core.agent.contracts import EvidencePack, ExecutionPlan, ReviewDecision, TaskSpec, ToolResult
from src.core.agent.context_identity import identity_for_tool_result
from src.core.agent.structured_evidence import (
    expected_semantic_query_identity,
    expected_structured_query_identity,
)


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
            "graph.search_assets": (0, 0),
            "graph.aggregate_assets": (0, 0),
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
        structured_failure = self._review_structured_results(task, required)
        if structured_failure is not None:
            capability, reason = structured_failure
            return ReviewDecision(
                outcome="missing_required_evidence",
                reasons=(reason,),
                missing_capabilities=(capability,),
                supplemental_allowed=allow_supplemental,
                next_capability=capability if allow_supplemental else None,
                next_arguments=self._arguments(task, capability) if allow_supplemental else None,
                material_limitations=(reason,),
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
            structured = result.structured_asset_set is not None
            if result.status in {"partial", "empty", "not_found"} and not (
                structured and result.status in {"empty", "not_found"}
            ):
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
        if capability in {"graph.search_assets", "graph.aggregate_assets"}:
            query = task.structured_query
            if query is None:
                return None
            request = (
                query.to_search_request()
                if capability == "graph.search_assets"
                else query.to_aggregate_request()
            )
            arguments = request.model_dump(
                mode="json",
                exclude={"cursor"},
                exclude_none=True,
            )
            arguments["semantic_query_id"] = expected_semantic_query_identity(query)
            if not arguments.get("group_by_fields"):
                arguments.pop("group_by_fields", None)
            return arguments
        minimum_entities = 2 if capability in {"graph.get_relationship", "graph.compare_assets", "graph.find_path"} else 1
        return {"entities": list(task.entities[:minimum_entities])}

    @staticmethod
    def _review_structured_results(
        task: TaskSpec,
        required: list[ToolResult],
    ) -> tuple[str, str] | None:
        structured_capabilities = {"graph.search_assets", "graph.aggregate_assets"}
        for result in required:
            capability = result.source_capability
            if capability not in structured_capabilities:
                continue
            evidence = result.structured_asset_set
            query = task.structured_query
            if evidence is None or query is None:
                return capability, f"{capability} did not provide typed structured evidence."
            expected_mode = "search" if capability == "graph.search_assets" else "aggregate"
            if evidence.capability != capability or evidence.mode != expected_mode or query.mode.value != expected_mode:
                return capability, f"{capability} evidence mode did not match the validated task."
            expected_identity = expected_structured_query_identity(query, evidence)
            expected_semantic_identity = expected_semantic_query_identity(query)
            provider_context = getattr(result.provider_result, "context", {})
            if (
                evidence.query_identity != expected_identity
                or result.normalized_query_hash != expected_identity
                or result.context_identity != expected_identity
                or evidence.semantic_query_id != expected_semantic_identity
                or result.semantic_query_id not in {"", expected_semantic_identity}
                or (
                    isinstance(provider_context, dict)
                    and provider_context.get("semantic_query_id") is not None
                    and provider_context.get("semantic_query_id") != expected_semantic_identity
                )
            ):
                return capability, f"{capability} evidence query identity did not match the validated task."
            if not evidence.active_graph_version:
                return capability, f"{capability} did not identify an active graph projection version."
            if evidence.provenance != "neo4j_active_organizational_projection":
                return capability, f"{capability} evidence provenance was not the active organizational projection."
            if result.entities:
                return capability, f"{capability} incorrectly attached focal entities to an Asset-set receipt."
            if result.total_count is None or result.included_count is None or result.omitted_count is None:
                return capability, f"{capability} did not preserve generic coverage counts."
            if result.total_count < 0 or result.included_count < 0 or result.omitted_count < 0:
                return capability, f"{capability} reported a negative coverage count."

            if expected_mode == "search":
                returned = evidence.returned_count
                matched = evidence.matched_total
                if returned != len(evidence.rows) or matched < returned:
                    return capability, f"{capability} reported inconsistent matched and returned counts."
                if evidence.truncated != (matched > returned):
                    return capability, f"{capability} reported inconsistent truncation semantics."
                if (
                    result.total_count != matched
                    or result.included_count != returned
                    or result.omitted_count != matched - returned
                    or result.truncated != evidence.truncated
                ):
                    return capability, f"{capability} generic and typed coverage metadata disagreed."
                continue

            if evidence.count < 0 or any(int(group.get("count", -1)) < 0 for group in evidence.groups):
                return capability, f"{capability} reported a negative aggregate count."
            if query.operation is None or evidence.operation != query.operation.value:
                return capability, f"{capability} aggregate operation did not match the validated task."
            expected_groups = tuple(
                item.value for item in (
                    query.group_by_fields
                    or ((query.group_by,) if query.group_by is not None else ())
                )
            )
            evidence_groups = evidence.group_by_fields or (
                (evidence.group_by,) if evidence.group_by is not None else ()
            )
            if evidence_groups != expected_groups:
                return capability, f"{capability} aggregate grouping did not match the validated task."
            expected_group = expected_groups[0] if len(expected_groups) == 1 else None
            if evidence.group_by != expected_group:
                return capability, f"{capability} legacy aggregate grouping did not match the validated task."
            group_total = sum(int(group.get("count") or 0) for group in evidence.groups)
            if evidence.operation == "count" and (evidence.groups or evidence.group_by_fields):
                return capability, f"{capability} count evidence unexpectedly contained grouped output."
            if evidence.operation == "group_count" and (
                not evidence_groups
                or group_total > evidence.count
                or (not evidence.truncated and group_total != evidence.count)
            ):
                return capability, f"{capability} reported inconsistent grouped aggregate counts."
            expected_included = group_total if evidence_groups else evidence.count
            if result.total_count != evidence.count or result.included_count != expected_included:
                return capability, f"{capability} generic and typed aggregate metadata disagreed."
            if result.omitted_count != max(0, result.total_count - result.included_count):
                return capability, f"{capability} reported an inconsistent omitted aggregate count."
            if result.truncated != evidence.truncated:
                return capability, f"{capability} generic and typed truncation metadata disagreed."
        return None
