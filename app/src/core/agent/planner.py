"""Single-pass structured LLM planner for validated multi-step tasks."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from src.core.agent.contracts import CapabilitySpec, ExecutionPlan, PlanStep, TaskSpec
from src.core.llm.errors import LLMError
from src.core.llm.output_parser import parse_json_object


logger = logging.getLogger(__name__)
DEFAULT_PLANNER_PROMPT_PATH = "app/prompts/planner_system_prompt.md"


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
    """Call the Planner model once; deterministic code owns repair and fallback."""

    def __init__(
        self,
        llm_client: Any,
        *,
        repair_enabled: bool = True,
        system_prompt_path: str = DEFAULT_PLANNER_PROMPT_PATH,
    ) -> None:
        self.llm_client = llm_client
        self.repair_enabled = repair_enabled
        self.last_repair_used = False
        self.system_prompt_path = system_prompt_path
        self.system_prompt = self._load_prompt(system_prompt_path)

    @staticmethod
    def _load_prompt(configured_path: str) -> str:
        path = Path(configured_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            prompt = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("Tracked Planner prompt is missing.") from exc
        if not prompt:
            raise RuntimeError("Tracked Planner prompt is empty.")
        safe_path = configured_path if not Path(configured_path).is_absolute() else path.name
        logger.info("event=planner_prompt_loaded path=%s chars=%s", safe_path, len(prompt))
        return prompt

    def plan(
        self,
        task: TaskSpec,
        capabilities: tuple[CapabilitySpec, ...],
        *,
        request_id: str,
        trace_id: str = "",
        events: Any | None = None,
    ) -> ExecutionPlan:
        if task.workflow_mode != "multi_step":
            raise PlannerError("planner_not_required", "Planner may run only for multi-step tasks.")
        self.last_repair_used = False
        try:
            result = self.llm_client.chat(
                self._messages(task, capabilities),
                request_id=request_id,
                purpose="planner",
                transient_retries=0,
                trace_id=trace_id,
            )
            if not str(getattr(result, "text", "") or "").strip():
                finish_reason = str(getattr(result, "finish_reason", "") or "")
                reasoning_present = bool(getattr(result, "reasoning_present", False))
                code = (
                    "planner_reasoning_exhausted"
                    if finish_reason == "length" and reasoning_present
                    else "planner_empty_output"
                )
                logger.warning(
                    "event=planner_output_unusable request_id=%s reason=%s finish_reason=%s reasoning_present=%s",
                    request_id,
                    code,
                    finish_reason or "none",
                    reasoning_present,
                )
                raise PlannerError(code, "Planner did not produce a usable structured plan.")
            return self._parse(result.text, task)
        except LLMError as exc:
            raise PlannerError("planner_failed", "Planner provider call failed safely.") from exc
        except PlannerError:
            raise
        except ValueError as exc:
            raise PlannerError("planner_schema_invalid", "Planner did not produce one valid JSON plan.") from exc

    def _messages(self, task: TaskSpec, capabilities: tuple[CapabilitySpec, ...]) -> list[dict[str, str]]:
        catalog = [self._catalog_entry(spec) for spec in capabilities if spec.planner_visible and spec.read_only]
        task_payload = {
            "task": {
                "intent": task.intent,
                "request": task.request,
                "entities": list(task.entities),
                "required_capabilities": list(task.required_capabilities),
                "optional_capabilities": list(task.optional_capabilities),
                "graph_scope": task.scope,
                "direction": task.direction,
                "depth": task.graph_depth,
                "relationship_mode": task.relationship_mode,
                "detail_level": task.detail_level,
            },
            "hard_limits": {"max_calls": 6, "max_entities": 2, "max_graph_depth": 2},
            "capability_catalog": catalog,
        }
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False, separators=(",", ":"))},
        ]

    @staticmethod
    def _catalog_entry(spec: CapabilitySpec) -> dict[str, Any]:
        schema = spec.input_schema.model_json_schema()
        allowed_arguments = list(spec.allowed_arguments or tuple(schema.get("properties", {})))
        properties = schema.get("properties", {})
        return {
            "name": spec.name,
            "purpose": spec.description,
            "minimum_entities": spec.required_entity_cardinality[0],
            "maximum_entities": spec.required_entity_cardinality[1],
            "allowed_arguments": allowed_arguments,
            "argument_schema": {
                "type": "object",
                "properties": {
                    name: properties[name]
                    for name in allowed_arguments
                    if name in properties
                },
                "additionalProperties": False,
            },
            "allowed_views": list(spec.allowed_views),
            "allowed_detail_levels": list(spec.allowed_detail_levels),
            "allowed_knowledge_purposes": list(spec.allowed_purposes),
            "allowed_scopes": list(spec.allowed_scopes),
            "allowed_depths": list(spec.allowed_depths),
            "dependencies": list(spec.dependencies),
            "parallelization": spec.parallelization,
            "reusable_locally": spec.reusable_locally,
            "read_only": spec.read_only,
        }

    @staticmethod
    def _parse(text: str, task: TaskSpec) -> ExecutionPlan:
        try:
            payload = PlannerPayload.model_validate(parse_json_object(text))
        except Exception as exc:
            raise PlannerError("planner_schema_invalid", "Planner output did not match the required schema.") from exc
        return ExecutionPlan(
            task=task,
            steps=tuple(
                PlanStep(
                    id=item.step_id,
                    capability=item.capability,
                    arguments=item.arguments,
                    depends_on=tuple(item.depends_on),
                    requirement="required" if item.required else "optional",
                    expected_evidence_type=item.expected_evidence,
                )
                for item in payload.steps
            ),
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
