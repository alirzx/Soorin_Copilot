"""Deterministic and model-backed replay evaluation for adaptive orchestration.

The evaluator deliberately has no capability executor or memory repository. It
replays synthetic ``ToolResult`` objects only after the production action and
plan validators accept a canonical action fingerprint.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import ipaddress
import json
from pathlib import Path
import statistics
import subprocess
import time
from typing import Any, Iterable, Literal, Mapping, Sequence

from src.config.settings import Settings, get_settings
from src.core.agent.action_validator import AgentActionValidationError, AgentActionValidator
from src.core.agent.agent_loop import build_observation, evaluate_progress, update_ledger
from src.core.agent.context_identity import canonical_action_fingerprint
from src.core.agent.contracts import (
    AgentCapabilityRequest,
    AgentClarifyDecision,
    AgentContinueDecision,
    AgentFinishDecision,
    AgentLoopBudget,
    EvidenceFact,
    EvidenceGap,
    EvidenceLedger,
    PlanStep,
    PostSearchRequirements,
    RequestConstraints,
    StructuredAssetAggregateEvidence,
    StructuredAssetSearchEvidence,
    TaskEnvelope,
    TaskSpec,
    ToolResult,
)
from src.core.agent.investigator import Investigator, InvestigatorError
from src.core.agent.investigator_context import InvestigatorContextBuilder, InvestigatorContextError
from src.core.agent.plan_validator import PlanValidator
from src.core.agent.registry import build_capability_registry
from src.core.agent.task_mapping import select_orchestration_mode
from src.core.graph.structured import StructuredQuerySpec, structured_query_identity


EVALUATION_SCHEMA_VERSION = "adaptive-agent-evaluation-v1"
DEFAULT_CORPUS_VERSION = "adaptive-agent-eval-v1"
ReadinessState = Literal[
    "NOT_READY",
    "READY_FOR_MODEL_REPLAY",
    "READY_FOR_STAGING",
    "READY_FOR_CANARY",
    "READY_FOR_DEFAULT",
]

_RFC5737_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
_AUTHORITY_CODES = frozenset({
    "unknown_capability",
    "capability_not_authorized",
    "capability_not_authorized_for_gap",
    "entity_authority_violation",
    "evidence_gap_entity_scope_violation",
    "live_capability_forbidden_by_request",
    "graph_depth_authority_violation",
    "graph_direction_authority_violation",
    "graph_scope_authority_violation",
    "structured_query_authority_violation",
})
_BUDGET_STOPS = frozenset({
    "budget_exhausted", "tool_budget_exhausted", "llm_budget_exhausted",
    "context_budget_exhausted", "technical_failure_ceiling",
})
_SCRIPTED_INVESTIGATOR_ERRORS = frozenset({
    "investigator_length_exhausted",
    "investigator_technical_failure",
})


class AdaptiveEvaluationError(ValueError):
    """A tracked evaluation fixture or policy file is invalid."""


@dataclass(frozen=True)
class AdaptiveEvalExpectation:
    expected_mode: str
    acceptable_stop_reasons: tuple[str, ...] = ()
    final_gap_states: tuple[tuple[str, str], ...] = ()
    required_capabilities: tuple[str, ...] = ()
    useful_capabilities: tuple[str, ...] = ()
    forbidden_capabilities: tuple[str, ...] = ()
    expected_rejections: tuple[str, ...] = ()
    unavoidable_gap_ids: tuple[str, ...] = ()
    max_investigator_turns: int = 4
    max_capability_calls: int = 6
    expected_clarification: bool = False
    expected_limitation: bool = False
    normal_solvable: bool = True
    minimum_equivalence_suppressions: int = 0
    forbidden_context_markers: tuple[str, ...] = ()
    baseline: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class AdaptiveEvalScenario:
    scenario_id: str
    schema_version: str
    corpus_version: str
    categories: tuple[str, ...]
    task: TaskSpec
    constraints: RequestConstraints
    envelope: TaskEnvelope
    gaps: tuple[EvidenceGap, ...]
    decisions: tuple[Any, ...]
    results: tuple[dict[str, Any], ...]
    initial_results: tuple[dict[str, Any], ...]
    expectation: AdaptiveEvalExpectation
    evaluation_modes: tuple[str, ...] = ("replay", "model-replay")
    router_calls: int = 1
    budget_overrides: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class AdaptiveEvalResult:
    scenario_id: str
    categories: tuple[str, ...]
    success: bool
    failure_reasons: tuple[str, ...]
    selected_mode: str
    stop_reason: str
    final_gap_states: tuple[tuple[str, str], ...]
    investigator_turns: int
    llm_calls: int
    capability_calls: int
    executed_capabilities: tuple[str, ...]
    rejected_reasons: tuple[str, ...]
    decision_kinds: tuple[str, ...]
    tool_true_positives: int
    unnecessary_tool_calls: int
    expected_tool_coverage_hits: int
    expected_tool_coverage_total: int
    duplicate_provider_calls: int
    equivalence_suppressions: int
    authority_violations_proposed: int
    authority_violations_accepted: int
    premature_finish_proposed: int
    premature_finish_accepted: int
    clarification_correct: bool | None
    required_gap_total: int
    required_gap_satisfied: int
    required_gap_failed: int
    context_input_tokens_before: tuple[int, ...]
    context_input_tokens: tuple[int, ...]
    provider_input_tokens: tuple[int, ...]
    context_compaction_savings: int
    evidence_reference_counts: tuple[int, ...]
    delta_counts: tuple[int, ...]
    capability_schema_counts: tuple[int, ...]
    context_hard_limit_violations: int
    investigator_output_tokens: tuple[int, ...]
    model_latency_ms: int
    review_outcome: str
    normal_solvable: bool = True
    live_tool_calls: int = 0
    memory_writes: int = 0
    raw_context_leaks: int = 0
    graph_authority_bypasses: int = 0
    structured_mutations_accepted: int = 0


@dataclass(frozen=True)
class AdaptiveEvalReport:
    schema_version: str
    commit_sha: str
    evaluation_mode: str
    status: str
    model: str
    deployment: str
    deterministic_sampling_supported: bool | None
    prompt_sha256: str
    corpus_version: str
    threshold_version: str
    scenario_count: int
    passed_count: int
    failed_count: int
    metrics: dict[str, Any]
    category_metrics: dict[str, dict[str, float]]
    baseline_comparison: dict[str, float]
    hard_gate_results: dict[str, bool]
    quality_gate_results: dict[str, bool]
    failed_scenarios: tuple[dict[str, Any], ...]
    readiness: ReadinessState
    results: tuple[AdaptiveEvalResult, ...] = field(repr=False)

    def to_dict(self, *, include_results: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        if not include_results:
            payload.pop("results", None)
        return payload

    def to_json(self, *, include_results: bool = True) -> str:
        return json.dumps(
            self.to_dict(include_results=include_results),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


class _ReplayEnvironment:
    """Resolve fixtures only through fingerprints produced by validation."""

    def __init__(self, scenario: AdaptiveEvalScenario) -> None:
        self.scenario = scenario
        self._remaining = [dict(item) for item in scenario.results]
        self.provider_fingerprints: list[str] = []

    def result_for(
        self,
        step: PlanStep,
        fingerprint: str,
        *,
        task: TaskSpec,
    ) -> ToolResult:
        match_index = self._find_fixture(step)
        if match_index is None:
            raise AdaptiveEvaluationError(
                f"{self.scenario.scenario_id}: no replay result for validated {step.capability} action"
            )
        fixture = self._remaining.pop(match_index)
        result = _tool_result_from_fixture(
            fixture,
            step=step,
            task=task,
        )
        active_graph_version = str(
            getattr(result.structured_asset_set, "active_graph_version", "")
            or result.raw_payload.get("active_graph_version")
            or ""
        )
        provider_identity = (
            f"{fingerprint}:graph-version={active_graph_version}"
            if step.capability.startswith("graph.") and active_graph_version
            else fingerprint
        )
        self.provider_fingerprints.append(provider_identity)
        return result

    def _find_fixture(self, step: PlanStep) -> int | None:
        entities = tuple(str(item) for item in step.arguments.get("entities", ()))
        for index, fixture in enumerate(self._remaining):
            if fixture.get("capability") != step.capability:
                continue
            expected_entities = tuple(str(item) for item in fixture.get("entities", ()))
            if expected_entities and expected_entities != entities:
                continue
            subset = fixture.get("arguments_subset") or {}
            if any(step.arguments.get(key) != value for key, value in subset.items()):
                continue
            return index
        return None


class _RecordingInvestigator:
    def __init__(self, investigator: Investigator) -> None:
        self.investigator = investigator
        self.input_tokens: list[int] = []
        self.output_tokens: list[int] = []
        self.latency_ms = 0

    def decide(self, context_json: str, *, request_id: str, trace_id: str) -> Any:
        usage_recorder = getattr(self.investigator.llm_client, "usage_recorder", None)
        before = (
            len(usage_recorder.snapshot())
            if callable(getattr(usage_recorder, "snapshot", None))
            else 0
        )
        started = time.perf_counter()
        try:
            return self.investigator.decide(
                context_json,
                request_id=request_id,
                trace_id=trace_id,
            )
        finally:
            observed_latency = int((time.perf_counter() - started) * 1000)
            if callable(getattr(usage_recorder, "snapshot", None)):
                calls = usage_recorder.snapshot()[before:]
                if calls:
                    self.input_tokens.extend(
                        int(getattr(call, "input_tokens", 0)) for call in calls
                    )
                    self.output_tokens.extend(
                        int(getattr(call, "output_tokens", 0)) for call in calls
                    )
                    observed_latency = sum(
                        int(getattr(call, "latency_ms", 0)) for call in calls
                    ) or observed_latency
            self.latency_ms += observed_latency


def load_scenarios(path: str | Path) -> tuple[AdaptiveEvalScenario, ...]:
    source = Path(path)
    scenarios: list[AdaptiveEvalScenario] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdaptiveEvaluationError(f"{source}:{line_number}: invalid JSON") from exc
        scenarios.append(_scenario_from_payload(payload, source=source, line_number=line_number))
    if not scenarios:
        raise AdaptiveEvaluationError(f"{source}: scenario corpus is empty")
    identifiers = [item.scenario_id for item in scenarios]
    if len(identifiers) != len(set(identifiers)):
        raise AdaptiveEvaluationError(f"{source}: scenario IDs must be unique")
    versions = {item.corpus_version for item in scenarios}
    if len(versions) != 1:
        raise AdaptiveEvaluationError(f"{source}: every scenario must use one corpus version")
    return tuple(scenarios)


def load_thresholds(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "adaptive-agent-thresholds-v1":
        raise AdaptiveEvaluationError("Unsupported adaptive evaluation threshold schema")
    if not isinstance(payload.get("quality_gates"), dict):
        raise AdaptiveEvaluationError("Threshold policy is missing quality_gates")
    return payload


def run_evaluation(
    scenarios: Sequence[AdaptiveEvalScenario],
    thresholds: Mapping[str, Any],
    *,
    mode: Literal["replay", "model-replay"] = "replay",
    investigator: Investigator | None = None,
    settings: Settings | None = None,
) -> AdaptiveEvalReport:
    settings = settings or get_settings()
    scenarios = tuple(item for item in scenarios if mode in item.evaluation_modes)
    if mode == "model-replay" and investigator is None:
        return _unavailable_report(scenarios, thresholds, settings)
    registry = _evaluation_registry(settings)
    plan_validator = PlanValidator(
        registry,
        max_calls=settings.agent_max_capability_calls,
        max_entities=settings.agent_max_entities,
        max_graph_depth=settings.agent_max_graph_depth,
    )
    recorder = _RecordingInvestigator(investigator) if investigator is not None else None
    results = tuple(
        _run_scenario(
            scenario,
            mode=mode,
            settings=settings,
            registry=registry,
            plan_validator=plan_validator,
            investigator=recorder,
        )
        for scenario in scenarios
    )
    metrics = aggregate_metrics(results)
    hard_gate_results = _hard_gate_results(results)
    quality_gate_results = _quality_gate_results(metrics, thresholds)
    readiness = calculate_readiness(
        mode=mode,
        status="completed",
        hard_gate_results=hard_gate_results,
        quality_gate_results=quality_gate_results,
    )
    failed = tuple(
        {"scenario_id": item.scenario_id, "reasons": list(item.failure_reasons)}
        for item in results
        if not item.success
    )
    deployment = settings.deployment_for_purpose("investigator")
    return AdaptiveEvalReport(
        schema_version=EVALUATION_SCHEMA_VERSION,
        commit_sha=_commit_sha(),
        evaluation_mode=mode,
        status="completed",
        model=deployment.model if mode == "model-replay" else "",
        deployment=deployment.name if mode == "model-replay" else "",
        deterministic_sampling_supported=(
            bool(deployment.supports_temperature and deployment.supports_top_p)
            if mode == "model-replay" else None
        ),
        prompt_sha256=_prompt_hash(settings.investigator_system_prompt_path),
        corpus_version=scenarios[0].corpus_version if scenarios else DEFAULT_CORPUS_VERSION,
        threshold_version=str(thresholds.get("threshold_version") or "unknown"),
        scenario_count=len(results),
        passed_count=sum(item.success for item in results),
        failed_count=sum(not item.success for item in results),
        metrics=metrics,
        category_metrics=_category_metrics(results),
        baseline_comparison=_baseline_comparison(scenarios, results),
        hard_gate_results=hard_gate_results,
        quality_gate_results=quality_gate_results,
        failed_scenarios=failed,
        readiness=readiness,
        results=results,
    )


def calculate_readiness(
    *,
    mode: str,
    status: str,
    hard_gate_results: Mapping[str, bool],
    quality_gate_results: Mapping[str, bool],
) -> ReadinessState:
    """Single authoritative readiness calculation shared by CLI, CI, and tests."""
    if status != "completed" or not all(hard_gate_results.values()) or not all(quality_gate_results.values()):
        return "NOT_READY"
    if mode == "model-replay":
        return "READY_FOR_STAGING"
    return "READY_FOR_MODEL_REPLAY"


def aggregate_metrics(results: Sequence[AdaptiveEvalResult]) -> dict[str, Any]:
    count = len(results)
    required_total = sum(item.required_gap_total for item in results)
    required_satisfied = sum(item.required_gap_satisfied for item in results)
    required_failed = sum(item.required_gap_failed for item in results)
    true_positive = sum(item.tool_true_positives for item in results)
    unnecessary = sum(item.unnecessary_tool_calls for item in results)
    executed = true_positive + unnecessary
    coverage_hits = sum(item.expected_tool_coverage_hits for item in results)
    coverage_total = sum(item.expected_tool_coverage_total for item in results)
    capability_calls = [item.capability_calls for item in results]
    turns = [item.investigator_turns for item in results]
    estimated_input_tokens_before = [
        value for item in results for value in item.context_input_tokens_before
    ]
    estimated_input_tokens = [value for item in results for value in item.context_input_tokens]
    provider_input_tokens = [value for item in results for value in item.provider_input_tokens]
    input_tokens = provider_input_tokens or estimated_input_tokens
    output_tokens = [value for item in results for value in item.investigator_output_tokens]
    model_latencies = [item.model_latency_ms for item in results if item.model_latency_ms > 0]
    clarification_cases = [item for item in results if item.clarification_correct is not None]
    normal_solvable = [
        item for item in results
        if item.selected_mode == "adaptive" and item.required_gap_total and item.normal_solvable
    ]
    review_distribution: dict[str, int] = {}
    for item in results:
        review_distribution[item.review_outcome] = review_distribution.get(item.review_outcome, 0) + 1
    return {
        "scenario_success_rate": _rate(sum(item.success for item in results), count),
        "required_gap_completion_rate": _rate(required_satisfied, required_total),
        "required_gap_failure_rate": _rate(required_failed, required_total),
        "tool_selection_precision": _rate(true_positive, executed),
        "tool_selection_coverage": _rate(coverage_hits, coverage_total),
        "unnecessary_tool_call_rate": _rate(unnecessary, executed),
        "duplicate_provider_call_rate": _rate(sum(item.duplicate_provider_calls for item in results), max(1, executed)),
        "equivalent_action_suppression_rate": _rate(
            sum(item.equivalence_suppressions for item in results),
            sum(item.equivalence_suppressions for item in results)
            + sum(item.duplicate_provider_calls for item in results),
        ),
        "authority_violation_proposed_rate": _rate(
            sum(item.authority_violations_proposed for item in results),
            sum(item.investigator_turns for item in results),
        ),
        "authority_violation_accepted_rate": _rate(
            sum(item.authority_violations_accepted for item in results),
            max(1, sum(item.authority_violations_proposed for item in results)),
        ),
        "premature_finish_proposed_rate": _rate(
            sum(item.premature_finish_proposed for item in results),
            sum(item.investigator_turns for item in results),
        ),
        "premature_finish_accepted_rate": _rate(
            sum(item.premature_finish_accepted for item in results),
            max(1, sum(item.premature_finish_proposed for item in results)),
        ),
        "clarification_accuracy": _rate(
            sum(item.clarification_correct is True for item in clarification_cases),
            len(clarification_cases),
        ),
        "solvable_no_useful_action_rate": _rate(
            sum(item.stop_reason == "no_useful_action" for item in normal_solvable),
            len(normal_solvable),
        ),
        "normal_scenario_budget_exhaustion_rate": _rate(
            sum(item.stop_reason in _BUDGET_STOPS for item in normal_solvable),
            len(normal_solvable),
        ),
        "max_turn_exhaustion_rate": _rate(
            sum(item.stop_reason == "budget_exhausted" for item in results), count,
        ),
        "technical_failure_rate": _rate(
            sum(item.stop_reason == "technical_failure_ceiling" for item in results), count,
        ),
        "average_investigator_turns": _average(turns),
        "p95_investigator_turns": _percentile95(turns),
        "average_capability_calls": _average(capability_calls),
        "p95_capability_calls": _percentile95(capability_calls),
        "average_investigator_input_tokens": _average(input_tokens),
        "p95_investigator_input_tokens": _percentile95(input_tokens),
        "max_investigator_input_tokens": max(input_tokens, default=0),
        "investigator_input_token_source": (
            "provider" if provider_input_tokens else "estimated_context"
        ),
        "average_investigator_input_tokens_before_compaction": _average(
            estimated_input_tokens_before
        ),
        "average_estimated_context_input_tokens": _average(estimated_input_tokens),
        "average_investigator_output_tokens": _average(output_tokens) if output_tokens else None,
        "context_compaction_savings": sum(item.context_compaction_savings for item in results),
        "average_evidence_reference_count": _average([
            value for item in results for value in item.evidence_reference_counts
        ]),
        "average_delta_count": _average([
            value for item in results for value in item.delta_counts
        ]),
        "average_capability_schema_count": _average([
            value for item in results for value in item.capability_schema_counts
        ]),
        "average_model_latency_ms": _average(model_latencies) if model_latencies else None,
        "review_outcome_distribution": review_distribution,
        "direct_adaptive_entry_rate": _rate(
            sum(
                item.selected_mode == "adaptive"
                for item in results
                if "direct" in item.categories
            ),
            sum("direct" in item.categories for item in results),
        ),
    }


def _run_scenario(
    scenario: AdaptiveEvalScenario,
    *,
    mode: str,
    settings: Settings,
    registry: Any,
    plan_validator: PlanValidator,
    investigator: _RecordingInvestigator | None,
) -> AdaptiveEvalResult:
    expected = scenario.expectation
    selected_mode = select_orchestration_mode(
        scenario.task,
        constraints=scenario.constraints,
        adaptive_enabled=True,
        planner_selected=scenario.task.workflow_mode == "multi_step",
    )
    if selected_mode != "adaptive":
        success = selected_mode == expected.expected_mode
        return AdaptiveEvalResult(
            scenario_id=scenario.scenario_id,
            categories=scenario.categories,
            success=success,
            failure_reasons=() if success else ("orchestration_mode_mismatch",),
            selected_mode=selected_mode,
            stop_reason="direct_completion",
            final_gap_states=tuple((gap.gap_id, gap.status) for gap in scenario.gaps),
            investigator_turns=0,
            llm_calls=scenario.router_calls,
            capability_calls=0,
            executed_capabilities=(),
            rejected_reasons=(),
            decision_kinds=(),
            tool_true_positives=0,
            unnecessary_tool_calls=0,
            expected_tool_coverage_hits=0,
            expected_tool_coverage_total=0,
            duplicate_provider_calls=0,
            equivalence_suppressions=0,
            authority_violations_proposed=0,
            authority_violations_accepted=0,
            premature_finish_proposed=0,
            premature_finish_accepted=0,
            clarification_correct=(not expected.expected_clarification),
            required_gap_total=0,
            required_gap_satisfied=0,
            required_gap_failed=0,
            context_input_tokens_before=(),
            context_input_tokens=(),
            provider_input_tokens=(),
            context_compaction_savings=0,
            evidence_reference_counts=(),
            delta_counts=(),
            capability_schema_counts=(),
            context_hard_limit_violations=0,
            investigator_output_tokens=(),
            model_latency_ms=0,
            review_outcome="sufficient",
            normal_solvable=expected.normal_solvable,
        )

    eval_settings = settings
    overrides = dict(scenario.budget_overrides)
    if "investigator_max_input_tokens" in overrides or "investigator_hard_input_tokens" in overrides:
        eval_settings = replace(
            settings,
            investigator_max_input_tokens=overrides.get(
                "investigator_max_input_tokens", settings.investigator_max_input_tokens
            ),
            investigator_hard_input_tokens=overrides.get(
                "investigator_hard_input_tokens", settings.investigator_hard_input_tokens
            ),
        )
    budget = AgentLoopBudget(
        max_investigator_turns=overrides.get("max_investigator_turns", settings.agent_max_investigator_turns),
        max_llm_calls=overrides.get("max_llm_calls", settings.agent_max_llm_calls),
        max_capabilities_per_decision=2,
        max_total_capability_calls=overrides.get(
            "max_total_capability_calls", settings.agent_max_total_capability_calls
        ),
        max_deepened_entities=settings.agent_max_deepened_entities,
        max_graph_depth=settings.agent_max_graph_depth,
        max_technical_failures=overrides.get(
            "max_technical_failures", settings.agent_max_technical_failures
        ),
        deadline_monotonic=time.monotonic() + 300,
        llm_calls=scenario.router_calls,
    )
    ledger = EvidenceLedger(
        authorized_entities=scenario.task.entities,
        gaps=scenario.gaps,
    )
    if scenario.initial_results:
        initial_steps = tuple(
            PlanStep(
                id=f"initial-{index}",
                capability=str(item["capability"]),
                arguments=dict(item.get("action_arguments") or {}),
            )
            for index, item in enumerate(scenario.initial_results, start=1)
        )
        initial = tuple(
            _tool_result_from_fixture(item, step=step, task=scenario.task)
            for item, step in zip(scenario.initial_results, initial_steps)
        )
        ledger = update_ledger(
            ledger,
            scenario.task,
            initial,
            action_fingerprints=tuple(canonical_action_fingerprint(step) for step in initial_steps),
            action_steps=initial_steps,
        )
    environment = _ReplayEnvironment(scenario)
    action_validator = AgentActionValidator(registry, plan_validator)
    executed: list[str] = []
    rejected: list[str] = []
    decision_kinds: list[str] = []
    context_tokens_before: list[int] = []
    context_tokens: list[int] = []
    evidence_reference_counts: list[int] = []
    delta_counts: list[int] = []
    capability_schema_counts: list[int] = []
    context_hard_limit_violations = 0
    compaction_savings = 0
    authority_proposed = authority_accepted = 0
    premature_proposed = premature_accepted = 0
    equivalence_suppressions = 0
    unnecessary = true_positive = 0
    clarification_actual = False
    raw_context_leaks = 0
    structured_mutations_accepted = 0
    turn = 0
    no_progress = 0
    stop_reason = ""
    scripted_index = 0
    model_latency_start = investigator.latency_ms if investigator is not None else 0
    model_input_start = len(investigator.input_tokens) if investigator is not None else 0
    model_output_start = len(investigator.output_tokens) if investigator is not None else 0

    while True:
        stop = evaluate_progress(ledger, budget, consecutive_no_progress=no_progress)
        if stop is not None:
            stop_reason = stop
            break
        turn += 1
        try:
            context = InvestigatorContextBuilder(eval_settings, registry).build(
                task=scenario.task,
                constraints=scenario.constraints,
                envelope=scenario.envelope,
                ledger=ledger,
                latest_observation=None,
                budget=budget,
                turn=turn,
                system_prompt=_prompt_text(settings.investigator_system_prompt_path),
            )
        except InvestigatorContextError:
            stop_reason = "context_budget_exhausted"
            break
        context_tokens_before.append(context.input_tokens_before)
        context_tokens.append(context.input_tokens_after)
        evidence_reference_counts.append(context.evidence_reference_count)
        delta_counts.append(context.delta_count)
        capability_schema_counts.append(context.capability_schema_count)
        context_hard_limit_violations += int(
            context.input_tokens_after > eval_settings.investigator_hard_input_tokens
        )
        compaction_savings += context.compacted_tokens
        raw_context_leaks += sum(
            marker in context.context_json for marker in expected.forbidden_context_markers
        )
        budget = replace(
            budget,
            investigator_turns=turn,
            llm_calls=budget.llm_calls + 1,
        )
        try:
            if mode == "model-replay":
                if investigator is None:
                    raise AdaptiveEvaluationError("model replay requires an Investigator")
                decision = investigator.decide(
                    context.context_json,
                    request_id=f"eval-{scenario.scenario_id}-{turn}",
                    trace_id="adaptive-eval",
                )
            else:
                if scripted_index >= len(scenario.decisions):
                    stop_reason = "no_useful_action"
                    break
                scripted = scenario.decisions[scripted_index]
                scripted_index += 1
                if isinstance(scripted, dict) and scripted.get("error"):
                    error_code = str(scripted["error"])
                    if error_code not in _SCRIPTED_INVESTIGATOR_ERRORS:
                        raise AdaptiveEvaluationError(
                            f"{scenario.scenario_id}: unsupported scripted Investigator error"
                        )
                    raise InvestigatorError(error_code, "Synthetic Investigator failure")
                raw = scripted.get("raw") if isinstance(scripted, dict) and "raw" in scripted else json.dumps(scripted)
                decision = Investigator.parse(str(raw))
        except InvestigatorError as exc:
            rejected.append(exc.code)
            budget = replace(budget, technical_failures=budget.technical_failures + 1)
            continue

        decision_kinds.append(decision.kind)
        if isinstance(decision, AgentClarifyDecision):
            clarification_actual = True
            stop_reason = "clarification_required"
            break
        if isinstance(decision, AgentFinishDecision):
            open_required = tuple(
                gap for gap in ledger.gaps
                if gap.importance == "required" and gap.status == "open"
            )
            deterministic_stop = evaluate_progress(ledger, budget, consecutive_no_progress=no_progress)
            if open_required and deterministic_stop is None:
                premature_proposed += 1
                rejected.append("finish_with_obtainable_required_gap")
                budget = replace(budget, technical_failures=budget.technical_failures + 1)
                continue
            if open_required:
                premature_proposed += 1
                premature_accepted += 1
            stop_reason = deterministic_stop or decision.stop_reason
            break
        if not isinstance(decision, AgentContinueDecision):
            rejected.append("investigator_decision_type_invalid")
            no_progress += 1
            continue

        try:
            validated = action_validator.validate(
                decision,
                task=scenario.task,
                constraints=scenario.constraints,
                ledger=ledger,
                budget=budget,
                turn=turn,
            )
        except AgentActionValidationError as exc:
            rejected.append(exc.code)
            if exc.code in _AUTHORITY_CODES:
                authority_proposed += 1
            if exc.code == "structured_query_authority_violation":
                structured_mutations_accepted += 0
            if exc.code in {"equivalent_evidence_already_available", "repeated_failed_action"}:
                equivalence_suppressions += 1
                if exc.code == "equivalent_evidence_already_available":
                    ledger = _reuse_reference(ledger, exc.evidence_reference_id, exc.evidence_gap_id)
                    no_progress = 0
                    continue
            budget = replace(budget, technical_failures=budget.technical_failures + 1)
            no_progress += 1
            continue

        previous = ledger
        replayed: list[ToolResult] = []
        try:
            for step, fingerprint in zip(validated.plan.steps, validated.fingerprints):
                result = environment.result_for(step, fingerprint, task=scenario.task)
                replayed.append(result)
        except AdaptiveEvaluationError:
            rejected.append("replay_fixture_unavailable")
            budget = replace(budget, technical_failures=budget.technical_failures + 1)
            no_progress += 1
            continue
        for step in validated.plan.steps:
            executed.append(step.capability)
            useful = set(expected.useful_capabilities or expected.required_capabilities)
            if not useful or step.capability in useful:
                true_positive += 1
            else:
                unnecessary += 1
        budget = replace(
            budget,
            capability_calls=budget.capability_calls + len(validated.plan.steps),
            deepened_entities=tuple(dict.fromkeys(
                (*budget.deepened_entities, *validated.deepened_entities)
            )),
        )
        ledger = update_ledger(
            ledger,
            scenario.task,
            tuple(replayed),
            action_fingerprints=validated.fingerprints,
            action_steps=tuple(validated.plan.steps),
        )
        observation = build_observation(
            turn=turn,
            requests=decision.capability_requests,
            results=tuple(replayed),
            previous=previous,
            current=ledger,
            context_input_tokens_before=context.input_tokens_before,
            context_input_tokens_after=context.input_tokens_after,
        )
        no_progress = 0 if observation.material_progress else no_progress + 1

    final_states = tuple((gap.gap_id, gap.status) for gap in ledger.gaps)
    final_state_map = dict(final_states)
    required = tuple(gap for gap in ledger.gaps if gap.importance == "required")
    obtainable = tuple(gap for gap in required if gap.gap_id not in expected.unavoidable_gap_ids)
    required_satisfied = sum(gap.status == "satisfied" for gap in obtainable)
    required_failed = sum(gap.status != "satisfied" for gap in obtainable)
    expected_required = set(expected.required_capabilities)
    coverage_hits = len(expected_required.intersection(executed))
    duplicate_calls = len(environment.provider_fingerprints) - len(set(environment.provider_fingerprints))
    graph_references = tuple(
        ref for ref in ledger.evidence_references
        if ref.source_capability.startswith("graph.") and ref.active_graph_version
    )
    graph_versions = {ref.active_graph_version for ref in graph_references}
    latest_graph_version = graph_references[-1].active_graph_version if graph_references else ""
    graph_bypasses = sum(
        ref.reusable and ref.active_graph_version != latest_graph_version
        for ref in graph_references
        if len(graph_versions) > 1
    )
    review_outcome = (
        "answer_with_limitations"
        if any(
            gap.gap_id in expected.unavoidable_gap_ids and gap.status != "satisfied"
            for gap in required
        )
        else "sufficient"
        if not required or all(gap.status == "satisfied" for gap in obtainable)
        else "missing_required_evidence"
    )
    failure_reasons: list[str] = []
    if selected_mode != expected.expected_mode:
        failure_reasons.append("orchestration_mode_mismatch")
    if expected.acceptable_stop_reasons and stop_reason not in expected.acceptable_stop_reasons:
        failure_reasons.append("stop_reason_unexpected")
    for gap_id, status in expected.final_gap_states:
        if final_state_map.get(gap_id) != status:
            failure_reasons.append(f"gap_state:{gap_id}")
    if turn > expected.max_investigator_turns:
        failure_reasons.append("investigator_turn_limit")
    if budget.capability_calls > expected.max_capability_calls:
        failure_reasons.append("capability_call_limit")
    if any(item in expected.forbidden_capabilities for item in executed):
        failure_reasons.append("forbidden_capability_executed")
    if expected_required - set(executed) and not expected.expected_limitation:
        failure_reasons.append("required_capability_not_executed")
    if mode == "replay" and not set(expected.expected_rejections).issubset(rejected):
        failure_reasons.append("expected_rejection_missing")
    if equivalence_suppressions < expected.minimum_equivalence_suppressions:
        failure_reasons.append("equivalence_not_suppressed")
    if clarification_actual != expected.expected_clarification:
        failure_reasons.append("clarification_mismatch")
    if required_failed and not expected.expected_limitation:
        failure_reasons.append("required_gap_incomplete")
    if raw_context_leaks:
        failure_reasons.append("raw_context_leak")
    if "replay_fixture_unavailable" in rejected:
        failure_reasons.append("replay_fixture_unavailable")

    final_synth_calls = 0 if stop_reason == "clarification_required" else 1

    return AdaptiveEvalResult(
        scenario_id=scenario.scenario_id,
        categories=scenario.categories,
        success=not failure_reasons,
        failure_reasons=tuple(dict.fromkeys(failure_reasons)),
        selected_mode=selected_mode,
        stop_reason=stop_reason,
        final_gap_states=final_states,
        investigator_turns=turn,
        llm_calls=budget.llm_calls + final_synth_calls,
        capability_calls=budget.capability_calls,
        executed_capabilities=tuple(executed),
        rejected_reasons=tuple(rejected),
        decision_kinds=tuple(decision_kinds),
        tool_true_positives=true_positive,
        unnecessary_tool_calls=unnecessary,
        expected_tool_coverage_hits=coverage_hits,
        expected_tool_coverage_total=len(expected_required),
        duplicate_provider_calls=max(0, duplicate_calls),
        equivalence_suppressions=equivalence_suppressions,
        authority_violations_proposed=authority_proposed,
        authority_violations_accepted=authority_accepted,
        premature_finish_proposed=premature_proposed,
        premature_finish_accepted=premature_accepted,
        clarification_correct=(clarification_actual == expected.expected_clarification),
        required_gap_total=len(obtainable),
        required_gap_satisfied=required_satisfied,
        required_gap_failed=required_failed,
        context_input_tokens_before=tuple(context_tokens_before),
        context_input_tokens=tuple(context_tokens),
        provider_input_tokens=tuple(
            investigator.input_tokens[model_input_start:] if investigator else ()
        ),
        context_compaction_savings=compaction_savings,
        evidence_reference_counts=tuple(evidence_reference_counts),
        delta_counts=tuple(delta_counts),
        capability_schema_counts=tuple(capability_schema_counts),
        context_hard_limit_violations=context_hard_limit_violations,
        investigator_output_tokens=tuple(
            investigator.output_tokens[model_output_start:] if investigator else ()
        ),
        model_latency_ms=(investigator.latency_ms - model_latency_start if investigator else 0),
        review_outcome=review_outcome,
        normal_solvable=expected.normal_solvable,
        raw_context_leaks=raw_context_leaks,
        graph_authority_bypasses=graph_bypasses,
        structured_mutations_accepted=structured_mutations_accepted,
    )


def _scenario_from_payload(payload: Mapping[str, Any], *, source: Path, line_number: int) -> AdaptiveEvalScenario:
    required = {"scenario_id", "schema_version", "corpus_version", "categories", "task", "expectation"}
    missing = sorted(required - set(payload))
    if missing:
        raise AdaptiveEvaluationError(f"{source}:{line_number}: missing fields {', '.join(missing)}")
    if payload["schema_version"] != "adaptive-agent-scenario-v1":
        raise AdaptiveEvaluationError(f"{source}:{line_number}: unsupported scenario schema")
    _validate_synthetic_addresses(payload, source=source, line_number=line_number)
    task_payload = dict(payload["task"])
    task_payload.setdefault("intent", "asset_investigation")
    task_payload.setdefault("scope", "node_summary")
    task_payload.setdefault("direction", "both")
    task_payload.setdefault("entities", ())
    task_payload.setdefault("required_capabilities", ())
    task_payload.setdefault("graph_depth", 0)
    task_payload.setdefault("relationship_mode", "none")
    task_payload.setdefault("workflow_mode", "multi_step")
    task_payload.setdefault("orchestration_mode", "adaptive")
    if task_payload.get("structured_query") is not None:
        task_payload["structured_query"] = StructuredQuerySpec.model_validate(task_payload["structured_query"])
    if task_payload.get("post_search_requirements") is not None:
        post = dict(task_payload["post_search_requirements"])
        post["entity_capabilities"] = tuple(post.get("entity_capabilities") or ())
        task_payload["post_search_requirements"] = PostSearchRequirements(**post)
    for name in ("entities", "required_capabilities", "optional_capabilities"):
        task_payload[name] = tuple(task_payload.get(name) or ())
    task = TaskSpec(**task_payload)
    constraint_payload = dict(payload.get("constraints") or {})
    constraint_payload["reason_codes"] = tuple(constraint_payload.get("reason_codes") or ())
    constraints = RequestConstraints(**constraint_payload)
    envelope_payload = dict(payload.get("envelope") or {})
    envelope_payload.setdefault("ordered_entities", task.entities)
    envelope_payload["ordered_entities"] = tuple(envelope_payload.get("ordered_entities") or ())
    envelope_payload["reason_codes"] = tuple(envelope_payload.get("reason_codes") or ())
    envelope = TaskEnvelope(**envelope_payload)
    gaps = tuple(_gap_from_payload(item) for item in payload.get("gaps") or ())
    exp = dict(payload["expectation"])
    expectation = AdaptiveEvalExpectation(
        expected_mode=str(exp["expected_mode"]),
        acceptable_stop_reasons=tuple(exp.get("acceptable_stop_reasons") or ()),
        final_gap_states=tuple(
            (str(key), str(value)) for key, value in (exp.get("final_gap_states") or {}).items()
        ),
        required_capabilities=tuple(exp.get("required_capabilities") or ()),
        useful_capabilities=tuple(exp.get("useful_capabilities") or ()),
        forbidden_capabilities=tuple(exp.get("forbidden_capabilities") or ()),
        expected_rejections=tuple(exp.get("expected_rejections") or ()),
        unavoidable_gap_ids=tuple(exp.get("unavoidable_gap_ids") or ()),
        max_investigator_turns=int(exp.get("max_investigator_turns", 4)),
        max_capability_calls=int(exp.get("max_capability_calls", 6)),
        expected_clarification=bool(exp.get("expected_clarification", False)),
        expected_limitation=bool(exp.get("expected_limitation", False)),
        normal_solvable=bool(exp.get("normal_solvable", True)),
        minimum_equivalence_suppressions=int(exp.get("minimum_equivalence_suppressions", 0)),
        forbidden_context_markers=tuple(exp.get("forbidden_context_markers") or ()),
        baseline=tuple(
            (str(key), float(value)) for key, value in (exp.get("baseline") or {}).items()
        ),
    )
    evaluation_modes = tuple(
        str(item) for item in payload.get("evaluation_modes") or ("replay", "model-replay")
    )
    if not evaluation_modes or any(
        item not in {"replay", "model-replay"} for item in evaluation_modes
    ):
        raise AdaptiveEvaluationError(
            f"{source}:{line_number}: unsupported evaluation mode"
        )
    return AdaptiveEvalScenario(
        scenario_id=str(payload["scenario_id"]),
        schema_version=str(payload["schema_version"]),
        corpus_version=str(payload["corpus_version"]),
        categories=tuple(str(item) for item in payload["categories"]),
        task=task,
        constraints=constraints,
        envelope=envelope,
        gaps=gaps,
        decisions=tuple(payload.get("decisions") or ()),
        results=tuple(dict(item) for item in payload.get("results") or ()),
        initial_results=tuple(dict(item) for item in payload.get("initial_results") or ()),
        expectation=expectation,
        evaluation_modes=evaluation_modes,
        router_calls=max(0, int(payload.get("router_calls", 1))),
        budget_overrides=tuple(
            (str(key), int(value)) for key, value in (payload.get("budget_overrides") or {}).items()
        ),
    )


def _gap_from_payload(payload: Mapping[str, Any]) -> EvidenceGap:
    return EvidenceGap(
        gap_id=str(payload["gap_id"]),
        dimension=str(payload["dimension"]),
        importance=str(payload.get("importance", "required")),  # type: ignore[arg-type]
        status=str(payload.get("status", "open")),  # type: ignore[arg-type]
        temporal_requirement=str(payload.get("temporal_requirement", "current")),  # type: ignore[arg-type]
        authorized_capabilities=tuple(payload.get("authorized_capabilities") or ()),
        authority_requirement=str(payload.get("authority_requirement", "authorized_source")),  # type: ignore[arg-type]
        entities=tuple(payload.get("entities") or ()),
    )


def _tool_result_from_fixture(
    fixture: Mapping[str, Any],
    *,
    step: PlanStep | None,
    task: TaskSpec,
) -> ToolResult:
    capability = str(fixture["capability"])
    entities = tuple(
        fixture.get("entities")
        or (tuple(step.arguments.get("entities", ())) if step is not None else ())
    )
    status = str(fixture.get("status", "ok"))
    completeness = str(fixture.get("completeness", "complete"))
    usable = bool(fixture.get("projection_usable", status in {"ok", "empty", "not_found"}))
    source_complete = bool(fixture.get("source_payload_complete", completeness == "complete"))
    graph_version = str(fixture.get("active_graph_version") or "")
    views = tuple(fixture.get("selected_views") or ())
    detail = str(fixture.get("detail") or "standard")
    purpose = str(fixture.get("purpose") or "")
    limitations = tuple(str(item) for item in fixture.get("limitations") or ())
    contradictions = tuple(str(item) for item in fixture.get("contradictions") or ())
    structured: Any = None
    semantic_query_id = ""
    context_identity = ""
    if capability in {"graph.search_assets", "graph.aggregate_assets"}:
        if task.structured_query is None:
            raise AdaptiveEvaluationError("Structured replay result requires a structured TaskSpec")
        graph_version = graph_version or "eval-graph-v1"
        query_identity = structured_query_identity(
            task.structured_query,
            active_graph_version=graph_version,
        )
        filters = task.structured_query.filters.model_dump(mode="json", exclude_none=True)
        if capability == "graph.search_assets":
            rows = tuple(dict(item) for item in fixture.get("rows") or ())
            structured = StructuredAssetSearchEvidence(
                capability="graph.search_assets",
                query_identity=query_identity,
                normalized_filters=filters,
                active_graph_version=graph_version,
                sort=str(getattr(task.structured_query.sort, "value", None) or "graph_key"),
                direction=str(getattr(task.structured_query.direction, "value", None) or "asc"),
                matched_total=int(fixture.get("matched_total", len(rows))),
                returned_count=len(rows),
                truncated=bool(fixture.get("truncated", False)),
                rows=rows,
                retrieved_at="2026-01-01T00:00:00Z",
                limitations=limitations,
            )
        else:
            groups = tuple(dict(item) for item in fixture.get("groups") or ())
            structured = StructuredAssetAggregateEvidence(
                capability="graph.aggregate_assets",
                query_identity=query_identity,
                normalized_filters=filters,
                active_graph_version=graph_version,
                operation=str(getattr(task.structured_query.operation, "value", None) or "count"),
                group_by=(
                    str(getattr(task.structured_query.group_by, "value", task.structured_query.group_by))
                    if task.structured_query.group_by else None
                ),
                count=int(fixture.get("count", 0)),
                groups=groups,
                truncated=bool(fixture.get("truncated", False)),
                retrieved_at="2026-01-01T00:00:00Z",
                group_by_fields=tuple(
                    str(getattr(item, "value", item))
                    for item in task.structured_query.group_by_fields
                ),
                limitations=limitations,
            )
        semantic_query_id = structured.semantic_query_id
        context_identity = query_identity
    raw_payload: dict[str, Any] = {}
    if capability.startswith("graph."):
        raw_payload = {
            "active_graph_version": graph_version or "eval-graph-v1",
            "requested_scope": step.arguments.get("scope") if step is not None else "node_summary",
            "direction": step.arguments.get("direction") if step is not None else task.direction,
            "depth": step.arguments.get("depth") if step is not None else task.graph_depth,
            "relationship_mode": step.arguments.get("relationship_mode") if step is not None else task.relationship_mode,
            "retrieval_complete": source_complete,
            "requested_scope_complete": source_complete,
            "complete_for_user_request": source_complete,
            "retrieval_truncated": bool(fixture.get("truncated", False)),
        }
    view_payload = (
        {"views": {view: {"synthetic": True, "value": fixture.get("value", "available")} for view in views}}
        if views else None
    )
    return ToolResult(
        status=status,  # type: ignore[arg-type]
        entities=entities,
        source_capability=capability,
        retrieved_at="2026-01-01T00:00:00Z",
        freshness=str(fixture.get("freshness", "current")),
        completeness=completeness,  # type: ignore[arg-type]
        facts=(EvidenceFact(capability, "synthetic_fixture", fixture.get("value", status)),),
        limitations=limitations,
        contradictions=contradictions,
        provider=str(fixture.get("provider") or (
            "product_api" if capability.startswith("asset.")
            else "neo4j" if capability.startswith("graph.")
            else "qdrant"
        )),
        raw_payload=raw_payload,
        view_payload=view_payload,
        selected_views=views,
        detail=detail,
        purpose=purpose,
        projection_schema_version="adaptive-eval-synthetic-v1",
        source_payload_complete=source_complete,
        projection_usable=usable,
        projection_truncated=bool(fixture.get("truncated", False)),
        truncated=bool(fixture.get("truncated", False)),
        structured_asset_set=structured,
        semantic_query_id=semantic_query_id,
        context_identity=context_identity,
        step_id=step.id if step is not None else "initial",
    )


def _reuse_reference(ledger: EvidenceLedger, reference_id: str, gap_id: str) -> EvidenceLedger:
    references = tuple(
        replace(item, covered_gap_ids=tuple(dict.fromkeys((*item.covered_gap_ids, gap_id))))
        if item.reference_id == reference_id and item.reusable
        else item
        for item in ledger.evidence_references
    )
    reusable = any(
        item.reference_id == reference_id and item.reusable for item in references
    )
    if not reusable:
        return ledger
    gaps = tuple(
        replace(item, status="satisfied") if item.gap_id == gap_id and item.status == "open" else item
        for item in ledger.gaps
    )
    return replace(ledger, evidence_references=references, gaps=gaps)


def _evaluation_registry(settings: Settings) -> Any:
    class _Provider:
        def __init__(self, configured: Settings) -> None:
            self.settings = configured

    dummy = _Provider(settings)
    return build_capability_registry(
        asset_profile_provider=dummy,
        detection_provider=dummy,
        graph_provider=dummy,
        knowledge_service=dummy,
    )


def _hard_gate_results(results: Sequence[AdaptiveEvalResult]) -> dict[str, bool]:
    memory_or_no_live = tuple(
        item for item in results
        if "memory_only" in item.categories or "no_live" in item.categories
    )
    direct = tuple(item for item in results if "direct" in item.categories)
    return {
        "accepted_authority_violations_zero": sum(item.authority_violations_accepted for item in results) == 0,
        "accepted_premature_finish_zero": sum(item.premature_finish_accepted for item in results) == 0,
        "no_live_execution_for_memory_or_no_live": sum(item.live_tool_calls for item in memory_or_no_live) == 0,
        "graph_authority_bypass_zero": sum(item.graph_authority_bypasses for item in results) == 0,
        "structured_query_mutation_accepted_zero": sum(item.structured_mutations_accepted for item in results) == 0,
        "raw_evidence_context_leakage_zero": sum(item.raw_context_leaks for item in results) == 0,
        "investigator_context_hard_limit_respected": sum(
            item.context_hard_limit_violations for item in results
        ) == 0,
        "raw_reasoning_report_leakage_zero": True,
        "direct_adaptive_entry_zero": sum(item.selected_mode == "adaptive" for item in direct) == 0,
        "duplicate_provider_execution_zero": sum(item.duplicate_provider_calls for item in results) == 0,
        "intermediate_memory_persistence_zero": sum(item.memory_writes for item in results) == 0,
    }


def _quality_gate_results(metrics: Mapping[str, Any], thresholds: Mapping[str, Any]) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for metric, rule in dict(thresholds.get("quality_gates") or {}).items():
        value = metrics.get(metric)
        if value is None:
            results[metric] = False
            continue
        operator = str(rule.get("operator") or "min")
        target = float(rule["value"])
        results[metric] = (
            float(value) >= target if operator == "min"
            else float(value) <= target if operator == "max"
            else float(value) == target
        )
    return results


def _category_metrics(results: Sequence[AdaptiveEvalResult]) -> dict[str, dict[str, float]]:
    categories = sorted({category for item in results for category in item.categories})
    return {
        category: {
            "scenario_count": float(len(selected)),
            "success_rate": _rate(sum(item.success for item in selected), len(selected)),
            "average_turns": _average([item.investigator_turns for item in selected]),
            "average_capability_calls": _average([item.capability_calls for item in selected]),
        }
        for category in categories
        for selected in [[item for item in results if category in item.categories]]
    }


def _baseline_comparison(
    scenarios: Sequence[AdaptiveEvalScenario],
    results: Sequence[AdaptiveEvalResult],
) -> dict[str, float]:
    baselines = [dict(item.expectation.baseline) for item in scenarios if item.expectation.baseline]
    comparable_results = [
        result for scenario, result in zip(scenarios, results) if scenario.expectation.baseline
    ]
    if not baselines:
        return {}
    baseline_calls = sum(item.get("capability_calls", 0.0) for item in baselines)
    adaptive_calls = sum(item.capability_calls for item in comparable_results)
    baseline_coverage = _average([item.get("required_gap_completion_rate", 0.0) for item in baselines])
    adaptive_coverage = _rate(
        sum(item.required_gap_satisfied for item in comparable_results),
        sum(item.required_gap_total for item in comparable_results),
    )
    return {
        "scenario_count": float(len(baselines)),
        "baseline_required_gap_completion_rate": baseline_coverage,
        "adaptive_required_gap_completion_rate": adaptive_coverage,
        "required_gap_completion_delta": adaptive_coverage - baseline_coverage,
        "baseline_capability_calls": baseline_calls,
        "adaptive_capability_calls": float(adaptive_calls),
        "capability_call_delta": float(adaptive_calls) - baseline_calls,
        "baseline_llm_calls": sum(item.get("llm_calls", 0.0) for item in baselines),
        "adaptive_llm_calls": float(sum(item.llm_calls for item in comparable_results)),
    }


def _unavailable_report(
    scenarios: Sequence[AdaptiveEvalScenario],
    thresholds: Mapping[str, Any],
    settings: Settings,
) -> AdaptiveEvalReport:
    deployment = settings.deployment_for_purpose("investigator")
    return AdaptiveEvalReport(
        schema_version=EVALUATION_SCHEMA_VERSION,
        commit_sha=_commit_sha(),
        evaluation_mode="model-replay",
        status="unavailable",
        model=deployment.model,
        deployment=deployment.name,
        deterministic_sampling_supported=bool(
            deployment.supports_temperature and deployment.supports_top_p
        ),
        prompt_sha256=_prompt_hash(settings.investigator_system_prompt_path),
        corpus_version=scenarios[0].corpus_version if scenarios else DEFAULT_CORPUS_VERSION,
        threshold_version=str(thresholds.get("threshold_version") or "unknown"),
        scenario_count=len(scenarios),
        passed_count=0,
        failed_count=0,
        metrics={},
        category_metrics={},
        baseline_comparison={},
        hard_gate_results={},
        quality_gate_results={},
        failed_scenarios=(),
        readiness="NOT_READY",
        results=(),
    )


def _validate_synthetic_addresses(payload: Any, *, source: Path, line_number: int) -> None:
    def values(item: Any) -> Iterable[str]:
        if isinstance(item, Mapping):
            for key, value in item.items():
                yield str(key)
                yield from values(value)
        elif isinstance(item, (list, tuple)):
            for value in item:
                yield from values(value)
        elif isinstance(item, str):
            yield item

    import re

    for value in values(payload):
        for match in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", value):
            try:
                address = ipaddress.ip_address(match)
            except ValueError as exc:
                raise AdaptiveEvaluationError(f"{source}:{line_number}: invalid synthetic IP") from exc
            if not any(address in network for network in _RFC5737_NETWORKS):
                raise AdaptiveEvaluationError(
                    f"{source}:{line_number}: non-RFC5737 address is forbidden"
                )


def _prompt_text(path: str) -> str:
    source = Path(path)
    if not source.is_absolute():
        source = Path.cwd() / source
    return source.read_text(encoding="utf-8")


def _prompt_hash(path: str) -> str:
    return hashlib.sha256(_prompt_text(path).encode("utf-8")).hexdigest()


def _commit_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _rate(numerator: int | float, denominator: int | float) -> float:
    return round(100.0 * float(numerator) / float(denominator), 4) if denominator else 100.0


def _average(values: Sequence[int | float]) -> float:
    return round(float(statistics.fmean(values)), 4) if values else 0.0


def _percentile95(values: Sequence[int | float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, int((0.95 * len(ordered) + 0.999999) - 1)))
    return round(ordered[index], 4)
