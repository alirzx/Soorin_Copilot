"""Deterministic validation for model-proposed and compiled execution plans."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from src.core.agent.contracts import ExecutionPlan, PlanStep
from src.core.agent.context_identity import (
    knowledge_actions_equivalent,
    knowledge_query_hash,
    knowledge_query_term_hashes,
    normalize_knowledge_query,
)
from src.core.agent.registry import CapabilityRegistry
from src.core.context.product_views import (
    approved_views,
    normalize_product_views,
    normalize_purpose,
    select_product_views,
)


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

    def __init__(
        self,
        code: str,
        message: str,
        *,
        step_id: str = "",
        capability: str = "",
        field: str = "plan",
        validation_rule: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.step_id = step_id
        self.capability = capability
        self.field = field
        self.validation_rule = validation_rule or code


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

    def validate(
        self,
        plan: ExecutionPlan,
        *,
        satisfied_capabilities: tuple[str, ...] = (),
    ) -> ExecutionPlan:
        if len(plan.task.entities) > self.max_entities:
            raise PlanValidationError("maximum_entities_exceeded", "Plan target entity limit exceeded.")
        if any(entity not in plan.task.entities for entity in plan.target_entities):
            raise PlanValidationError("entity_authority_violation", "Plan attempted to introduce an unresolved target entity.")
        satisfied = set(satisfied_capabilities)
        if not plan.steps:
            if set(plan.task.required_capabilities + plan.task.optional_capabilities) - satisfied:
                raise PlanValidationError("plan_steps_missing", "Plan contains no execution steps.")
            return replace(plan, validated=True, maximum_allowed_calls=self.max_calls)
        if len(plan.steps) > min(self.max_calls, plan.maximum_allowed_calls):
            raise PlanValidationError("maximum_calls_exceeded", "Plan capability call limit exceeded.")

        step_ids = [step.id.strip() for step in plan.steps]
        if not all(step_ids) or len(step_ids) != len(set(step_ids)):
            raise PlanValidationError("step_id_invalid", "Plan step identifiers must be unique and non-empty.")

        normalized: list[PlanStep] = []
        signatures: set[str] = set()
        knowledge_signatures: list[tuple[str, str, tuple[str, ...]]] = []
        knowledge_budget_total = 0
        known_ids = set(step_ids)
        for step in plan.steps:
            try:
                spec = self.registry.get(step.capability)
            except KeyError as exc:
                raise PlanValidationError("unknown_capability", "Plan requested an unknown capability.") from exc
            if not spec.read_only or spec.side_effect_class != "read_only":
                raise PlanValidationError("capability_not_read_only", "Plan requested a non-read-only capability.")
            if plan.source in {"llm", "investigator"} and not spec.planner_visible:
                raise PlanValidationError("capability_not_planner_visible", "Plan requested a hidden capability.")
            if any(dependency not in known_ids for dependency in step.depends_on):
                raise PlanValidationError("unknown_dependency", "Plan references an unknown dependency.")
            if step.id in step.depends_on:
                raise PlanValidationError("self_dependency", "Plan step cannot depend on itself.")

            arguments = dict(step.arguments)
            allowed_arguments = set(
                spec.allowed_arguments
                or tuple(spec.input_schema.model_json_schema().get("properties", {}))
            )
            unsupported = (
                sorted(set(arguments) - allowed_arguments)
                if plan.source in {"llm", "investigator"}
                else []
            )
            if unsupported:
                raise PlanValidationError(
                    "unsupported_argument",
                    "Plan capability arguments contain an unsupported field.",
                    step_id=step.id,
                    capability=step.capability,
                    field=unsupported[0],
                    validation_rule="capability_argument_allowlist",
                )
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
                normalized_query = normalize_knowledge_query(str(arguments["query"]))
                if not normalized_query:
                    raise PlanValidationError("knowledge_query_invalid", "Knowledge query is empty after normalization.")
                query_hash = knowledge_query_hash(normalized_query)
                query_terms = knowledge_query_term_hashes(normalized_query)
                if len(knowledge_signatures) >= 2:
                    raise PlanValidationError("knowledge_call_limit_exceeded", "At most two Knowledge calls are allowed.")
                for previous_purpose, previous_hash, previous_terms in knowledge_signatures:
                    equivalent = query_hash == previous_hash or knowledge_actions_equivalent(
                        purpose, query_terms, previous_purpose, previous_terms
                    )
                    if equivalent:
                        raise PlanValidationError(
                            "duplicate_knowledge_search",
                            "A second Knowledge call requires a distinct purpose and meaningfully different query.",
                        )
                knowledge_signatures.append((purpose, query_hash, query_terms))
            elif "entities" in allowed_arguments:
                arguments.setdefault("entities", list(plan.task.entities))
            if step.capability in {"asset.get_profile", "asset.get_detection"}:
                provider = "asset_profile" if step.capability == "asset.get_profile" else "detection"
                detail = str(arguments.get("detail") or plan.task.detail_level)
                if detail in {"detailed", "report"}:
                    detail = "deep"
                if detail not in {"brief", "standard", "deep"}:
                    raise PlanValidationError("product_detail_invalid", "Plan requested an invalid Product detail level.")
                views = normalize_product_views(provider, tuple(
                    arguments.get("views")
                    or select_product_views(provider, plan.task.request, detail)
                ))
                if len(views) > 6 or any(view not in approved_views(provider) for view in views):
                    raise PlanValidationError("product_view_invalid", "Plan requested an unapproved Product evidence view.")
                arguments["views"] = list(dict.fromkeys(views))
                arguments["detail"] = detail
                arguments.setdefault("max_context_tokens", 5000 if detail == "deep" else 3000)
                arguments.setdefault(
                    "purpose",
                    "explain_detection"
                    if provider == "detection" and "evidence" in views
                    else "establish_identity"
                    if "identity" in views
                    else "asset_summary",
                )
                arguments["purpose"] = normalize_purpose(
                    str(arguments["purpose"]),
                    "asset_summary",
                )
            graph_scope_contracts = {
                    "graph.get_summary": "node_summary",
                    "graph.get_neighbors": plan.task.scope,
                    "graph.get_relationship": "one_hop",
                    "graph.compare_assets": "multi_entity_comparison",
                    "graph.find_path": "path",
                }
            graph_depth_contracts = {
                    "graph.get_summary": 0,
                    "graph.get_neighbors": plan.task.graph_depth,
                    "graph.get_relationship": 1,
                    "graph.compare_assets": 1,
                    "graph.find_path": 0,
                }
            if step.capability in graph_scope_contracts:
                expected_scope = graph_scope_contracts[step.capability]
                expected_depth = graph_depth_contracts[step.capability]
                supplied_scope = arguments.get("scope")
                supplied_depth = arguments.get("depth")
                if supplied_scope not in {None, expected_scope}:
                    raise PlanValidationError(
                        "graph_scope_authority_violation",
                        "Planner contradicted the Router-owned Graph scope.",
                    )
                if supplied_depth is not None and int(supplied_depth) != expected_depth:
                    raise PlanValidationError(
                        "graph_depth_authority_violation",
                        "Planner contradicted the Router-owned Graph depth.",
                    )
                arguments["scope"] = expected_scope
                arguments["direction"] = plan.task.direction
                arguments["depth"] = expected_depth
                arguments["relationship_mode"] = plan.task.relationship_mode
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
                parsed_arguments = spec.input_schema.model_validate(arguments)
            except ValidationError as exc:
                first_error = exc.errors(include_url=False)[0] if exc.errors() else {}
                location = ".".join(str(item) for item in first_error.get("loc", ())) or "arguments"
                rule = str(first_error.get("type") or "pydantic_schema_validation")
                raise PlanValidationError(
                    "argument_schema_invalid",
                    "Plan capability arguments are invalid.",
                    step_id=step.id,
                    capability=step.capability,
                    field=location,
                    validation_rule=rule,
                ) from exc
            requested_limit = getattr(parsed_arguments, "limit", None)
            if (
                requested_limit is not None
                and spec.maximum_result_scope is not None
                and requested_limit > spec.maximum_result_scope
            ):
                raise PlanValidationError(
                    "maximum_result_scope_exceeded",
                    "Plan capability result limit exceeds the configured maximum.",
                    step_id=step.id,
                    capability=step.capability,
                    field="limit",
                    validation_rule="maximum_result_scope",
                )

            signature = json.dumps(
                {"capability": step.capability, "arguments": arguments},
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            if signature in signatures:
                raise PlanValidationError("duplicate_capability_call", "Plan contains duplicate equivalent capability calls.")
            signatures.add(signature)
            requirement = (
                "required"
                if step.capability in plan.task.required_capabilities
                else "optional"
                if step.capability in plan.task.optional_capabilities
                else step.requirement
            )
            normalized.append(
                replace(
                    step,
                    arguments=arguments,
                    requirement=requirement,
                )
            )

        self._validate_dag(normalized)
        planned_capabilities = {step.capability for step in normalized}
        missing_required = set(plan.task.required_capabilities) - planned_capabilities - satisfied
        if missing_required:
            raise PlanValidationError(
                "required_capability_missing",
                "Plan omitted a capability required by the validated TaskSpec.",
            )
        missing_optional = set(plan.task.optional_capabilities) - planned_capabilities - satisfied
        if missing_optional:
            raise PlanValidationError(
                "optional_capability_missing",
                "Plan omitted an optional enrichment requested by the validated TaskSpec.",
            )
        return replace(
            plan,
            steps=tuple(normalized),
            validated=True,
            target_entities=plan.target_entities or plan.task.entities,
            goal=plan.goal or plan.task.request,
            maximum_allowed_calls=min(self.max_calls, plan.maximum_allowed_calls),
        )

    def repair(
        self,
        plan: ExecutionPlan,
        error: PlanValidationError,
    ) -> tuple[ExecutionPlan, tuple[str, ...]]:
        """Perform one bounded mechanical repair without another model call."""
        steps = list(plan.steps)
        actions: list[str] = []
        if error.code == "entity_cardinality_invalid":
            repaired: list[PlanStep] = []
            for step in steps:
                entities = tuple(step.arguments.get("entities") or ())
                if step.capability in {"asset.get_profile", "asset.get_detection"} and len(entities) > 1:
                    for entity in entities:
                        repaired.append(
                            replace(
                                step,
                                id=f"{step.id}-{len(repaired) + 1}",
                                arguments={**step.arguments, "entities": [entity]},
                            )
                        )
                    actions.append("split_multi_entity_product_call")
                else:
                    repaired.append(step)
            steps = repaired
        elif error.code in {"duplicate_knowledge_search", "duplicate_capability_call"}:
            seen: set[str] = set()
            seen_knowledge: list[tuple[str, tuple[str, ...]]] = []
            repaired = []
            for step in steps:
                signature = json.dumps(
                    {"capability": step.capability, "arguments": step.arguments},
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                )
                normalized_query = normalize_knowledge_query(str(step.arguments.get("query", "")))
                if step.capability == "knowledge.search":
                    purpose = str(step.arguments.get("purpose", "interpret_evidence")).strip().casefold()
                    terms = knowledge_query_term_hashes(normalized_query)
                    if any(
                        knowledge_actions_equivalent(
                            purpose, terms, previous_purpose, previous_terms
                        )
                        for previous_purpose, previous_terms in seen_knowledge
                    ):
                        actions.append("remove_duplicate_retrieval")
                        continue
                    seen_knowledge.append((purpose, terms))
                    signature = f"knowledge:{normalized_query}:{purpose}"
                if signature in seen:
                    actions.append("remove_duplicate_retrieval")
                    continue
                seen.add(signature)
                repaired.append(step)
            steps = repaired
        elif error.code == "step_id_invalid":
            id_map: dict[str, str] = {}
            repaired = []
            for index, step in enumerate(steps, start=1):
                new_id = f"step-{index}"
                id_map.setdefault(step.id, new_id)
                repaired.append(replace(step, id=new_id))
            steps = [
                replace(step, depends_on=tuple(id_map.get(item, item) for item in step.depends_on))
                for step in repaired
            ]
            actions.append("normalize_step_ids")
        elif error.code == "unknown_dependency":
            known = {step.id for step in steps}
            steps = [
                replace(step, depends_on=tuple(item for item in step.depends_on if item in known))
                for step in steps
            ]
            actions.append("remove_missing_dependency_reference")
        else:
            return plan, ()
        if not actions or len(steps) > min(self.max_calls, plan.maximum_allowed_calls):
            return plan, ()
        return replace(plan, steps=tuple(steps), validated=False), tuple(actions)

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
