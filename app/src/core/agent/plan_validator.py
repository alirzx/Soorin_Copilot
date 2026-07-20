"""Deterministic validation for model-proposed and compiled execution plans."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from src.core.agent.contracts import ExecutionPlan, PlanStep
from src.core.agent.registry import CapabilityRegistry
from src.core.context.product_views import approved_views, select_product_views


KNOWLEDGE_PURPOSES = {
    "general_reference",
    "interpret_evidence",
    "recommended_response_actions",
    "response_actions",
    "investigation_procedure",
    "hardening_guidance",
}


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
        knowledge_signatures: list[tuple[str, str, set[str]]] = []
        knowledge_budget_total = 0
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
                arguments.setdefault("purpose", "interpret_evidence")
                arguments.setdefault("max_context_tokens", 3000)
                knowledge_budget_total += int(arguments["max_context_tokens"])
                if knowledge_budget_total > 6000:
                    raise PlanValidationError(
                        "knowledge_budget_exceeded",
                        "Combined Knowledge context budget exceeds the bounded maximum.",
                    )
                purpose = str(arguments["purpose"]).strip().casefold()
                if purpose not in KNOWLEDGE_PURPOSES:
                    raise PlanValidationError("knowledge_purpose_invalid", "Knowledge purpose is not approved.")
                normalized_query = _normalize_query(str(arguments["query"]))
                if not normalized_query:
                    raise PlanValidationError("knowledge_query_invalid", "Knowledge query is empty after normalization.")
                query_hash = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()[:16]
                query_terms = set(normalized_query.split())
                if len(knowledge_signatures) >= 2:
                    raise PlanValidationError("knowledge_call_limit_exceeded", "At most two Knowledge calls are allowed.")
                for previous_purpose, previous_hash, previous_terms in knowledge_signatures:
                    equivalent = query_hash == previous_hash or _query_similarity(query_terms, previous_terms) >= 0.85
                    if purpose == previous_purpose or equivalent:
                        raise PlanValidationError(
                            "duplicate_knowledge_search",
                            "A second Knowledge call requires a distinct purpose and meaningfully different query.",
                        )
                knowledge_signatures.append((purpose, query_hash, query_terms))
            else:
                arguments.setdefault("entities", list(plan.task.entities))
            if step.capability in {"asset.get_profile", "asset.get_detection"}:
                provider = "asset_profile" if step.capability == "asset.get_profile" else "detection"
                detail = str(arguments.get("detail") or plan.task.detail_level)
                if detail in {"detailed", "report"}:
                    detail = "deep"
                if detail not in {"brief", "standard", "deep"}:
                    raise PlanValidationError("product_detail_invalid", "Plan requested an invalid Product detail level.")
                views = tuple(
                    arguments.get("views")
                    or select_product_views(provider, plan.task.request, detail)
                )
                if len(views) > 5 or any(view not in approved_views(provider) for view in views):
                    raise PlanValidationError("product_view_invalid", "Plan requested an unapproved Product evidence view.")
                arguments["views"] = list(dict.fromkeys(views))
                arguments["detail"] = detail
                arguments.setdefault("max_context_tokens", 5000 if detail == "deep" else 3000)
                arguments.setdefault(
                    "purpose",
                    "assess_anomaly"
                    if provider == "detection" and "anomaly_risk" in views
                    else "establish_identity"
                    if "identity_role" in views
                    else "asset_summary",
                )
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


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", query.casefold())).strip()


def _query_similarity(first: set[str], second: set[str]) -> float:
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)
