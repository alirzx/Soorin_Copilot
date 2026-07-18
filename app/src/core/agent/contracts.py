"""Minimal typed contracts for bounded Copilot investigations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict


ToolStatus = Literal["ok", "empty", "not_configured", "unavailable", "invalid", "partial", "not_found"]
Completeness = Literal["complete", "partial", "unknown"]
ReviewOutcome = Literal["sufficient", "answer_with_limitations", "missing_required_evidence", "safe_failure"]
WorkflowMode = Literal["direct", "multi_step"]


@dataclass(frozen=True)
class EvidenceFact:
    source_capability: str
    statement: str
    value: Any
    entity: str | None = None
    fact_type: str = "observed"
    confidence: float | None = None


@dataclass(frozen=True)
class ToolResult:
    status: ToolStatus
    entities: tuple[str, ...]
    source_capability: str
    retrieved_at: str
    freshness: str
    completeness: Completeness
    facts: tuple[EvidenceFact, ...] = ()
    limitations: tuple[str, ...] = ()
    total_count: int | None = None
    included_count: int | None = None
    truncated: bool = False
    error_classification: str | None = None
    contradictions: tuple[str, ...] = ()
    context_included: bool = True


@dataclass(frozen=True)
class TaskSpec:
    request: str
    intent: str
    scope: str
    direction: str
    entities: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    workflow_mode: WorkflowMode = "direct"
    semantic_decision_source: str = "unknown"
    requires_multiple_entities: bool = False
    max_steps: int = 1


@dataclass(frozen=True)
class PlanStep:
    id: str
    capability: str
    depends_on: tuple[str, ...] = ()
    status: str = "pending"


@dataclass(frozen=True)
class ExecutionPlan:
    task: TaskSpec
    steps: tuple[PlanStep, ...]
    max_iterations: int = 1
    validated: bool = False


@dataclass(frozen=True)
class EvidencePack:
    task: TaskSpec
    tool_results: tuple[ToolResult, ...]
    facts: tuple[EvidenceFact, ...]
    limitations: tuple[str, ...]
    contradictions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewDecision:
    outcome: ReviewOutcome
    reasons: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 0
    retryable_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    version: str
    input_schema: type[Any]
    output_schema: type[Any]
    required_entity_cardinality: tuple[int, int]
    read_only: bool
    timeout_seconds: float
    retry_policy: RetryPolicy
    planner_visible: bool


class InvestigationState(TypedDict, total=False):
    request_id: str
    session_id: str
    message: str
    ui_context: dict[str, Any] | None
    task: TaskSpec
    execution_plan: ExecutionPlan
    tool_results: list[ToolResult]
    evidence_pack: EvidencePack
    review_decision: ReviewDecision
    iteration_count: int
    errors: list[str]
    stages: list[str]
    final_response: dict[str, Any]
