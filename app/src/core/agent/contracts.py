"""Canonical typed contracts for bounded Copilot investigations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict


ToolStatus = Literal["ok", "empty", "not_configured", "unavailable", "invalid", "partial", "not_found"]
Completeness = Literal["complete", "partial", "unknown"]
ReviewOutcome = Literal["sufficient", "answer_with_limitations", "missing_required_evidence", "safe_failure"]
WorkflowMode = Literal["direct", "multi_step"]
StepRequirement = Literal["required", "optional"]
PlanSource = Literal["deterministic", "llm", "deterministic_fallback"]


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
    step_id: str = ""
    provider: str = ""
    valid_at: str | None = None
    latency_ms: int = 0
    cache_status: str = "unknown"
    retry_count: int = 0
    omitted_count: int | None = None
    safe_error_code: str | None = None
    evidence_type: str = "provider_evidence"
    citations: tuple[dict[str, Any], ...] = ()
    raw_payload: Any = None
    provider_result: Any = None


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
    detail_level: str = "standard"
    freshness_requirement: str = "current_when_available"
    is_followup: bool = False
    graph_depth: int = 0
    relationship_mode: str = "none"


@dataclass(frozen=True)
class PlanStep:
    id: str
    capability: str
    depends_on: tuple[str, ...] = ()
    status: str = "pending"
    arguments: dict[str, Any] = field(default_factory=dict)
    requirement: StepRequirement = "required"
    expected_evidence_type: str = "provider_evidence"

    @property
    def step_id(self) -> str:
        return self.id


@dataclass(frozen=True)
class ExecutionPlan:
    task: TaskSpec
    steps: tuple[PlanStep, ...]
    max_iterations: int = 1
    validated: bool = False
    plan_id: str = ""
    goal: str = ""
    target_entities: tuple[str, ...] = ()
    maximum_allowed_calls: int = 6
    planner_called: bool = False
    source: PlanSource = "deterministic"
    stop_condition: str = "required_evidence_collected"


@dataclass(frozen=True)
class EvidencePack:
    task: TaskSpec
    tool_results: tuple[ToolResult, ...]
    facts: tuple[EvidenceFact, ...]
    limitations: tuple[str, ...]
    contradictions: tuple[str, ...] = ()
    request_id: str = ""
    trace_id: str = ""
    plan_id: str = ""
    goal: str = ""
    resolved_entities: tuple[str, ...] = ()
    time_scope: str = "current_request"
    plan_summary: tuple[dict[str, Any], ...] = ()
    provider_coverage: dict[str, str] = field(default_factory=dict)
    graph_completeness: str = "not_requested"
    rag_citations: tuple[dict[str, Any], ...] = ()
    missing_evidence: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    supplemental_history: tuple[dict[str, Any], ...] = ()
    review_outcome: ReviewOutcome | None = None


@dataclass(frozen=True)
class ReviewDecision:
    outcome: ReviewOutcome
    reasons: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    supplemental_allowed: bool = False
    next_capability: str | None = None
    next_arguments: dict[str, Any] | None = None


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
    description: str = ""
    evidence_type: str = "provider_evidence"
    side_effect_class: str = "read_only"
    cache_policy: str = "provider_managed"
    freshness_policy: str = "point_in_time"
    maximum_graph_depth: int = 0
    maximum_result_scope: int | None = None
    required_permissions: tuple[str, ...] = ("read",)
    concurrency_group: str = "default"


class InvestigationState(TypedDict, total=False):
    request_id: str
    trace_id: str
    session_id: str
    message: str
    ui_context: dict[str, Any] | None
    task: TaskSpec
    execution_plan: ExecutionPlan
    tool_results: list[ToolResult]
    evidence_pack: EvidencePack
    review_decision: ReviewDecision
    supplemental_retrieval_count: int
    planner_called: bool
    fallback_used: bool
    workflow_mode: WorkflowMode
    iteration_count: int
    errors: list[str]
    stages: list[str]
    final_response: dict[str, Any]
