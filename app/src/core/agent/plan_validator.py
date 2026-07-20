"""Deterministic validation for model-proposed and compiled execution plans."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from src.core.agent.contracts import ExecutionPlan, PlanStep
from src.core.agent.registry import CapabilityRegistry


class PlanValidationError(ValueError):
    """A plan is structurally invalid or exceeds a deterministic policy limit."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlanValidator:
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        max_calls: int = 6,
        max_entities: int = 2,
        max_graph_depth: int = 2,
    ) -> None:
        self.registry = registry
        self.max_calls = max(1, max_calls)
        self.max_entities = max(1, max_entities)
        self.max_graph_depth = max(0, max_graph_depth)

    def validate(self, plan: ExecutionPlan) -> ExecutionPlan:
        if len(plan.task.entities) > self.max_entities:
            raise PlanValidationError("maximum_entities_exceeded", "Plan target entity limit exceeded.")
        if any(entity not in plan.task.entities for entity in plan.target_entities):
            raise PlanValidationError("entity_authority_violation", "Plan attempted to introduce an unresolved target entity.")
        if not plan.steps:
            if plan.task.required_capabilities:
                raise PlanValidationError("plan_steps_missing", "Plan contains no execution steps.")
            return replace(plan, validated=True, maximum_allowed_calls=self.max_calls)
        if len(plan.steps) > min(self.max_calls, plan.maximum_allowed_calls):
            raise PlanValidationError("maximum_calls_exceeded", "Plan capability call limit exceeded.")

        step_ids = [step.id.strip() for step in plan.steps]
        if not all(step_ids) or len(step_ids) != len(set(step_ids)):
            raise PlanValidationError("step_id_invalid", "Plan step identifiers must be unique and non-empty.")

        normalized: list[PlanStep] = []
        signatures: set[str] = set()
        known_ids = set(step_ids)
        for step in plan.steps:
            try:
                spec = self.registry.get(step.capability)
            except KeyError as exc:
                raise PlanValidationError("unknown_capability", "Plan requested an unknown capability.") from exc
            if not spec.read_only or spec.side_effect_class != "read_only":
                raise PlanValidationError("capability_not_read_only", "Plan requested a non-read-only capability.")
            if plan.source == "llm" and not spec.planner_visible:
                raise PlanValidationError("capability_not_planner_visible", "Plan requested a hidden capability.")
            if any(dependency not in known_ids for dependency in step.depends_on):
                raise PlanValidationError("unknown_dependency", "Plan references an unknown dependency.")
            if step.id in step.depends_on:
                raise PlanValidationError("self_dependency", "Plan step cannot depend on itself.")

            arguments = dict(step.arguments)
            if step.capability == "knowledge.search":
                arguments.setdefault("query", plan.task.request)
            else:
                arguments.setdefault("entities", list(plan.task.entities))
            entities = tuple(arguments.get("entities") or ())
            if any(entity not in plan.task.entities for entity in entities):
                raise PlanValidationError("entity_authority_violation", "Plan attempted to introduce an unresolved entity.")
            minimum, maximum = spec.required_entity_cardinality
            if not minimum <= len(entities) <= maximum:
                raise PlanValidationError("entity_cardinality_invalid", "Plan capability entity cardinality is invalid.")
            depth = int(arguments.get("depth", plan.task.graph_depth) or 0)
            if step.capability.startswith("graph.") and depth > min(self.max_graph_depth, spec.maximum_graph_depth or self.max_graph_depth):
                raise PlanValidationError("maximum_graph_depth_exceeded", "Plan graph depth limit exceeded.")
            try:
                spec.input_schema.model_validate(arguments)
            except ValidationError as exc:
                raise PlanValidationError("argument_schema_invalid", "Plan capability arguments are invalid.") from exc

            signature = json.dumps(
                {"capability": step.capability, "arguments": arguments},
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            if signature in signatures:
                raise PlanValidationError("duplicate_capability_call", "Plan contains duplicate equivalent capability calls.")
            signatures.add(signature)
            normalized.append(replace(step, arguments=arguments))

        self._validate_dag(normalized)
        return replace(
            plan,
            steps=tuple(normalized),
            validated=True,
            target_entities=plan.target_entities or plan.task.entities,
            goal=plan.goal or plan.task.request,
            maximum_allowed_calls=min(self.max_calls, plan.maximum_allowed_calls),
        )

    @staticmethod
    def _validate_dag(steps: list[PlanStep]) -> None:
        dependencies = {step.id: set(step.depends_on) for step in steps}
        completed: set[str] = set()
        while len(completed) < len(steps):
            ready = {
                step_id
                for step_id, required in dependencies.items()
                if step_id not in completed and required <= completed
            }
            if not ready:
                raise PlanValidationError("dependency_cycle", "Plan dependencies must form a directed acyclic graph.")
            completed.update(ready)
