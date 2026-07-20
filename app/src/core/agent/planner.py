"""Single-pass structured LLM planner for validated multi-step tasks."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from src.core.agent.contracts import CapabilitySpec, ExecutionPlan, PlanStep, TaskSpec
from src.core.llm.errors import LLMError
from src.core.llm.output_parser import parse_json_object


PLANNER_SYSTEM_PROMPT = """You are the bounded Soorin investigation planner.
Return exactly one JSON object. Select only capabilities supplied in the capability catalog.
Use only supplied resolved entities. Never invent entities, URLs, permissions, tools, or actions.
All steps are read-only. Maximum 6 steps, maximum graph depth 2, and no recursive work.
Schema: {"goal":str,"target_entities":[str],"steps":[{"step_id":str,"capability":str,"arguments":object,"depends_on":[str],"required":bool,"expected_evidence":str}],"stop_condition":str}.
"""


class PlannerStepPayload(BaseModel):
    step_id: str = Field(min_length=1, max_length=64)
    capability: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=6)
    required: bool = True
    expected_evidence: str = "provider_evidence"


class PlannerPayload(BaseModel):
    goal: str = Field(min_length=1, max_length=600)
    target_entities: list[str] = Field(default_factory=list, max_length=2)
    steps: list[PlannerStepPayload] = Field(default_factory=list, max_length=6)
    stop_condition: str = "required_evidence_collected"


class PlannerError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BoundedPlanner:
    """Propose one plan and perform at most one compact schema-repair attempt."""

    def __init__(self, llm_client: Any, *, repair_enabled: bool = True) -> None:
        self.llm_client = llm_client
        self.repair_enabled = repair_enabled
        self.last_repair_used = False

    def plan(
        self,
        task: TaskSpec,
        capabilities: tuple[CapabilitySpec, ...],
        *,
        request_id: str,
        events: Any | None = None,
    ) -> ExecutionPlan:
        if task.workflow_mode != "multi_step":
            raise PlannerError("planner_not_required", "Planner may run only for multi-step tasks.")
        self.last_repair_used = False
        messages = self._messages(task, capabilities)
        try:
            result = self.llm_client.chat(messages, request_id=request_id, purpose="planner", transient_retries=0)
            return self._parse(result.text, task)
        except (LLMError, PlannerError, ValidationError, ValueError) as first_error:
            if not self.repair_enabled or isinstance(first_error, LLMError):
                raise PlannerError("planner_failed", "Planner did not produce a usable plan.") from first_error
            self.last_repair_used = True
            if events:
                events.emit("planner_repair_started", planner_called=True, reason="structured_output_invalid")
            repair_messages = [
                *messages,
                {
                    "role": "user",
                    "content": "The previous answer did not match the required JSON schema. Return one corrected JSON object only.",
                },
            ]
            try:
                repaired = self.llm_client.chat(
                    repair_messages,
                    request_id=request_id,
                    purpose="planner_repair",
                    transient_retries=0,
                )
                plan = self._parse(repaired.text, task)
                if events:
                    events.emit("planner_repair_completed", plan_id=plan.plan_id, planner_called=True)
                return plan
            except (LLMError, PlannerError, ValidationError, ValueError) as repair_error:
                raise PlannerError("planner_repair_failed", "Planner repair did not produce a usable plan.") from repair_error

    @staticmethod
    def _messages(task: TaskSpec, capabilities: tuple[CapabilitySpec, ...]) -> list[dict[str, str]]:
        catalog = [
            {
                "name": spec.name,
                "description": spec.description,
                "entity_cardinality": list(spec.required_entity_cardinality),
                "evidence_type": spec.evidence_type,
                "max_graph_depth": spec.maximum_graph_depth,
                "read_only": spec.read_only,
            }
            for spec in capabilities
            if spec.planner_visible and spec.read_only
        ]
        task_payload = {
            "operation": task.intent,
            "request": task.request,
            "entities": list(task.entities),
            "required_evidence_domains": list(task.required_capabilities),
            "graph_scope": task.scope,
            "direction": task.direction,
            "depth": task.graph_depth,
            "detail_level": task.detail_level,
            "hard_limits": {"max_calls": 6, "max_entities": 2, "max_graph_depth": 2},
            "capabilities": catalog,
        }
        return [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False, separators=(",", ":"))},
        ]

    @staticmethod
    def _parse(text: str, task: TaskSpec) -> ExecutionPlan:
        try:
            payload = PlannerPayload.model_validate(parse_json_object(text))
        except Exception as exc:
            raise PlannerError("planner_schema_invalid", "Planner output did not match the required schema.") from exc
        steps = tuple(
            PlanStep(
                id=item.step_id,
                capability=item.capability,
                arguments=item.arguments,
                depends_on=tuple(item.depends_on),
                requirement="required" if item.required else "optional",
                expected_evidence_type=item.expected_evidence,
            )
            for item in payload.steps
        )
        return ExecutionPlan(
            task=task,
            steps=steps,
            max_iterations=1,
            validated=False,
            plan_id=uuid4().hex[:12],
            goal=payload.goal,
            target_entities=tuple(payload.target_entities),
            maximum_allowed_calls=6,
            planner_called=True,
            source="llm",
            stop_condition=payload.stop_condition,
        )
