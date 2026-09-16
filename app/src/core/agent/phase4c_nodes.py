"""Phase 4C bounded cross-source deepening for structured Asset discovery.

The structured Asset search remains a zero-entity discovery operation. This
runtime extension may select at most two returned Assets *after* discovery and
then execute existing Product/Detection/Graph/Knowledge capabilities for those
focal Assets. Search rows never become conversational entities merely because
they were returned by Neo4j.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from src.core.agent.contracts import ExecutionPlan, InvestigationState, TaskSpec
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.agent.structured_continuity import structured_query_context_from_state
from src.core.agent.task_mapping import compile_direct_plan
from src.core.context.models import (
    EntityResolution,
    ResolvedEntity,
    StructuredResultReferenceDecision,
)
from src.core.memory.episodes import MemoryContextKey


logger = logging.getLogger(__name__)

_DEEP_ANALYSIS = re.compile(
    r"\b(?:analy[sz]e|investigate|assess|examine|review|inspect|deep(?:ly)?|comprehensive|"
    r"suspicious|anomal(?:y|ous)|risk|security|classification|detection|behavio(?:u)?r|profile)\b",
    re.IGNORECASE,
)
_COMPARE = re.compile(
    r"\b(?:compare|comparison|versus|vs\.?|difference|differences|both|first\s+two|top\s+two)\b",
    re.IGNORECASE,
)
_RANKED_ONE = re.compile(
    r"\b(?:first\s+(?:one|result|asset|match)|top\s+(?:one|result|asset|match)|leading\s+(?:result|asset|match)|"
    r"highest\b|lowest\b|most\b|least\b)\b",
    re.IGNORECASE,
)
_RANKED_TWO = re.compile(
    r"\b(?:first\s+two|top\s+two|highest\s+two|lowest\s+two|two\s+highest|two\s+lowest)\b",
    re.IGNORECASE,
)
_TOPOLOGY = re.compile(
    r"\b(?:graph|topology|network|connection|connections|peer|peers|relationship|relationships|"
    r"communication|communications|reach|degree|inbound|outbound|behavio(?:u)?r|anomal(?:y|ous)|suspicious)\b",
    re.IGNORECASE,
)
_DETECTION = re.compile(
    r"\b(?:detection|classification|classifier|confidence|rule|rules|signal|signals|role|"
    r"anomal(?:y|ous)|suspicious|risk|security|compromise|malicious)\b",
    re.IGNORECASE,
)
_KNOWLEDGE = re.compile(
    r"\b(?:mitre|att&ck|nist|hardening|guidance|procedure|runbook|playbook|response|remediation|"
    r"containment|recommendation|recommendations|best\s+practice)\b",
    re.IGNORECASE,
)
_BROAD = re.compile(
    r"\b(?:analy[sz]e|investigate|assess|comprehensive|deep(?:ly)?)\b",
    re.IGNORECASE,
)
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


def _phase4c_deepening_results(state: InvestigationState) -> tuple[Any, ...]:
    """Return usable focal evidence produced by the bounded Phase 4C fan-out."""
    return tuple(
        result
        for result in state.get("tool_results") or ()
        if str(getattr(result, "step_id", "") or "").startswith("deepening-")
        and result.source_capability in _FOCAL_CAPABILITIES
        and result.status in {"ok", "partial"}
        and result.projection_usable
    )


def _deepened_entities(state: InvestigationState) -> tuple[str, ...]:
    """Derive at most two focals; ordinary search/Product rows never qualify."""
    focal: list[str] = []
    for result in _phase4c_deepening_results(state):
        for entity in result.entities:
            if entity not in focal:
                focal.append(entity)
    return tuple(focal[:2])


class Phase4CWorkflowNodes(CopilotWorkflowNodes):
    """Keep the established workflow and add one deterministic post-search stage."""

    def join_specialist_results(self, state: InvestigationState) -> dict[str, Any]:
        base = super().join_specialist_results(state)
        results = list(base.get("tool_results") or ())
        task = state.get("task")
        if task is None or task.intent != "asset_search" or task.structured_query is None:
            return base
        if not _DEEP_ANALYSIS.search(task.request):
            return base

        search_result = next(
            (
                item
                for item in results
                if item.source_capability == "graph.search_assets"
                and item.structured_asset_set is not None
            ),
            None,
        )
        if search_result is None or search_result.status not in {
            "ok",
            "partial",
            "empty",
            "not_found",
        }:
            logger.info(
                "event=phase4c_candidate_selection request_id=%s candidates_found=0 candidates_selected=0 "
                "reason=search_evidence_unavailable",
                state.get("request_id", ""),
            )
            return base

        evidence = search_result.structured_asset_set
        rows = tuple(getattr(evidence, "rows", ()) or ())
        selected, reason = self._select_candidates(
            task,
            rows,
            retrieval_truncated=bool(getattr(evidence, "truncated", False)),
            matched_total=int(getattr(evidence, "matched_total", len(rows)) or 0),
        )
        logger.info(
            "event=phase4c_candidate_selection request_id=%s candidates_found=%s candidates_selected=%s "
            "selection_reason=%s retrieval_truncated=%s",
            state.get("request_id", ""),
            len(rows),
            len(selected),
            reason,
            bool(getattr(evidence, "truncated", False)),
        )
        if not selected:
            return base

        try:
            focal_task, focal_route = self._focal_task_and_route(state, task, selected)
            plan = self._compile_deepening_plan(focal_task, state["execution_plan"])
            _registry, validator, executor = self.service._capability_runtime_snapshot()
            validated = validator.validate(plan)
            deepening_results = executor.execute(
                validated,
                base_payload={
                    "request_id": state["request_id"],
                    "session_id": state["session_id"],
                    "route": focal_route,
                },
            )
        except Exception as exc:
            logger.exception(
                "event=phase4c_deepening_failed request_id=%s candidates_selected=%s error_type=%s",
                state.get("request_id", ""),
                len(selected),
                type(exc).__name__,
            )
            return base

        product_calls = sum(
            item.source_capability in {"asset.get_profile", "asset.get_detection"}
            for item in deepening_results
        )
        topology_calls = sum(
            item.source_capability.startswith("graph.")
            and item.source_capability != "graph.search_assets"
            for item in deepening_results
        )
        knowledge_calls = sum(
            item.source_capability == "knowledge.search" for item in deepening_results
        )
        partial_failures = sum(
            item.status
            in {"partial", "unavailable", "not_configured", "invalid", "not_found"}
            for item in deepening_results
        )
        logger.info(
            "event=phase4c_deepening_completed request_id=%s candidates_found=%s candidates_selected=%s "
            "product_calls=%s topology_calls=%s knowledge_calls=%s partial_failures=%s",
            state.get("request_id", ""),
            len(rows),
            len(selected),
            product_calls,
            topology_calls,
            knowledge_calls,
            partial_failures,
        )

        # Parent search evidence remains first-class discovery evidence. Focal
        # results are appended as independent ToolResults, so EvidencePack,
        # ContextComposer and Synth preserve native provider provenance.
        merged = [*results, *deepening_results]
        return {
            **base,
            "tool_results": merged,
            "capability_results": merged,
        }

    def review_retrieval(self, state: InvestigationState) -> dict[str, Any]:
        """Make focal verification failures visible without weakening search truth."""
        update = super().review_retrieval(state)
        task = state.get("task")
        if task is None or task.intent != "asset_search":
            return update
        deepening = tuple(
            item
            for item in state.get("tool_results") or ()
            if item.step_id.startswith("deepening-")
        )
        if not deepening:
            return update

        decision = update["review_decision"]
        if decision.outcome in {"safe_failure", "missing_required_evidence"}:
            return update

        material: list[str] = []
        caveats: list[str] = []
        for result in deepening:
            label = result.source_capability
            operational = label != "knowledge.search"
            if result.status in {"unavailable", "not_configured", "invalid", "not_found"}:
                target = material if operational else caveats
                target.append(f"{label} could not verify the selected focal Asset evidence.")
            elif result.status == "partial" or result.completeness != "complete" or result.truncated:
                target = material if operational else caveats
                target.append(f"{label} supplied incomplete evidence for the selected focal Asset.")
            if operational and label in {"asset.get_profile", "asset.get_detection"} and (
                not result.source_payload_complete or not result.projection_usable
            ):
                material.append(f"{label} did not supply a usable Product projection for the selected focal Asset.")
            caveats.extend(result.limitations)

        material_tuple = tuple(dict.fromkeys(material))
        caveat_tuple = tuple(dict.fromkeys(caveats))
        if not material_tuple and not caveat_tuple:
            return update
        revised = replace(
            decision,
            outcome="answer_with_limitations" if material_tuple else decision.outcome,
            reasons=tuple(dict.fromkeys((*decision.reasons, *material_tuple))),
            limitations=tuple(dict.fromkeys((*decision.limitations, *material_tuple, *caveat_tuple))),
            caveats=tuple(dict.fromkeys((*decision.caveats, *caveat_tuple))),
            material_limitations=tuple(
                dict.fromkeys((*decision.material_limitations, *material_tuple))
            ),
            supplemental_allowed=False,
            next_capability=None,
            next_arguments=None,
        )
        pack = self.service.evidence_reviewer.with_review(state["evidence_pack"], revised)
        logger.info(
            "event=phase4c_evidence_review request_id=%s focal_result_count=%s "
            "material_limitation_count=%s caveat_count=%s outcome=%s",
            state.get("request_id", ""),
            len(deepening),
            len(material_tuple),
            len(caveat_tuple),
            revised.outcome,
        )
        return {**update, "review_decision": revised, "evidence_pack": pack, "next_edge": "compose"}

    def update_memory(self, state: InvestigationState) -> dict[str, Any]:
        """Persist only successfully deepened Assets as operational focals."""
        deepening_results = _phase4c_deepening_results(state)
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
        capabilities = {result.source_capability for result in deepening_results}
        pair = len(focal) == 2
        graph_used = bool({"graph.get_summary", "graph.compare_assets"} & capabilities)
        route = replace(
            state["routing_result"],
            use_graph=graph_used,
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
            direction="both" if graph_used else "none",
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
            direction="both" if graph_used else "none",
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
            for result in deepening_results
            if bool(set(result.entities).intersection(focal))
        ]
        patched["require_baseline_for_operational_mutation"] = True

        # The focal task intentionally drops its structured query. Preserve the
        # separately-reviewed discovery set on the prior routing state before
        # delegating to the ordinary memory transition.
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

    @staticmethod
    def _baseline_capture_rejection_reason(
        task: Any,
        results: tuple[Any, ...],
        identity: Any,
        review: Any,
    ) -> str:
        # The focal Assets selected during Phase 4C are execution-local. Do not
        # attach their current evidence to the zero-entity structured-search
        # episode baseline. StructuredQueryContext remains the continuity source.
        if getattr(task, "intent", "") in {"asset_search", "asset_aggregate"}:
            return "structured_asset_set_not_focal_baseline"
        return CopilotWorkflowNodes._baseline_capture_rejection_reason(
            task,
            results,
            identity,
            review,
        )

    @staticmethod
    def _select_candidates(
        task: TaskSpec,
        rows: tuple[dict[str, Any], ...],
        *,
        retrieval_truncated: bool = True,
        matched_total: int | None = None,
    ) -> tuple[tuple[str, ...], str]:
        if not rows:
            return (), "zero_candidates"
        ips = tuple(
            dict.fromkeys(
                str(row.get("ip") or "").strip()
                for row in rows
                if str(row.get("ip") or "").strip()
            )
        )
        if not ips:
            return (), "rows_without_valid_ip"
        if len(ips) == 1:
            return (ips[0],), "single_candidate"

        request = task.request
        comparison = bool(_COMPARE.search(request))
        if comparison:
            if len(ips) == 2:
                return ips[:2], "exact_two_candidates_for_comparison"
            if (
                _RANKED_TWO.search(request)
                and task.structured_query
                and task.structured_query.sort is not None
            ):
                return ips[:2], "explicit_ranked_pair"
            return (), "ambiguous_multi_candidate_comparison"

        if _RANKED_ONE.search(request):
            # "first" refers to deterministic returned order. Other rank wording
            # requires the Router to have encoded an explicit sort.
            first_word = bool(re.search(r"\bfirst\b", request, re.IGNORECASE))
            if first_word:
                return (ips[0],), "explicit_ranked_single"
            query = task.structured_query
            if query is not None and query.sort is not None:
                sort_field = query.sort.value
                ranked_rows = tuple(
                    row for row in rows
                    if str(row.get("ip") or "").strip() in ips
                )
                if not ranked_rows or ranked_rows[0].get(sort_field) is None:
                    return (), "ranked_value_unavailable"
                top_value = ranked_rows[0].get(sort_field)
                tied = tuple(
                    str(row.get("ip") or "").strip()
                    for row in ranked_rows
                    if row.get(sort_field) == top_value
                )
                if len(tied) == 1:
                    return (tied[0],), "unique_ranked_single"
                complete_tie_boundary = (
                    len(tied) < len(ranked_rows)
                    or (matched_total is not None and matched_total == len(ranked_rows))
                    or not retrieval_truncated
                )
                if len(tied) == 2 and complete_tie_boundary:
                    return tied, "exact_top_two_tie"
                return (), "ranked_top_tie_requires_secondary_criterion"
            return (), "rank_without_structured_sort"

        return (), "ambiguous_multi_candidate_selection"

    def _focal_task_and_route(
        self,
        state: InvestigationState,
        parent: TaskSpec,
        selected: tuple[str, ...],
    ) -> tuple[TaskSpec, Any]:
        request = parent.request
        pair = len(selected) == 2
        broad = bool(_BROAD.search(request))
        use_detection = broad or bool(_DETECTION.search(request))
        use_topology = broad or bool(_TOPOLOGY.search(request)) or pair
        use_knowledge = bool(_KNOWLEDGE.search(request))

        capabilities: list[str] = ["asset.get_profile"]
        if use_detection:
            capabilities.append("asset.get_detection")
        if use_topology:
            capabilities.append("graph.compare_assets" if pair else "graph.get_summary")
        optional: tuple[str, ...] = ("knowledge.search",) if use_knowledge else ()

        focal_task = TaskSpec(
            request=request,
            intent="asset_investigation",
            scope="multi_entity_comparison" if pair else "node_summary",
            direction="both",
            entities=selected,
            required_capabilities=tuple(capabilities),
            optional_capabilities=optional,
            structured_query=None,
            workflow_mode="direct",
            semantic_decision_source="deterministic_fallback",
            requires_multiple_entities=pair,
            recommended_steps=min(6, len(capabilities) + len(optional)),
            detail_level=parent.detail_level,
            freshness_requirement=parent.freshness_requirement,
            is_followup=False,
            graph_depth=1 if pair else 0,
            relationship_mode="compare" if pair else "none",
            temporal_mode=parent.temporal_mode,
            evidence_mode=parent.evidence_mode,
            response_depth=parent.response_depth,
        )

        resolved = [
            ResolvedEntity(type="ip", value=ip, source="conversation")
            for ip in selected
        ]
        route = replace(
            state["routing_result"],
            use_graph=use_topology,
            use_detection=use_detection,
            use_asset_profile=True,
            use_knowledge=use_knowledge,
            structured_query=None,
            entity_binding="active_pair" if pair else "active_single",
            requested_entity_binding="none",
            resolved_entity_binding="active_pair" if pair else "active_single",
            binding_source="conversation",
            binding_available=True,
            binding_normalized=True,
            binding_normalization_reason="phase4c_deterministic_candidate_selection",
            materialized_entity_count=len(selected),
            materialized_entities=selected,
            target_entity=resolved[0] if len(resolved) == 1 else None,
            target_entities=resolved,
            matched_signals=list(
                dict.fromkeys(
                    (*((state["routing_result"].matched_signals or [])), "phase4c_focal_deepening")
                )
            ),
            graph_intent_detected=use_topology,
            asset_investigation_detected=True,
            followup_detected=False,
            intent="asset_investigation",
            scope="multi_entity_comparison" if pair else "node_summary",
            direction="both",
            depth=1 if pair else 0,
            requires_multiple_entities=pair,
            relationship_mode="compare" if pair else "none",
            route_normalized=True,
            route_normalization_reason="phase4c_deterministic_candidate_selection",
        )
        return focal_task, route

    @staticmethod
    def _compile_deepening_plan(
        task: TaskSpec,
        parent_plan: ExecutionPlan,
    ) -> ExecutionPlan:
        compiled = compile_direct_plan(
            task,
            plan_id=f"{parent_plan.plan_id}-deep"[:32],
        )
        remaining_calls = max(
            0,
            min(parent_plan.maximum_allowed_calls, 6) - len(parent_plan.steps),
        )
        steps = list(compiled.steps)
        if len(steps) > remaining_calls:
            optional_ids = {
                step.id for step in steps if step.requirement == "optional"
            }
            if optional_ids:
                steps = [step for step in steps if step.id not in optional_ids]
                task = replace(task, optional_capabilities=())
                compiled = replace(compiled, task=task)
        if len(steps) > remaining_calls:
            raise ValueError("phase4c_deepening_call_budget_exceeded")
        renamed = tuple(
            replace(step, id=f"deepening-{index}")
            for index, step in enumerate(steps, start=1)
        )
        return replace(
            compiled,
            steps=renamed,
            maximum_allowed_calls=remaining_calls,
            source="deterministic",
        )
