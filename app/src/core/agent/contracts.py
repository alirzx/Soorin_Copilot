"""Canonical typed contracts for bounded Copilot investigations."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Annotated, Any, Literal, TypedDict
import operator

from src.core.graph.structured import StructuredQuerySpec
from src.core.identity import RequestIdentity


ToolStatus = Literal["ok", "empty", "not_configured", "unavailable", "invalid", "partial", "not_found"]
Completeness = Literal["complete", "partial", "unknown"]
ReviewOutcome = Literal["sufficient", "answer_with_limitations", "missing_required_evidence", "safe_failure"]
WorkflowMode = Literal["direct", "multi_step"]
TemporalMode = Literal["current", "historical", "mixed", "compare_previous_current"]
EvidenceMode = Literal["normal", "memory_only", "no_live_refresh", "current_verification", "verify_if_stale"]
ConversationOperation = Literal[
    "new_task",
    "follow_up",
    "memory_recall",
    "current_verification",
    "compare_previous_current",
    "memory_write",
    "topic_detach",
]
TurnTarget = Literal[
    "explicit_entity",
    "ui_entity",
    "active_entity",
    "active_pair",
    "conversation",
    "none",
]
EpisodeTransition = Literal["keep", "switch", "detach"]
ResponseDepth = Literal["brief", "standard", "deep", "report"]
StepRequirement = Literal["required", "optional"]
PlanSource = Literal["deterministic", "llm", "deterministic_fallback"]
WorkflowStatus = Literal[
    "running",
    "completed",
    "completed_with_limitations",
    "clarification_required",
    "partial_failure",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class EvidenceFact:
    source_capability: str
    statement: str
    value: Any
    entity: str | None = None
    fact_type: str = "observed"
    confidence: float | None = None


@dataclass(frozen=True)
class EvidenceReceipt:
    """Immutable acquisition record, deliberately separate from model context."""

    source_capability: str
    entities: tuple[str, ...]
    status: ToolStatus
    freshness: str
    completeness: Completeness
    retrieved_at: str
    source_payload_complete: bool
    projection_usable: bool
    truncated: bool = False
    projection_truncated: bool = False
    schema_version: str = "evidence-receipt-v1"
    payload_json: str = "{}"
    scope: str = "none"
    direction: str = "none"
    depth: int = 0

    @classmethod
    def from_payload(cls, *, payload: Any, **kwargs: Any) -> "EvidenceReceipt":
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return cls(payload_json=encoded, **kwargs)

    def payload(self) -> Any:
        return json.loads(self.payload_json)


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
    selected_views: tuple[str, ...] = ()
    detail: str = "standard"
    purpose: str = ""
    view_payload: Any = None
    payload_inventory: dict[str, Any] = field(default_factory=dict)
    included_paths: tuple[str, ...] = ()
    omitted_section_count: int = 0
    view_token_estimate: int = 0
    normalized_query_hash: str = ""
    context_identity: str = ""
    context_inclusion_reason: str | None = None
    context_representation: str = "unreviewed"
    context_token_estimate: int = 0
    context_token_cap: int = 0
    source_payload_complete: bool = False
    projection_usable: bool = False
    usable_fact_count: int = 0
    projection_truncated: bool = False
    projection_omitted_count: int = 0
    projection_schema_version: str = ""
    evidence_receipt: EvidenceReceipt | None = None


@dataclass(frozen=True)
class TaskEnvelope:
    """Immutable deterministic authority carried from resolution through planning.

    Semantic routing may select providers, but it must not change the resolved
    entity order, temporal authority, or an already-established comparison.
    """

    ordered_entities: tuple[str, ...] = ()
    reference_type: str = "none"
    operation: ConversationOperation = "new_task"
    temporal_scope: TemporalMode = "current"
    freshness_requirement: str = "current_when_available"
    allow_live: bool = True
    require_current: bool = False
    comparison_required: bool = False
    task_family: str = "general"
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.ordered_entities) > 2:
            raise ValueError("Task envelopes support at most two entities")
        if len(set(self.ordered_entities)) != len(self.ordered_entities):
            raise ValueError("Task envelope entities must be distinct")
        if self.comparison_required and len(self.ordered_entities) != 2:
            raise ValueError("Comparison envelopes require exactly two entities")


@dataclass(frozen=True)
class TaskSpec:
    request: str
    intent: str
    scope: str
    direction: str
    entities: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    optional_capabilities: tuple[str, ...] = ()
    structured_query: StructuredQuerySpec | None = None
    workflow_mode: WorkflowMode = "direct"
    semantic_decision_source: str = "unknown"
    requires_multiple_entities: bool = False
    recommended_steps: int = 1
    detail_level: str = "standard"
    freshness_requirement: str = "current_when_available"
    is_followup: bool = False
    graph_depth: int = 0
    relationship_mode: str = "none"
    temporal_mode: TemporalMode = "current"
    evidence_mode: EvidenceMode = "normal"
    response_depth: ResponseDepth = "standard"

    @property
    def max_steps(self) -> int:
        """Temporary read-only compatibility alias; plan limits live elsewhere."""
        return self.recommended_steps


@dataclass(frozen=True)
class RequestConstraints:
    """Deterministic request authority resolved before semantic routing."""

    allow_live: bool = True
    require_current: bool = False
    memory_only: bool = False
    memory_write: bool = False
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnPolicy:
    """Authoritative deterministic control decision for one conversation turn."""

    operation: ConversationOperation
    target: TurnTarget
    target_entities: tuple[str, ...] = ()
    requires_domain_router: bool = True
    episode_transition: EpisodeTransition = "keep"
    operational_state_mutation_allowed: bool = True
    reason_codes: tuple[str, ...] = ()


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
    result_coverage: dict[str, str] = field(default_factory=dict)
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
    caveats: tuple[str, ...] = ()
    material_limitations: tuple[str, ...] = ()


SpecialistStatus = Literal["completed", "completed_with_limitations", "skipped", "failed"]


@dataclass(frozen=True)
class AssetInvestigationResult:
    entities: tuple[str, ...]
    executed_capabilities: tuple[str, ...]
    result_statuses: tuple[tuple[str, str], ...]
    identity_role_fact_count: int = 0
    service_software_fact_count: int = 0
    risk_behavior_fact_count: int = 0
    role_consistency_indicators: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    freshness: tuple[str, ...] = ()
    truncated: bool = False
    status: SpecialistStatus = "skipped"


@dataclass(frozen=True)
class GraphAnalysisResult:
    entities: tuple[str, ...]
    executed_capabilities: tuple[str, ...]
    result_statuses: tuple[tuple[str, str], ...]
    scope: str = "none"
    direction: str = "none"
    depth: int = 0
    candidate_count: int = 0
    retrieved_count: int = 0
    complete_for_request: bool = False
    direct_relationship: bool | None = None
    missing_evidence: tuple[str, ...] = ()
    truncated: bool = False
    status: SpecialistStatus = "skipped"


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
    allowed_arguments: tuple[str, ...] = ()
    allowed_views: tuple[str, ...] = ()
    allowed_detail_levels: tuple[str, ...] = ()
    allowed_purposes: tuple[str, ...] = ()
    allowed_scopes: tuple[str, ...] = ()
    allowed_depths: tuple[int, ...] = ()
    dependencies: tuple[str, ...] = ()
    reusable_locally: bool = False
    parallelization: str = "independent_when_dependencies_allow"


class InvestigationState(TypedDict, total=False):
    request_identity: RequestIdentity
    request_id: str
    trace_id: str
    session_id: str
    message: str
    original_message: str
    ui_context: dict[str, Any] | None
    streaming: bool
    workflow_id: str
    thread_id: str
    started_at: str
    updated_at: str
    completed_at: str
    workflow_status: WorkflowStatus
    terminal: bool
    resumed: bool
    resolved_entities: Any
    active_entity_state: Any
    recent_messages: list[dict[str, str]]
    request_constraints: RequestConstraints
    turn_policy: TurnPolicy
    task_envelope: TaskEnvelope
    pending_working_facts: tuple[Any, ...]
    routing_result: Any
    plan_validation_result: dict[str, Any]
    capability_results: list[ToolResult]
    supplemental_retrieval_state: dict[str, Any]
    composed_context: str
    synthesizer_task_context: Any
    synthesizer_dynamic_prompt: str
    synthesizer_module_names: tuple[str, ...]
    model_messages: list[dict[str, str]]
    conversation_snapshot: Any
    memory_context_key: Any
    long_term_memory_selection: Any
    evidence_requirements: Any
    evidence_gap_plan: Any
    memory_tool_results: list[ToolResult]
    synthesis_request: dict[str, Any]
    context_review: dict[str, Any]
    synthesis_result: dict[str, Any]
    memory_update_result: dict[str, Any]
    retry_counters: dict[str, int]
    failure_metadata: dict[str, Any]
    limitation_reasons: list[str]
    next_edge: str
    clarification: dict[str, Any]
    node_records: Annotated[list[dict[str, Any]], operator.add]
    completed_nodes: Annotated[list[str], operator.add]
    task: TaskSpec
    execution_plan: ExecutionPlan
    tool_results: list[ToolResult]
    asset_specialist_result: AssetInvestigationResult
    graph_specialist_result: GraphAnalysisResult
    specialist_tool_results: list[ToolResult]
    generic_tool_results: list[ToolResult]
    specialist_records: list[dict[str, Any]]
    evidence_pack: EvidencePack
    review_decision: ReviewDecision
    supplemental_retrieval_count: int
    planner_called: bool
    fallback_used: bool
    routing_fallback_used: bool
    workflow_mode: WorkflowMode
    iteration_count: int
    errors: Annotated[list[str], operator.add]
    stages: Annotated[list[str], operator.add]
    final_response: dict[str, Any]
