"""Deterministic security boundary for Investigator-proposed actions."""

from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Any
from uuid import uuid4

from src.core.agent.adaptive_evidence import equivalent_evidence_for_step
from src.core.agent.context_identity import canonical_action_fingerprint
from src.core.agent.contracts import (
    AgentContinueDecision,
    AgentLoopBudget,
    EvidenceLedger,
    ExecutionPlan,
    PlanStep,
    RequestConstraints,
    TaskSpec,
)
from src.core.agent.plan_validator import PlanValidationError, PlanValidator
from src.core.agent.registry import CapabilityRegistry
from src.core.agent.task_mapping import compile_direct_plan


class AgentActionValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        evidence_reference_id: str = "",
        evidence_gap_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.evidence_reference_id = evidence_reference_id
        self.evidence_gap_id = evidence_gap_id


@dataclass(frozen=True)
class ValidatedAgentAction:
    plan: ExecutionPlan
    fingerprints: tuple[str, ...]
    deepened_entities: tuple[str, ...]


class AgentActionValidator:
    """Authorize, compile, and revalidate every adaptive capability request."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        plan_validator: PlanValidator,
    ) -> None:
        self.registry = registry
        self.plan_validator = plan_validator

    def validate(
        self,
        decision: AgentContinueDecision,
        *,
        task: TaskSpec,
        constraints: RequestConstraints,
        ledger: EvidenceLedger,
        budget: AgentLoopBudget,
        turn: int,
    ) -> ValidatedAgentAction:
        if time.monotonic() >= budget.deadline_monotonic:
            raise AgentActionValidationError("request_deadline_exceeded", "Adaptive request deadline was reached.")
        requests = decision.capability_requests
        if not 1 <= len(requests) <= budget.max_capabilities_per_decision:
            raise AgentActionValidationError("capability_count_invalid", "Investigator decision exceeded its per-turn capability bound.")
        if budget.capability_calls + len(requests) > budget.max_total_capability_calls:
            raise AgentActionValidationError("tool_budget_exhausted", "Adaptive request capability budget was exhausted.")
        gap = next((item for item in ledger.gaps if item.gap_id == decision.evidence_gap_id), None)
        if gap is None or gap.status != "open":
            raise AgentActionValidationError("evidence_gap_invalid", "Investigator selected an unknown or closed evidence gap.")

        authorized_entities = set(ledger.authorized_entities) | set(ledger.structured_candidates)
        steps: list[PlanStep] = []
        selected_entities: list[str] = []
        for index, request in enumerate(requests, start=1):
            try:
                spec = self.registry.get(request.capability)
            except KeyError as exc:
                raise AgentActionValidationError("unknown_capability", "Investigator requested an unknown capability.") from exc
            if not spec.read_only or not spec.planner_visible or spec.side_effect_class != "read_only":
                raise AgentActionValidationError("capability_not_authorized", "Investigator requested a non-authorized capability.")
            if request.capability not in gap.authorized_capabilities:
                raise AgentActionValidationError("capability_not_authorized_for_gap", "Capability is not authorized for the selected evidence gap.")
            if not constraints.allow_live:
                raise AgentActionValidationError("live_capability_forbidden_by_request", "Live actions are forbidden by request policy.")

            arguments = dict(request.arguments)
            if request.capability in {"graph.search_assets", "graph.aggregate_assets"}:
                canonical = self._structured_arguments(task, request.capability)
                if arguments and arguments != canonical:
                    raise AgentActionValidationError(
                        "structured_query_authority_violation",
                        "Investigator attempted to alter the Router-owned structured query.",
                    )
                arguments = canonical
            entities = tuple(str(item) for item in tuple(arguments.get("entities") or ()))
            if not entities and spec.required_entity_cardinality[0] > 0:
                if len(gap.entities) == spec.required_entity_cardinality[0]:
                    entities = gap.entities
                elif len(task.entities) == spec.required_entity_cardinality[0]:
                    entities = task.entities
                if entities:
                    arguments["entities"] = list(entities)
            if any(entity not in authorized_entities for entity in entities):
                raise AgentActionValidationError("entity_authority_violation", "Investigator introduced an unauthorized entity.")
            if gap.entities and entities != gap.entities:
                raise AgentActionValidationError(
                    "evidence_gap_entity_scope_violation",
                    "Investigator selected an entity scope outside the chosen evidence gap.",
                )
            selected_entities.extend(entity for entity in entities if entity not in selected_entities)
            self._validate_graph_authority(request.capability, arguments, task)
            steps.append(PlanStep(
                id=f"agent-{turn}-{index}",
                capability=request.capability,
                arguments=arguments,
                requirement="required" if gap.importance == "required" else "optional",
                expected_evidence_type=spec.evidence_type,
            ))

        deepened = tuple(
            entity for entity in selected_entities
            if entity in ledger.structured_candidates and entity not in budget.deepened_entities
        )
        if len(set((*budget.deepened_entities, *deepened))) > budget.max_deepened_entities:
            raise AgentActionValidationError("entity_deepening_budget_exhausted", "Adaptive focal entity bound was exceeded.")
        action_task = replace(
            task,
            entities=tuple(selected_entities) or task.entities,
            required_capabilities=tuple(dict.fromkeys(step.capability for step in steps)),
            optional_capabilities=(),
            workflow_mode="direct",
            orchestration_mode="adaptive",
            recommended_steps=len(steps),
        )
        plan = ExecutionPlan(
            task=action_task,
            steps=tuple(steps),
            max_iterations=1,
            validated=False,
            plan_id=f"agent-{turn}-{uuid4().hex[:8]}",
            goal=task.request,
            target_entities=tuple(selected_entities) or task.entities,
            maximum_allowed_calls=len(steps),
            planner_called=False,
            source="investigator",
            stop_condition="selected_evidence_gap_observed",
        )
        try:
            validated = self.plan_validator.validate(plan)
        except PlanValidationError as exc:
            raise AgentActionValidationError(exc.code, "Adaptive action failed deterministic plan validation.") from exc
        fingerprints = tuple(self.fingerprint(step) for step in validated.steps)
        for step, fingerprint in zip(validated.steps, fingerprints):
            equivalent = equivalent_evidence_for_step(
                step,
                gap,
                task,
                ledger.evidence_references,
                action_fingerprint=fingerprint,
            )
            if equivalent is not None:
                raise AgentActionValidationError(
                    equivalent.reason,
                    "Equivalent evidence is already represented in the request ledger.",
                    evidence_reference_id=equivalent.reference_id,
                    evidence_gap_id=gap.gap_id,
                )
        if any(item in ledger.action_fingerprints for item in fingerprints):
            raise AgentActionValidationError("repeated_action", "Investigator repeated an equivalent action without new evidence.")
        return ValidatedAgentAction(validated, fingerprints, deepened)

    @staticmethod
    def fingerprint(step: PlanStep) -> str:
        return canonical_action_fingerprint(step)

    @staticmethod
    def _structured_arguments(task: TaskSpec, capability: str) -> dict[str, Any]:
        if task.structured_query is None:
            raise AgentActionValidationError("structured_query_missing", "Structured capability lacks Router-owned selectors.")
        direct = compile_direct_plan(replace(
            task,
            required_capabilities=(capability,),
            optional_capabilities=(),
            orchestration_mode="direct",
        ))
        if not direct.steps or direct.steps[0].capability != capability:
            raise AgentActionValidationError("structured_query_mismatch", "Structured capability contradicts the validated task.")
        return dict(direct.steps[0].arguments)

    @staticmethod
    def _validate_graph_authority(capability: str, arguments: dict[str, Any], task: TaskSpec) -> None:
        if not capability.startswith("graph.") or capability in {"graph.search_assets", "graph.aggregate_assets"}:
            return
        supplied_depth = arguments.get("depth")
        if supplied_depth is not None and int(supplied_depth) > task.graph_depth:
            raise AgentActionValidationError("graph_depth_authority_violation", "Investigator broadened Router-owned Graph depth.")
        supplied_direction = arguments.get("direction")
        if supplied_direction not in {None, task.direction}:
            raise AgentActionValidationError("graph_direction_authority_violation", "Investigator broadened Router-owned Graph direction.")
        supplied_scope = arguments.get("scope")
        expected_scope = "node_summary" if capability == "graph.get_summary" else task.scope
        if supplied_scope not in {None, expected_scope}:
            raise AgentActionValidationError("graph_scope_authority_violation", "Investigator broadened Router-owned Graph scope.")
