"""Request-scoped implementations for the durable Copilot workflow nodes."""

from __future__ import annotations

import logging
import json
import time
from dataclasses import replace
from typing import Any

from src.core.agent.action_validator import AgentActionValidationError, AgentActionValidator
from src.core.agent.agent_loop import (
    build_observation,
    evaluate_progress,
    initialize_ledger,
    update_ledger,
)
from src.core.agent.contracts import (
    AgentClarifyDecision,
    AgentContinueDecision,
    AgentFinishDecision,
    AgentLoopBudget,
    AgentLoopState,
    AgentObservation,
    InvestigationState,
)
from src.core.agent.events import WorkflowEventContext, WorkflowEventLogger
from src.core.agent.evidence_policy import (
    EvidenceRequirementPolicy,
    MemorySufficiencyGate,
    apply_gap_plan,
    build_gap_plan,
    evidence_refs_from_validated_result,
    log_gap_plan,
    structured_memory_statement,
)
from src.core.agent.evidence import apply_context_inclusion, context_package_from_evidence
from src.core.agent.plan_validator import PlanValidationError
from src.core.agent.planner import PlannerError
from src.core.agent.investigator import InvestigatorError
from src.core.agent.investigator_context import (
    InvestigatorContextBuilder,
    InvestigatorContextError,
)
from src.core.agent.task_mapping import (
    compile_direct_plan,
    compile_supplemental_plan,
    derive_request_constraints,
    derive_task_envelope,
    derive_turn_policy,
    evidence_mode_from_request,
    enforce_task_envelope,
    historical_evidence_classes_for_request,
    is_broad_conversation_recall,
    materialize_turn_policy_target,
    task_spec_from_route,
    select_orchestration_mode,
)
from src.core.agent.specialists import AssetInvestigationSpecialist, GraphAnalysisSpecialist
from src.core.agent.structured_continuity import structured_query_context_from_state
from src.core.agent.structured_presentation import (
    is_structured_presentation_only,
    render_structured_presentation,
)
from src.core.context import ContextComposer, normalize_intent_route
from src.core.context.intent import (
    SECURITY_ANALYSIS_WORDS,
    resolution_from_materialized_decision,
)
from src.core.context.models import EntityResolution, ResolvedEntity, RouteDecision, approx_tokens
from src.core.context.compaction import (
    current_evidence_projections,
    episodic_baseline_projections,
    historical_baseline_projections,
    investigation_baseline_from_results,
)
from src.core.context.synthesizer_prompt import SynthesizerPromptBuilder
from src.core.copilot.fallback_answer import build_evidence_fallback_answer
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent
from src.core.llm.token_estimator import TokenEstimator
from src.core.memory.episodes import EntityVisit, MemoryContextKey
from src.core.memory.store import extract_working_facts
from src.core.memory.long_term import LongTermMemoryRecord
from src.core.memory.routing_state import SessionRoutingState
from src.core.observability.metrics import get_metrics


logger = logging.getLogger(__name__)
service_logger = logging.getLogger("src.core.copilot.service")


class CopilotWorkflowNodes:
    """Perform one bounded responsibility per LangGraph node."""

    def __init__(self, service: Any, *, stream_sink: Any = None) -> None:
        self.service = service
        self.settings = service.settings
        self.stream_sink = stream_sink
        self.context_composer = ContextComposer(self.settings)
        self.synthesizer_prompt_builder = SynthesizerPromptBuilder()
        self.evidence_requirement_policy = EvidenceRequirementPolicy()
        self.memory_sufficiency_gate = MemorySufficiencyGate()

    def resolve_entities(self, state: InvestigationState) -> dict[str, Any]:
        session = state["session_id"]
        routing_state = self.service.routing_state_store.get(session)
        recent = (
            self.service.memory_store.recent_for_routing(
                session,
                max(2, self.settings.conversation_recent_raw_messages),
            )
            if self.settings.chat_store_history
            else []
        )
        get_metrics().observe_memory_retrieval(
            "short_term",
            "hit" if recent else "miss",
        )
        constraints = derive_request_constraints(state["message"])
        resolution = self.service.entity_resolver.resolve(
            state["message"].strip(),
            state.get("ui_context"),
            routing_state,
            recent_messages=recent,
            request_id=state["request_id"],
            conversation_scope=is_broad_conversation_recall(state["message"]),
        )
        turn_policy = derive_turn_policy(
            state["message"], constraints, resolution, routing_state
        )
        resolution = materialize_turn_policy_target(resolution, turn_policy)
        task_envelope = derive_task_envelope(
            resolution, constraints, turn_policy, routing_state
        )
        pending_facts = extract_working_facts(state["message"]) if constraints.memory_write else ()
        resolved_values = tuple(item.value for item in resolution.entities)
        pending_facts = tuple(
            replace(
                fact,
                scope=(
                    "conversation"
                    if fact.key == "analyst_name" or not resolved_values
                    else "entity"
                ),
                entity_ids=() if fact.key == "analyst_name" or not resolved_values else resolved_values,
                source_request_id=state["request_id"],
            )
            for fact in pending_facts
        )
        logger.info(
            "event=request_constraints_resolved request_id=%s allow_live=%s require_current=%s memory_only=%s memory_write=%s reason_count=%s",
            state["request_id"],
            constraints.allow_live,
            constraints.require_current,
            constraints.memory_only,
            constraints.memory_write,
            len(constraints.reason_codes),
        )
        retrieve_long_term = getattr(self.service, "retrieve_long_term_memory", None)
        long_term_selection = (
            retrieve_long_term(
                identity=state["request_identity"],
                message=state["message"].strip(),
                entity_ids=tuple(item.value for item in resolution.entities),
                allow_entity_scoped=bool(
                    resolution.entities
                    and turn_policy.operation != "topic_detach"
                    and turn_policy.target != "conversation"
                ),
                required_evidence_classes=historical_evidence_classes_for_request(
                    state["message"]
                ),
            )
            if retrieve_long_term is not None
            else None
        )
        update: dict[str, Any] = {
            "resolved_entities": resolution,
            "active_entity_state": routing_state,
            "recent_messages": recent,
            "long_term_memory_selection": long_term_selection,
            "request_constraints": constraints,
            "turn_policy": turn_policy,
            "task_envelope": task_envelope,
            "pending_working_facts": pending_facts,
            "next_edge": "route",
        }
        if (
            resolution.reference_type == "compare_with_reference"
            and resolution.explicit_candidate_count == 1
            and len({item.value for item in resolution.entities}) != 2
        ):
            update.update(
                self._clarification(
                    "Please provide the second IP address to compare, or first select/investigate the previous asset.",
                    "comparison_second_entity_required",
                )
            )
        return update

    def route(self, state: InvestigationState) -> dict[str, Any]:
        entities = state["resolved_entities"]
        routing_state = state["active_entity_state"]
        recent = state.get("recent_messages") or []
        constraints = state.get("request_constraints") or derive_request_constraints(state["message"])
        turn_policy = state.get("turn_policy") or derive_turn_policy(
            state["message"], constraints, entities, routing_state
        )
        task_envelope = state.get("task_envelope") or derive_task_envelope(
            entities, constraints, turn_policy, routing_state
        )
        if not turn_policy.requires_domain_router:
            values = tuple(item.value for item in entities.entities)
            source = entities.entities[0].source if entities.entities else ""
            binding = (
                "explicit" if source == "message" else
                "ui" if source == "ui" else
                "active_pair" if len(values) == 2 else
                "active_single" if values else
                "none"
            )
            route = RouteDecision(
                use_graph=False,
                use_detection=False,
                use_asset_profile=False,
                use_knowledge=False,
                reason="deterministic_memory_fast_path",
                entity_binding=binding,
                requested_entity_binding=binding,
                resolved_entity_binding=binding,
                binding_source=source,
                binding_available=bool(values),
                binding_normalized=bool(values),
                binding_normalization_reason=(
                    "memory_request_preserves_resolved_entity" if values else None
                ),
                materialized_entity_count=len(values),
                materialized_entities=values,
                target_entity=entities.primary_entity,
                target_entities=list(entities.entities),
                followup_detected=bool(values),
                intent="asset_investigation" if values else "general_knowledge",
                scope="node_summary" if values else "none",
                direction="both" if values else "none",
                decision_source="deterministic_fallback",
                semantic_router_called=False,
            )
            logger.info(
                "event=memory_fast_path_selected request_id=%s router_bypassed=true entity_count=%s pending_working_fact_count=%s",
                state["request_id"],
                len(values),
                len(state.get("pending_working_facts") or ()),
            )
            return {
                "routing_result": route,
                "resolved_entities": entities,
                "routing_fallback_used": False,
                "next_edge": "validate_task",
            }
        decision = self.service.intent_router.classify(
            state["message"].strip(),
            entities,
            routing_state,
            ui_context=state.get("ui_context"),
            recent_messages=recent,
            trace_id=state["trace_id"],
            request_id=state["request_id"],
        )
        if decision.fallback_used and str(decision.error_reason or "").startswith(
            "structured_result_"
        ):
            logger.info(
                "event=structured_result_reference_rejected request_id=%s drop_reason=%s",
                state["request_id"],
                str(decision.error_reason)[:120],
            )
            return {
                "routing_fallback_used": True,
                **self._clarification(
                    "That structured result does not retain the requested Asset reference. "
                    "Please rerun or refine the search, or choose one of the retained results.",
                    "structured_result_reference_unavailable",
                ),
            }
        if decision.fallback_used:
            route_entities = entities
            route = self.service.fallback_router.route(
                state["message"].strip(),
                entities,
                routing_state,
                fallback_reason=decision.fallback_reason or decision.error_reason or "router_failed",
                request_id=state["request_id"],
                constraints=constraints,
                turn_policy=turn_policy,
            )
            route = replace(
                route,
                semantic_router_called=decision.router_called,
                semantic_router_latency_ms=decision.latency_ms,
                semantic_router_retry_count=decision.retry_count,
                semantic_router_finish_reason=decision.finish_reason,
                semantic_router_content_present=decision.content_present,
                semantic_router_error=decision.error_reason,
                semantic_router_status=decision.runtime_status,
                fallback_used=True,
                fallback_reason=decision.fallback_reason or decision.error_reason,
            )
            if route.reason == "deterministic_structured_parse_unavailable":
                return {
                    "routing_fallback_used": True,
                    **self._clarification(
                        "I recognized this as a structured Asset-set request, but its selectors or thresholds are not safely representable. Please restate it with an allow-listed field and an unambiguous value.",
                        "structured_query_clarification_required",
                    ),
                }
            recognized_fallback = bool(
                route.structured_query is not None
                or route.use_graph
                or route.use_detection
                or route.use_asset_profile
                or route.use_knowledge
            )
            if decision.runtime_status in {"technical_failure", "disabled"} and not recognized_fallback:
                route = replace(
                    route,
                    intent=None,
                    reason="semantic_router_technical_failure",
                    use_graph=False,
                    use_detection=False,
                    use_asset_profile=False,
                    use_knowledge=False,
                    entity_binding="none",
                    requested_entity_binding="none",
                    resolved_entity_binding="none",
                    binding_source="none",
                    binding_available=False,
                    materialized_entity_count=0,
                    materialized_entities=(),
                    target_entity=None,
                    target_entities=[],
                    scope="none",
                    direction="none",
                    depth=0,
                    requires_multiple_entities=False,
                    relationship_mode="none",
                    matched_signals=["semantic_router_technical_failure", "fail_closed"],
                    semantic_router_status=decision.runtime_status,
                )
                logger.warning(
                    "event=semantic_router_technical_failure request_id=%s fallback_reason=%s "
                    "deterministic_route_recognized=false",
                    state["request_id"],
                    str(route.fallback_reason or "router_failed")[:120],
                )
                return {
                    "routing_result": route,
                    "resolved_entities": EntityResolution(status="none"),
                    "routing_fallback_used": True,
                    "workflow_status": "failed",
                    "terminal": True,
                    "failure_metadata": {
                        "error_type": "semantic_router_technical_failure",
                        "safe_error_code": "semantic_router_technical_failure",
                        "retryable": True,
                    },
                    "next_edge": "safe_failure",
                }
            if route.structured_query is not None:
                route_entities = EntityResolution(status="none")
        else:
            route_entities = resolution_from_materialized_decision(decision, entities)
            route = normalize_intent_route(decision, route_entities)
            if decision.intent == "out_of_scope":
                logger.info(
                    "event=semantic_router_out_of_scope request_id=%s terminal=true",
                    state["request_id"],
                )
                return {
                    "routing_result": route,
                    "resolved_entities": EntityResolution(status="none"),
                    "turn_policy": replace(
                        turn_policy,
                        operation="topic_detach",
                        target="none",
                        target_entities=(),
                        episode_transition="keep",
                        operational_state_mutation_allowed=False,
                    ),
                    "routing_fallback_used": False,
                    "workflow_status": "failed",
                    "terminal": True,
                    "failure_metadata": {
                        "error_type": "out_of_scope",
                        "safe_error_code": "out_of_scope",
                        "retryable": False,
                    },
                    "next_edge": "safe_failure",
                }
            if decision.intent == "unclear":
                return {
                    "routing_result": route,
                    "resolved_entities": EntityResolution(status="none"),
                    "routing_fallback_used": False,
                    **self._clarification(
                        "I’m not certain which asset, result set, or analysis goal you mean. Please clarify the target and the question you want answered.",
                        "semantic_request_ambiguous",
                    ),
                }
            active_pair = tuple(dict.fromkeys(routing_state.active_entities))
            semantic_comparison = bool(
                route.scope == "multi_entity_comparison"
                or route.requires_multiple_entities
                or route.relationship_mode == "compare"
            )
            if (
                semantic_comparison
                and entities.explicit_candidate_count == 0
                and not entities.reference_suppressed
                and len(active_pair) == 2
                and len({item.value for item in route_entities.entities}) < 2
            ):
                recovered = [
                    ResolvedEntity(type="ip", value=value, source="conversation")
                    for value in active_pair
                ]
                route_entities = EntityResolution(
                    status="resolved",
                    entities=recovered,
                    primary_entity=None,
                    entity_mode="multiple",
                    candidate_count=2,
                    explicit_candidate_count=0,
                    valid_entity_count=2,
                    reference_detected=True,
                    reference_type="semantic_active_pair_recovery",
                )
                route = replace(
                    route,
                    use_graph=True,
                    entity_binding="active_pair",
                    resolved_entity_binding="active_pair",
                    binding_source="conversation",
                    binding_available=True,
                    binding_normalized=True,
                    binding_normalization_reason="semantic_comparison_recovers_unique_active_pair",
                    materialized_entity_count=2,
                    materialized_entities=active_pair,
                    target_entity=None,
                    target_entities=recovered,
                    followup_detected=True,
                    scope="multi_entity_comparison",
                    direction="both",
                    depth=max(1, route.depth),
                    requires_multiple_entities=True,
                    relationship_mode="compare",
                    route_normalized=True,
                    route_normalization_reason="semantic_comparison_recovers_unique_active_pair",
                )
                turn_policy = replace(
                    turn_policy,
                    target="active_pair",
                    target_entities=active_pair,
                    episode_transition="keep",
                    reason_codes=tuple(
                        dict.fromkeys(
                            (*turn_policy.reason_codes, "semantic_comparison_active_pair_recovered")
                        )
                    ),
                )
                task_envelope = derive_task_envelope(
                    route_entities, constraints, turn_policy, routing_state
                )
                logger.info(
                    "event=comparison_pair_recovered request_id=%s "
                    "comparison_pair_recovered=true comparison_pair_source=active_operational_pair "
                    "active_pair=%s",
                    state["request_id"],
                    ",".join(active_pair),
                )
        reference = route.structured_result_reference
        if reference.kind == "select_entities":
            turn_policy = derive_turn_policy(
                state["message"], constraints, route_entities, routing_state
            )
            task_envelope = derive_task_envelope(
                route_entities, constraints, turn_policy, routing_state
            )
            logger.info(
                "event=structured_result_reference_resolved request_id=%s "
                "reference_kind=select_entities selected_count=%s",
                state["request_id"],
                len(reference.ordinals),
            )
        elif reference.kind == "set_query":
            turn_policy = replace(
                turn_policy,
                operation="follow_up",
                target="none",
                target_entities=(),
                episode_transition="keep",
                reason_codes=tuple(
                    dict.fromkeys((*turn_policy.reason_codes, "structured_set_continuation"))
                ),
            )
            task_envelope = derive_task_envelope(
                route_entities, constraints, turn_policy, routing_state
            )
            logger.info(
                "event=structured_result_reference_resolved request_id=%s "
                "reference_kind=set_query selected_count=0",
                state["request_id"],
            )
        elif reference.kind == "historical_recall":
            constraints = replace(
                constraints,
                allow_live=False,
                require_current=False,
                memory_only=True,
                reason_codes=tuple(
                    dict.fromkeys(
                        (*constraints.reason_codes, "historical_structured_result_recall")
                    )
                ),
            )
            turn_policy = replace(
                turn_policy,
                operation="memory_recall",
                target="conversation",
                target_entities=(),
                episode_transition="keep",
                operational_state_mutation_allowed=False,
                reason_codes=tuple(
                    dict.fromkeys(
                        (*turn_policy.reason_codes, "historical_structured_result_recall")
                    )
                ),
            )
            task_envelope = derive_task_envelope(
                route_entities, constraints, turn_policy, routing_state
            )
            logger.info(
                "event=structured_result_reference_resolved request_id=%s "
                "reference_kind=historical_recall selected_count=0",
                state["request_id"],
            )
        evidence_mode = evidence_mode_from_request(state["message"])
        if (
            evidence_mode in {"memory_only", "no_live_refresh"}
            and route_entities.status != "resolved"
            and entities.status == "resolved"
            and entities.entities
            and not entities.reference_suppressed
        ):
            # A valid deterministic entity must survive an inapplicable router binding.
            route_entities = entities
            values = tuple(item.value for item in entities.entities)
            source = entities.entities[0].source
            binding = {
                "message": "explicit",
                "ui": "ui",
                "conversation": "active_pair" if len(values) == 2 else "active_single",
            }[source]
            route = replace(
                route,
                entity_binding=binding,
                resolved_entity_binding=binding,
                binding_source=source,
                binding_available=True,
                binding_normalized=True,
                binding_normalization_reason="memory_request_preserves_resolved_entity",
                materialized_entity_count=len(values),
                materialized_entities=values,
                target_entity=entities.primary_entity,
                target_entities=list(entities.entities),
                followup_detected=True,
            )
        if SECURITY_ANALYSIS_WORDS.search(state["message"]) and "security_or_anomaly" not in route.matched_signals:
            route = replace(route, matched_signals=[*route.matched_signals, "security_or_anomaly"])
        route = enforce_task_envelope(route, task_envelope)
        long_term_selection = state.get("long_term_memory_selection")
        if route.structured_query is not None:
            long_term_selection = self._without_entity_scoped_ltm(
                long_term_selection,
                request_id=state["request_id"],
            )
        return {
            "routing_result": route,
            "resolved_entities": route_entities,
            "turn_policy": turn_policy,
            "task_envelope": task_envelope,
            "request_constraints": constraints,
            "long_term_memory_selection": long_term_selection,
            "routing_fallback_used": bool(route.fallback_used),
            "next_edge": "validate_task",
        }

    def validate_task(self, state: InvestigationState) -> dict[str, Any]:
        route = state["routing_result"]
        entities = state["resolved_entities"]
        distinct = tuple(dict.fromkeys(route.materialized_entities))
        if (
            entities.explicit_candidate_count > self.settings.agent_max_entities
            or len(distinct) > self.settings.agent_max_entities
        ):
            return self._clarification(
                "Please provide no more than two IP addresses for this request.",
                "too_many_entities",
            )
        if route.scope == "multi_entity_comparison" and len(distinct) != 2:
            return self._clarification(
                "Please provide exactly two distinct IP addresses for this comparison.",
                "comparison_requires_two_distinct_entities",
            )
        if (
            entities.reference_type == "compare_with_reference"
            and entities.explicit_candidate_count == 1
            and len({item.value for item in entities.entities}) != 2
        ):
            return self._clarification(
                "Please provide the second IP address to compare, or first select/investigate the previous asset.",
                "comparison_second_entity_required",
            )
        try:
            task = task_spec_from_route(
                route,
                state["message"].strip(),
                state.get("request_constraints"),
                state.get("turn_policy"),
                state.get("task_envelope"),
            )
        except Exception as exc:
            return {
                "workflow_status": "failed",
                "failure_metadata": {
                    "error_type": type(exc).__name__,
                    "safe_error_code": "task_validation_failed",
                },
                "next_edge": "safe_failure",
            }
        constraints = state.get("request_constraints") or derive_request_constraints(state["message"])
        if constraints.require_current and not task.required_capabilities:
            return {
                "workflow_status": "failed",
                "failure_metadata": {
                    "error_type": "current_verification_requires_live_evidence",
                    "safe_error_code": "current_verification_requires_live_evidence",
                    "retryable": False,
                },
                "limitation_reasons": ["current_verification_requires_live_evidence"],
                "next_edge": "safe_failure",
            }

        planner_selected = (
            task.workflow_mode == "multi_step"
            and bool((state.get("request_constraints") or derive_request_constraints(state["message"])).allow_live)
            and self.settings.planner_enabled
        )
        orchestration_mode = select_orchestration_mode(
            task,
            constraints=constraints,
            adaptive_enabled=self.settings.adaptive_agent_enabled,
            planner_selected=planner_selected,
        )
        task = replace(task, orchestration_mode=orchestration_mode)
        planner_selected = planner_selected and orchestration_mode == "fixed"
        requirements = self.evidence_requirement_policy.derive(task)
        selection = state.get("long_term_memory_selection")
        memories = tuple(getattr(selection, "memories", ()) or ())
        decisions = self.memory_sufficiency_gate.evaluate(requirements, memories)
        gap_plan = build_gap_plan(requirements, decisions)
        log_gap_plan(state["request_id"], gap_plan)
        WorkflowEventLogger(
            logger,
            WorkflowEventContext(
                state.get("request_id", ""),
                state.get("trace_id", ""),
                state.get("session_id", ""),
            ),
        ).emit(
            "orchestration_mode_selected",
            orchestration_mode=orchestration_mode,
            status="selected",
            planner_called=planner_selected,
        )
        return {
            "task": task,
            "workflow_mode": task.workflow_mode,
            "orchestration_mode": orchestration_mode,
            "planner_called": planner_selected,
            "memory_context_key": self._authorized_memory_context_key(state, task),
            "evidence_requirements": requirements,
            "evidence_gap_plan": gap_plan,
            "memory_tool_results": [],
            "next_edge": (
                "adaptive" if orchestration_mode == "adaptive"
                else "planner" if planner_selected
                else "direct"
            ),
        }

    def initialize_agent_loop(self, state: InvestigationState) -> dict[str, Any]:
        """Initialize request-local adaptive state after memory and routing authority."""
        route = state.get("routing_result")
        router_calls = (
            1 + int(getattr(route, "semantic_router_retry_count", 0) or 0)
            if bool(getattr(route, "semantic_router_called", False))
            else 0
        )
        gap_plan = state.get("evidence_gap_plan")
        memory_results: list[Any] = []
        if gap_plan is not None:
            try:
                _unused_plan, gap_plan, memory_results = apply_gap_plan(
                    compile_direct_plan(state["task"]),
                    gap_plan,
                )
            except ValueError:
                memory_results = []
        ledger = initialize_ledger(state["task"], gap_plan)
        if memory_results:
            ledger = update_ledger(
                ledger,
                state["task"],
                tuple(memory_results),
            )
        budget = AgentLoopBudget(
            max_investigator_turns=self.settings.agent_max_investigator_turns,
            max_llm_calls=self.settings.agent_max_llm_calls,
            max_capabilities_per_decision=2,
            max_total_capability_calls=self.settings.agent_max_total_capability_calls,
            max_deepened_entities=self.settings.agent_max_deepened_entities,
            max_graph_depth=self.settings.agent_max_graph_depth,
            max_technical_failures=self.settings.agent_max_technical_failures,
            deadline_monotonic=time.monotonic() + self.settings.agent_request_timeout_seconds,
            llm_calls=router_calls,
        )
        loop = AgentLoopState(
            turn=0,
            budget=budget,
            ledger=ledger,
            started_monotonic=time.monotonic(),
        )
        self._agent_events(state).emit(
            "agent_loop_initialized",
            orchestration_mode="adaptive",
            agent_turn=0,
            gap_count=len(ledger.gaps),
            llm_call_count=router_calls,
            remaining_turns=budget.remaining_turns,
            remaining_tool_calls=budget.remaining_capability_calls,
            status="started",
        )
        get_metrics().agent_loops.labels("started").inc() if get_metrics().enabled else None
        return {
            "agent_loop_state": loop,
            "agent_loop_started_at": loop.started_monotonic,
            "memory_tool_results": memory_results,
            "tool_results": memory_results,
            "capability_results": memory_results,
            "evidence_gap_plan": gap_plan,
            "next_edge": "evaluate",
        }

    def evaluate_agent_progress(self, state: InvestigationState) -> dict[str, Any]:
        loop = state["agent_loop_state"]
        stop_reason = evaluate_progress(
            loop.ledger,
            loop.budget,
            consecutive_no_progress=loop.consecutive_no_progress,
        )
        if stop_reason is None:
            self._agent_events(state).emit(
                "agent_progress_evaluated",
                agent_turn=loop.turn,
                gap_count=sum(item.status == "open" for item in loop.ledger.gaps),
                remaining_turns=loop.budget.remaining_turns,
                remaining_tool_calls=loop.budget.remaining_capability_calls,
                material_progress=(loop.observations[-1].material_progress if loop.observations else False),
                status="continue",
            )
            return {"next_edge": "decide"}
        return self._finish_agent_loop(state, loop, stop_reason)

    def investigator_decide(self, state: InvestigationState) -> dict[str, Any]:
        loop = state["agent_loop_state"]
        turn = loop.budget.investigator_turns + 1
        events = self._agent_events(state)
        events.emit(
            "agent_decision_requested",
            agent_turn=turn,
            gap_count=sum(item.status == "open" for item in loop.ledger.gaps),
            llm_call_count=loop.budget.llm_calls,
            remaining_turns=loop.budget.remaining_turns,
            remaining_tool_calls=loop.budget.remaining_capability_calls,
            status="requested",
        )
        registry, _validator, _executor = self.service._capability_runtime_snapshot()
        try:
            context = InvestigatorContextBuilder(self.settings, registry).build(
                task=state["task"],
                constraints=state["request_constraints"],
                envelope=state["task_envelope"],
                ledger=loop.ledger,
                latest_observation=loop.observations[-1] if loop.observations else None,
                budget=loop.budget,
                long_term_selection=state.get("long_term_memory_selection"),
                turn=turn,
                system_prompt=getattr(self.service.investigator, "system_prompt", ""),
            )
        except InvestigatorContextError:
            return self._finish_agent_loop(state, loop, "context_budget_exhausted", budget_type="context")
        get_metrics().observe_investigator_context(
            context.input_tokens_before,
            context.input_tokens_after,
        )
        events.emit(
            "agent_context_budget_checked",
            agent_turn=turn,
            raw_estimate=context.raw_estimate,
            calibrated_estimate=context.calibrated_estimate,
            output_reservation=context.output_reservation,
            input_tokens_before=context.input_tokens_before,
            input_tokens_after=context.input_tokens_after,
            compacted_tokens=context.compacted_tokens,
            reference_count=context.evidence_reference_count,
            delta_count=context.delta_count,
            capability_schema_count=context.capability_schema_count,
            remaining_hard_budget=context.remaining_hard_budget,
            remaining_turns=loop.budget.remaining_turns,
            status="ok",
        )
        if context.compacted:
            events.emit(
                "agent_context_compacted",
                agent_turn=turn,
                input_tokens_before=context.input_tokens_before,
                input_tokens_after=context.input_tokens_after,
                compacted_tokens=context.compacted_tokens,
                reference_count=context.evidence_reference_count,
                status="compacted",
            )
        consumed = replace(
            loop.budget,
            investigator_turns=turn,
            llm_calls=loop.budget.llm_calls + 1,
        )
        contextual_loop = replace(
            loop,
            latest_context_tokens_before=context.input_tokens_before,
            latest_context_tokens_after=context.input_tokens_after,
        )
        try:
            if self.service.investigator is None:
                raise InvestigatorError("investigator_unavailable", "Investigator role is unavailable.")
            decision = self.service.investigator.decide(
                context.context_json,
                request_id=state["request_id"],
                trace_id=state["trace_id"],
            )
        except InvestigatorError as exc:
            failed_budget = replace(
                consumed,
                technical_failures=consumed.technical_failures + 1,
            )
            failed_loop = replace(contextual_loop, turn=turn, budget=failed_budget)
            events.emit(
                "agent_decision_invalid",
                level=logging.WARNING,
                agent_turn=turn,
                status="invalid",
                error_type=exc.code,
                llm_call_count=failed_budget.llm_calls,
            )
            return {
                "agent_loop_state": failed_loop,
                "agent_decision_status": "invalid",
                "next_edge": "retry",
            }
        updated = replace(contextual_loop, turn=turn, budget=consumed, latest_decision=decision)
        events.emit(
            "agent_decision_received",
            agent_turn=turn,
            decision_kind=decision.kind,
            capability_count=len(decision.capability_requests) if isinstance(decision, AgentContinueDecision) else 0,
            llm_call_count=consumed.llm_calls,
            status="received",
        )
        return {
            "agent_loop_state": updated,
            "agent_decision": decision,
            "agent_decision_status": "valid",
            "next_edge": "validate",
        }

    def validate_agent_action(self, state: InvestigationState) -> dict[str, Any]:
        loop = state["agent_loop_state"]
        decision = state["agent_decision"]
        events = self._agent_events(state)
        if isinstance(decision, AgentClarifyDecision):
            events.emit(
                "agent_loop_finished",
                agent_turn=loop.turn,
                decision_kind=decision.kind,
                stop_reason="clarification_required",
                status="clarification_required",
            )
            return {
                **self._clarification(decision.clarification_summary, decision.clarification_code),
                "agent_loop_state": replace(loop, stop_reason="clarification_required"),
                "agent_action_edge": "clarify",
            }
        if isinstance(decision, AgentFinishDecision):
            open_required = tuple(
                item for item in loop.ledger.gaps
                if item.importance == "required" and item.status == "open"
            )
            deterministic_stop = evaluate_progress(
                loop.ledger,
                loop.budget,
                consecutive_no_progress=loop.consecutive_no_progress,
            )
            if open_required and deterministic_stop is None:
                failed = replace(
                    loop,
                    budget=replace(
                        loop.budget,
                        technical_failures=loop.budget.technical_failures + 1,
                    ),
                )
                events.emit(
                    "agent_action_rejected",
                    level=logging.WARNING,
                    agent_turn=loop.turn,
                    decision_kind=decision.kind,
                    status="rejected",
                    reason="finish_with_obtainable_required_gap",
                )
                get_metrics().observe_agent_action("rejected")
                return {"agent_loop_state": failed, "agent_action_edge": "retry", "next_edge": "retry"}
            reason = deterministic_stop or decision.stop_reason
            return self._finish_agent_loop(state, loop, reason)
        if not isinstance(decision, AgentContinueDecision):
            return self._reject_agent_action(state, loop, "investigator_decision_type_invalid")

        registry, validator, _executor = self.service._capability_runtime_snapshot()
        get_metrics().observe_agent_action("selected")
        try:
            validated = AgentActionValidator(registry, validator).validate(
                decision,
                task=state["task"],
                constraints=state["request_constraints"],
                ledger=loop.ledger,
                budget=loop.budget,
                turn=loop.turn,
            )
        except AgentActionValidationError as exc:
            if exc.code == "repeated_action":
                return self._finish_agent_loop(state, loop, "repeated_action")
            if exc.code in {
                "equivalent_evidence_already_available",
                "repeated_failed_action",
            }:
                capability = (
                    decision.capability_requests[0].capability
                    if decision.capability_requests
                    else ""
                )
                self._agent_events(state).emit(
                    "agent_evidence_equivalent",
                    agent_turn=loop.turn,
                    capability=capability,
                    status="equivalent",
                    reason=exc.code,
                    material_progress=False,
                )
                self._agent_events(state).emit(
                    "agent_action_equivalent_blocked",
                    level=logging.INFO,
                    agent_turn=loop.turn,
                    capability=capability,
                    status="skipped",
                    reason=exc.code,
                    material_progress=False,
                )
                get_metrics().observe_agent_equivalent_action(exc.code)
                if exc.code == "equivalent_evidence_already_available":
                    return self._reuse_equivalent_agent_evidence(
                        state,
                        loop,
                        reference_id=exc.evidence_reference_id,
                        gap_id=exc.evidence_gap_id,
                        reason=exc.code,
                    )
                return self._reject_agent_action(state, loop, exc.code)
            if exc.code == "tool_budget_exhausted":
                return self._finish_agent_loop(state, loop, "tool_budget_exhausted", budget_type="tool")
            return self._reject_agent_action(state, loop, exc.code)
        events.emit(
            "agent_action_validated",
            agent_turn=loop.turn,
            decision_kind=decision.kind,
            capability_count=len(validated.plan.steps),
            status="validated",
        )
        get_metrics().observe_agent_action("validated")
        return {
            "agent_action_plan": validated.plan,
            "agent_action_fingerprints": validated.fingerprints,
            "agent_action_edge": "execute",
            "next_edge": "execute",
        }

    def execute_agent_action(self, state: InvestigationState) -> dict[str, Any]:
        loop = state["agent_loop_state"]
        plan = state["agent_action_plan"]
        _registry, _validator, executor = self.service._capability_runtime_snapshot()
        results = executor.execute(
            plan,
            base_payload={
                "request_id": state["request_id"],
                "session_id": state["session_id"],
                "route": state["routing_result"],
            },
        )
        candidates = set(loop.ledger.structured_candidates)
        deepened = tuple(
            entity for entity in plan.target_entities
            if entity in candidates and entity not in loop.budget.deepened_entities
        )
        budget = replace(
            loop.budget,
            capability_calls=loop.budget.capability_calls + len(plan.steps),
            deepened_entities=tuple(dict.fromkeys((*loop.budget.deepened_entities, *deepened))),
        )
        updated_loop = replace(loop, budget=budget)
        existing = list(state.get("tool_results") or ())
        self._agent_events(state).emit(
            "agent_tool_observation",
            agent_turn=loop.turn,
            capability_count=len(plan.steps),
            tool_call_count=budget.capability_calls,
            status="observed",
        )
        get_metrics().observe_agent_action("executed")
        return {
            "agent_loop_state": updated_loop,
            "agent_action_results": results,
            "tool_results": [*existing, *results],
            "capability_results": [*existing, *results],
            "execution_plan": plan,
            "iteration_count": int(state.get("iteration_count", 0)) + 1,
            "next_edge": "observe",
        }

    def build_agent_observation(self, state: InvestigationState) -> dict[str, Any]:
        loop = state["agent_loop_state"]
        results = tuple(state.get("agent_action_results") or ())
        current = update_ledger(
            loop.ledger,
            state["task"],
            results,
            action_fingerprints=state.get("agent_action_fingerprints") or (),
            action_steps=tuple(state["agent_action_plan"].steps),
        )
        decision = state.get("agent_decision")
        requests = decision.capability_requests if isinstance(decision, AgentContinueDecision) else ()
        observation = build_observation(
            turn=loop.turn,
            requests=requests,
            results=results,
            previous=loop.ledger,
            current=current,
            context_input_tokens_before=loop.latest_context_tokens_before,
            context_input_tokens_after=loop.latest_context_tokens_after,
        )
        created_count = len(observation.new_evidence_references)
        changed_count = len(observation.changed_evidence_references)
        get_metrics().observe_agent_evidence_references(created_count, changed_count)
        get_metrics().observe_agent_material_progress(observation.material_progress)
        if created_count or changed_count:
            self._agent_events(state).emit(
                "agent_evidence_reference_created",
                agent_turn=loop.turn,
                reference_count=observation.evidence_reference_count,
                new_reference_count=created_count,
                changed_reference_count=changed_count,
                status="updated",
            )
        if not observation.material_progress:
            self._agent_events(state).emit(
                "agent_no_material_progress",
                agent_turn=loop.turn,
                capability_count=len(requests),
                reference_count=observation.evidence_reference_count,
                material_progress=False,
                status="no_progress",
            )
        self._agent_events(state).emit(
            "agent_observation_delta_built",
            agent_turn=loop.turn,
            capability_count=len(requests),
            gap_count=len(observation.remaining_gap_ids),
            new_reference_count=created_count,
            changed_reference_count=changed_count,
            resolved_gap_count=len(observation.new_coverage),
            remaining_gap_count=len(observation.remaining_gap_ids),
            material_progress=observation.material_progress,
            status="built",
        )
        return {
            "agent_pending_ledger": current,
            "agent_pending_observation": observation,
            "next_edge": "update",
        }

    def update_agent_ledger(self, state: InvestigationState) -> dict[str, Any]:
        loop = state["agent_loop_state"]
        observation = state["agent_pending_observation"]
        updated = replace(
            loop,
            ledger=state["agent_pending_ledger"],
            observations=(*loop.observations, observation)[-8:],
            consecutive_no_progress=(
                0 if observation.material_progress else loop.consecutive_no_progress + 1
            ),
        )
        self._agent_events(state).emit(
            "agent_ledger_updated",
            agent_turn=loop.turn,
            reference_count=len(updated.ledger.evidence_references),
            remaining_gap_count=sum(item.status == "open" for item in updated.ledger.gaps),
            material_progress=observation.material_progress,
            status="updated",
        )
        return {"agent_loop_state": updated, "next_edge": "evaluate"}

    @staticmethod
    def _agent_events(state: InvestigationState) -> WorkflowEventLogger:
        return WorkflowEventLogger(
            logger,
            WorkflowEventContext(state["request_id"], state["trace_id"], state["session_id"]),
        )

    def _reject_agent_action(
        self,
        state: InvestigationState,
        loop: AgentLoopState,
        reason: str,
    ) -> dict[str, Any]:
        budget = replace(
            loop.budget,
            technical_failures=loop.budget.technical_failures + 1,
        )
        decision = loop.latest_decision
        requests = decision.capability_requests if isinstance(decision, AgentContinueDecision) else ()
        observation = AgentObservation(
            turn=loop.turn,
            capability_requests=requests,
            result_references=(),
            status_summary=(),
            new_coverage=(),
            new_contradictions=(),
            remaining_gap_ids=tuple(item.gap_id for item in loop.ledger.gaps if item.status == "open"),
            material_progress=False,
            tool_call_count=0,
            rejected_actions=(reason,),
            budget_delta=(("technical_failures", 1),),
            evidence_reference_count=len(loop.ledger.evidence_references),
            context_input_tokens_before=loop.latest_context_tokens_before,
            context_input_tokens_after=loop.latest_context_tokens_after,
        )
        updated = replace(
            loop,
            budget=budget,
            observations=(*loop.observations, observation)[-8:],
            consecutive_no_progress=loop.consecutive_no_progress + 1,
        )
        self._agent_events(state).emit(
            "agent_action_rejected",
            level=logging.WARNING,
            agent_turn=loop.turn,
            status="rejected",
            reason=reason,
        )
        get_metrics().observe_agent_action("rejected")
        return {"agent_loop_state": updated, "agent_action_edge": "retry", "next_edge": "retry"}

    def _reuse_equivalent_agent_evidence(
        self,
        state: InvestigationState,
        loop: AgentLoopState,
        *,
        reference_id: str,
        gap_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Close only the proven compatible gap without mutating its ToolResult."""
        reference_exists = any(
            item.reference_id == reference_id
            for item in loop.ledger.evidence_references
        )
        gap_is_open = any(
            item.gap_id == gap_id and item.status == "open"
            for item in loop.ledger.gaps
        )
        if not reference_exists or not gap_is_open:
            return self._reject_agent_action(
                state,
                loop,
                "equivalent_evidence_reference_invalid",
            )
        references = tuple(
            replace(
                item,
                covered_gap_ids=tuple(dict.fromkeys((*item.covered_gap_ids, gap_id))),
            )
            if item.reference_id == reference_id
            else item
            for item in loop.ledger.evidence_references
        )
        gaps = tuple(
            replace(item, status="satisfied")
            if item.gap_id == gap_id and item.status == "open"
            else item
            for item in loop.ledger.gaps
        )
        ledger = replace(loop.ledger, gaps=gaps, evidence_references=references)
        decision = loop.latest_decision
        requests = decision.capability_requests if isinstance(decision, AgentContinueDecision) else ()
        observation = AgentObservation(
            turn=loop.turn,
            capability_requests=requests,
            result_references=(reference_id,) if reference_id else (),
            status_summary=(),
            new_coverage=(gap_id,),
            new_contradictions=(),
            remaining_gap_ids=tuple(item.gap_id for item in gaps if item.status == "open"),
            material_progress=True,
            tool_call_count=0,
            rejected_actions=(reason,),
            evidence_reference_count=len(references),
            context_input_tokens_before=loop.latest_context_tokens_before,
            context_input_tokens_after=loop.latest_context_tokens_after,
        )
        updated = replace(
            loop,
            ledger=ledger,
            observations=(*loop.observations, observation)[-8:],
            consecutive_no_progress=0,
        )
        get_metrics().observe_agent_material_progress(True)
        self._agent_events(state).emit(
            "agent_observation_delta_built",
            agent_turn=loop.turn,
            capability_count=len(requests),
            gap_count=len(observation.remaining_gap_ids),
            resolved_gap_count=len(observation.new_coverage),
            remaining_gap_count=len(observation.remaining_gap_ids),
            material_progress=True,
            reason=reason,
            status="reused",
        )
        self._agent_events(state).emit(
            "agent_ledger_updated",
            agent_turn=loop.turn,
            reference_count=len(references),
            remaining_gap_count=len(observation.remaining_gap_ids),
            material_progress=True,
            status="updated",
        )
        return {"agent_loop_state": updated, "agent_action_edge": "retry", "next_edge": "retry"}

    def _finish_agent_loop(
        self,
        state: InvestigationState,
        loop: AgentLoopState,
        stop_reason: Any,
        *,
        budget_type: str = "",
    ) -> dict[str, Any]:
        updated = replace(loop, stop_reason=stop_reason)
        if budget_type:
            get_metrics().observe_agent_budget_exhaustion(budget_type)
        status = "completed" if stop_reason in {"evidence_sufficient", "goal_satisfied"} else "limited"
        get_metrics().observe_agent_loop(
            status=status,
            turns=loop.budget.investigator_turns,
            duration_seconds=max(0.0, time.monotonic() - loop.started_monotonic),
            stop_reason=stop_reason,
        )
        self._agent_events(state).emit(
            "agent_loop_finished",
            orchestration_mode="adaptive",
            agent_turn=loop.turn,
            stop_reason=stop_reason,
            llm_call_count=loop.budget.llm_calls,
            tool_call_count=loop.budget.capability_calls,
            status=status,
        )
        limitations = list(state.get("limitation_reasons") or ())
        if status == "limited" and stop_reason not in limitations:
            limitations.append(str(stop_reason))
        return {
            "agent_loop_state": updated,
            "agent_action_edge": "finish",
            "limitation_reasons": limitations,
            "next_edge": "finish",
        }

    @staticmethod
    def _clarification(answer: str, code: str) -> dict[str, Any]:
        return {
            "workflow_status": "clarification_required",
            "terminal": True,
            "clarification": {"answer": answer, "code": code},
            "next_edge": "clarification",
        }

    @staticmethod
    def _without_entity_scoped_ltm(selection: Any, *, request_id: str) -> Any:
        """Re-scope preliminary LTM after a zero-entity structured route is authoritative."""
        if selection is None:
            return None
        memories = tuple(
            item
            for item in tuple(getattr(selection, "memories", ()) or ())
            if not tuple(getattr(getattr(item, "memory", None), "entity_ids", ()) or ())
        )
        baselines = tuple(
            item
            for item in tuple(getattr(selection, "baseline_memories", ()) or ())
            if not tuple(getattr(getattr(item, "memory", None), "entity_ids", ()) or ())
        )
        removed = (
            len(tuple(getattr(selection, "memories", ()) or ())) - len(memories)
            + len(tuple(getattr(selection, "baseline_memories", ()) or ())) - len(baselines)
        )
        if not removed:
            return selection
        logger.info(
            "event=long_term_memory_rescoped request_id=%s scope=global_structured_set removed_entity_scoped=%s retained=%s",
            request_id,
            removed,
            len(memories),
        )
        return replace(
            selection,
            memories=memories,
            baseline_memories=baselines,
            selected_count=len(memories),
            estimated_tokens=sum(item.estimated_tokens for item in memories),
            limitations=tuple(
                dict.fromkeys(
                    (*tuple(getattr(selection, "limitations", ()) or ()), "entity_scoped_ltm_excluded_for_global_query")
                )
            ),
        )

    def _authorized_memory_context_key(self, state: InvestigationState, task: Any) -> MemoryContextKey:
        candidate = MemoryContextKey.from_task(task)
        policy = state.get("turn_policy")
        if not getattr(policy, "operational_state_mutation_allowed", True):
            # Recall scope controls selection only. It must not be replaced by
            # the active operational key merely because no episode transition
            # is allowed.
            return candidate
        if getattr(policy, "episode_transition", "switch") != "keep":
            return candidate
        working = self.service.memory_store.repository.get_working(state["session_id"])
        return working.context_key if working is not None else candidate


    def build_direct_plan(self, state: InvestigationState) -> dict[str, Any]:
        return {
            "execution_plan": compile_direct_plan(state["task"]),
            "planner_called": False,
            "next_edge": "validate_plan",
        }

    def build_plan(self, state: InvestigationState) -> dict[str, Any]:
        if not (state.get("request_constraints") or derive_request_constraints(state["message"])).allow_live:
            return {
                "execution_plan": compile_direct_plan(state["task"]),
                "planner_called": False,
                "next_edge": "validate_plan",
            }
        registry, _validator, _executor = self.service._capability_runtime_snapshot()
        try:
            plan = self.service.planner.plan(
                state["task"],
                registry.list(planner_visible=True),
                request_id=state["request_id"],
                trace_id=state["trace_id"],
            )
        except PlannerError as exc:
            plan = replace(
                compile_direct_plan(state["task"]),
                source="deterministic_fallback",
                planner_called=True,
            )
            return {
                "execution_plan": plan,
                "planner_called": True,
                "fallback_used": True,
                "failure_metadata": {
                    "error_type": exc.code,
                    "safe_error_code": exc.code,
                    "recovered": True,
                },
                "next_edge": "validate_plan",
            }
        return {"execution_plan": plan, "planner_called": True, "next_edge": "validate_plan"}

    def validate_plan(self, state: InvestigationState) -> dict[str, Any]:
        _registry, validator, _executor = self.service._capability_runtime_snapshot()
        plan, gap_plan, memory_results = apply_gap_plan(
            state["execution_plan"],
            state["evidence_gap_plan"],
        )
        constraints = state.get("request_constraints")
        if constraints is not None and not constraints.allow_live and plan.steps:
            return {
                "plan_validation_result": {
                    "valid": False,
                    "fallback_allowed": False,
                    "error_code": "live_capability_forbidden_by_request",
                    "step_id": plan.steps[0].id,
                },
                "failure_metadata": {
                    "error_type": "live_capability_forbidden_by_request",
                    "safe_error_code": "live_capability_forbidden_by_request",
                    "retryable": False,
                },
                "next_edge": "safe_failure",
            }
        for selection in gap_plan.view_selections:
            logger.info(
                "event=view_selected request_id=%s capability=%s views=%s entity_count=%s reason=%s",
                state["request_id"],
                selection.capability,
                ",".join(selection.views),
                len(selection.entities),
                selection.reason,
            )
            get_metrics().observe_view(selection.capability, selection.views)
        for result in memory_results:
            logger.info(
                "event=tool_skipped_from_memory request_id=%s capability=%s entity_count=%s reason=memory_reused_authoritative",
                state["request_id"],
                result.source_capability,
                len(result.entities),
            )
        try:
            validated = validator.validate(
                plan,
                satisfied_capabilities=gap_plan.skipped_capabilities,
            )
        except PlanValidationError as exc:
            fallback_allowed = plan.source == "llm" and not state.get("fallback_used")
            return {
                "plan_validation_result": {
                    "valid": False,
                    "fallback_allowed": fallback_allowed,
                    "error_code": exc.code,
                    "step_id": exc.step_id,
                },
                "failure_metadata": {
                    "error_type": exc.code,
                    "safe_error_code": exc.code,
                    "retryable": False,
                },
                "next_edge": "fallback" if fallback_allowed else "safe_failure",
            }
        return {
            "execution_plan": validated,
            "evidence_gap_plan": gap_plan,
            "memory_tool_results": memory_results,
            "plan_validation_result": {"valid": True, "fallback_allowed": False},
            "next_edge": "execute",
        }

    def build_fallback_plan(self, state: InvestigationState) -> dict[str, Any]:
        return {
            "execution_plan": replace(
                compile_direct_plan(state["task"]),
                source="deterministic_fallback",
                planner_called=bool(state.get("planner_called")),
            ),
            "fallback_used": True,
            "next_edge": "validate_plan",
        }

    def execute_capabilities(self, state: InvestigationState) -> dict[str, Any]:
        constraints = state.get("request_constraints")
        if constraints is not None and not constraints.allow_live and state["execution_plan"].steps:
            return {
                "tool_results": list(state.get("memory_tool_results") or ()),
                "capability_results": list(state.get("memory_tool_results") or ()),
                "failure_metadata": {
                    "error_type": "live_capability_forbidden_by_request",
                    "safe_error_code": "live_capability_forbidden_by_request",
                    "retryable": False,
                },
                "next_edge": "safe_failure",
            }
        _registry, _validator, executor = self.service._capability_runtime_snapshot()
        results = executor.execute(
            state["execution_plan"],
            base_payload={
                "request_id": state["request_id"],
                "session_id": state["session_id"],
                "route": state["routing_result"],
            },
        )
        return {
            "tool_results": [*(state.get("memory_tool_results") or ()), *results],
            "capability_results": [*(state.get("memory_tool_results") or ()), *results],
            "iteration_count": int(state.get("iteration_count", 0)) + 1,
            "next_edge": "build_evidence",
        }

    def dispatch_specialists(self, state: InvestigationState) -> dict[str, Any]:
        """Run validated domain steps through bounded zero-LLM specialist subgraphs."""
        _registry, _validator, executor = self.service._capability_runtime_snapshot()
        plan = state["execution_plan"]
        events = WorkflowEventLogger(
            logger,
            WorkflowEventContext(state["request_id"], state["trace_id"], state["session_id"]),
        )
        base = {
            "request_id": state["request_id"],
            "trace_id": state["trace_id"],
            "session_id": state["session_id"],
            "workflow_id": state["workflow_id"],
            "execution_plan": plan,
            "route": state["routing_result"],
        }
        outputs: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        specialist_results: list[Any] = []
        for key, label, specialist_type, prefix in (
            ("asset_specialist_result", "asset_investigation", AssetInvestigationSpecialist, "asset."),
            ("graph_specialist_result", "graph_analysis", GraphAnalysisSpecialist, "graph."),
        ):
            capability_count = sum(step.capability.startswith(prefix) for step in plan.steps)
            if not capability_count:
                events.emit(
                    "specialist_skipped",
                    workflow_id=state["workflow_id"],
                    specialist=label,
                    status="skipped",
                    capability_count=0,
                    reason=f"no_{prefix.rstrip('.')}_capability",
                )
                records.append({"specialist": label, "status": "skipped", "reason": f"no_{prefix.rstrip('.')}_capability"})
                continue
            events.emit(
                "specialist_started",
                workflow_id=state["workflow_id"],
                specialist=label,
                status="running",
                entity_count=len(plan.task.entities),
                capability_count=capability_count,
            )
            started = time.perf_counter()
            try:
                result = specialist_type(executor).run({**base, "specialist": label})
            except Exception as exc:
                events.emit(
                    "specialist_failed",
                    level=logging.ERROR,
                    workflow_id=state["workflow_id"],
                    specialist=label,
                    status="failed",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error_type=type(exc).__name__,
                )
                raise
            typed = result["specialist_result"]
            specialist_results.extend(result.get("tool_results") or ())
            outputs[key] = typed
            records.append(
                {
                    "specialist": label,
                    "status": typed.status,
                    "entity_count": len(typed.entities),
                    "capability_count": len(typed.executed_capabilities),
                    "result_count": len(typed.result_statuses),
                }
            )
            events.emit(
                "specialist_completed",
                workflow_id=state["workflow_id"],
                specialist=label,
                status=typed.status,
                latency_ms=int((time.perf_counter() - started) * 1000),
                entity_count=len(typed.entities),
                capability_count=len(typed.executed_capabilities),
                result_count=len(typed.result_statuses),
                missing_count=len(typed.missing_evidence),
                truncated=typed.truncated,
                next_edge="join_specialist_results",
            )

        generic_steps = tuple(
            step
            for step in plan.steps
            if not step.capability.startswith(("asset.", "graph."))
        )
        generic_results: list[Any] = []
        if generic_steps:
            generic_ids = {step.id for step in generic_steps}
            generic_capabilities = tuple(dict.fromkeys(step.capability for step in generic_steps))
            generic_plan = replace(
                plan,
                task=replace(
                    plan.task,
                    required_capabilities=tuple(
                        capability
                        for capability in generic_capabilities
                        if capability in plan.task.required_capabilities
                    ),
                    optional_capabilities=tuple(
                        capability
                        for capability in generic_capabilities
                        if capability in plan.task.optional_capabilities
                    ),
                ),
                steps=tuple(
                    replace(step, depends_on=tuple(item for item in step.depends_on if item in generic_ids))
                    for step in generic_steps
                ),
                maximum_allowed_calls=min(plan.maximum_allowed_calls, len(generic_steps)),
                validated=True,
            )
            generic_results = executor.execute(
                generic_plan,
                base_payload={
                    "request_id": state["request_id"],
                    "session_id": state["session_id"],
                    "route": state["routing_result"],
                },
            )
        return {
            **outputs,
            "specialist_tool_results": specialist_results,
            "generic_tool_results": generic_results,
            "specialist_records": records,
            "next_edge": "join_specialist_results",
        }

    def join_specialist_results(self, state: InvestigationState) -> dict[str, Any]:
        """Restore deterministic parent-plan result order before evidence review."""
        results = [
            *(state.get("memory_tool_results") or ()),
            *(state.get("specialist_tool_results") or ()),
            *(state.get("generic_tool_results") or ()),
        ]
        by_step = {item.step_id: item for item in results}
        ordered = [
            *[item for item in results if item.provider == "long_term_memory"],
            *[by_step[step.id] for step in state["execution_plan"].steps if step.id in by_step],
        ]
        return {
            "tool_results": ordered,
            "capability_results": ordered,
            "iteration_count": int(state.get("iteration_count", 0)) + 1,
            "next_edge": "build_evidence",
        }

    def build_evidence(self, state: InvestigationState) -> dict[str, Any]:
        pack = self.service.evidence_reviewer.build_pack(
            state["task"],
            state.get("tool_results") or [],
            plan=state.get("execution_plan"),
            request_id=state["request_id"],
            trace_id=state["trace_id"],
            supplemental_history=tuple(
                (state.get("supplemental_retrieval_state") or {}).get("history") or ()
            ),
            post_search_enrichment=state.get("post_search_enrichment_summary"),
        )
        return {"evidence_pack": pack, "next_edge": "review_retrieval"}

    def review_retrieval(self, state: InvestigationState) -> dict[str, Any]:
        allow = (
            state.get("orchestration_mode") != "adaptive"
            and getattr(state.get("task"), "orchestration_mode", "direct") != "adaptive"
            and
            bool((state.get("request_constraints") or derive_request_constraints(state["message"])).allow_live)
            and
            self.settings.agent_max_supplemental_retrievals > 0
            and int(state.get("supplemental_retrieval_count", 0)) < 1
        )
        decision = self.service.evidence_reviewer.review(
            state["task"],
            state.get("tool_results") or [],
            allow_supplemental=allow,
        )
        get_metrics().observe_evidence_review(
            str(
                state.get("orchestration_mode")
                or getattr(state.get("task"), "orchestration_mode", "unknown")
                or "unknown"
            ),
            decision.outcome,
        )
        pack = self.service.evidence_reviewer.with_review(state["evidence_pack"], decision)
        logger.info(
            "event=evidence_review_classified request_id=%s outcome=%s caveat_count=%s material_limitation_count=%s",
            state["request_id"],
            decision.outcome,
            len(decision.caveats),
            len(decision.material_limitations),
        )
        next_edge = "supplemental" if decision.supplemental_allowed and allow else "compose"
        return {"review_decision": decision, "evidence_pack": pack, "next_edge": next_edge}

    def supplemental_retrieval(self, state: InvestigationState) -> dict[str, Any]:
        if not (state.get("request_constraints") or derive_request_constraints(state["message"])).allow_live:
            return {"next_edge": "build_evidence"}
        decision = state["review_decision"]
        if int(state.get("supplemental_retrieval_count", 0)) >= 1:
            return {"next_edge": "build_evidence"}
        if not decision.next_capability or decision.next_arguments is None:
            return {"next_edge": "build_evidence"}
        _registry, validator, executor = self.service._capability_runtime_snapshot()
        plan = validator.validate(
            compile_supplemental_plan(
                state["task"],
                decision.next_capability,
                decision.next_arguments,
                plan_id=state["execution_plan"].plan_id,
            )
        )
        existing = state.get("tool_results") or []
        duplicate = any(
            item.source_capability == decision.next_capability
            and item.entities == tuple(decision.next_arguments.get("entities") or ())
            for item in existing
        )
        if duplicate:
            return {
                "supplemental_retrieval_count": 1,
                "supplemental_retrieval_state": {
                    "status": "skipped",
                    "reason": "duplicate_equivalent_call",
                    "history": (),
                },
                "next_edge": "build_evidence",
            }
        results = executor.execute(
            plan,
            base_payload={
                "request_id": state["request_id"],
                "session_id": state["session_id"],
                "route": state["routing_result"],
            },
        )
        return {
            "tool_results": [*existing, *results],
            "capability_results": [*existing, *results],
            "supplemental_retrieval_count": 1,
            "supplemental_retrieval_state": {
                "status": results[0].status if results else "unavailable",
                "history": (
                    {
                        "capability": decision.next_capability,
                        "status": results[0].status if results else "unavailable",
                    },
                ),
            },
            "next_edge": "build_evidence",
        }

    def compose_context(self, state: InvestigationState) -> dict[str, Any]:
        task = state["task"]
        pack = state["evidence_pack"]
        package = context_package_from_evidence(pack, state["resolved_entities"])
        context_key = state.get("memory_context_key") or MemoryContextKey.from_task(task)
        long_term_selection = state.get("long_term_memory_selection")
        long_term_memories = tuple(
            getattr(long_term_selection, "memories", ()) or ()
        )
        structured_long_term_baselines = tuple(
            getattr(long_term_selection, "baseline_memories", ()) or ()
        ) or long_term_memories
        snapshot = (
            self.service.memory_store.prepare_for_model(
                state["session_id"],
                self.settings,
                state["active_entity_state"],
                context_key=context_key,
                request_id=state["request_id"],
                long_term_memories=long_term_memories,
                activate_context=(
                    getattr(state.get("turn_policy"), "operational_state_mutation_allowed", True)
                    and
                    getattr(state.get("turn_policy"), "episode_transition", "switch") in {"switch", "detach"}
                ),
                thread_recall=(
                    getattr(state.get("turn_policy"), "operation", "") == "memory_recall"
                    and getattr(state.get("turn_policy"), "target", "") == "conversation"
                ),
            )
            if self.settings.chat_store_history or long_term_memories
            else None
        )
        history = list(snapshot.messages if snapshot else [])
        active_state = state["active_entity_state"]
        structured_reference_kind = getattr(
            getattr(state["routing_result"], "structured_result_reference", None),
            "kind",
            "none",
        )
        continuity_arguments = {
            "structured_context": getattr(active_state, "structured_query_context", None),
            "structured_lineage": tuple(
                getattr(active_state, "structured_query_lineage", ()) or ()
            ),
            "structured_reference_kind": structured_reference_kind,
            "active_focal_entities": tuple(getattr(active_state, "active_entities", ()) or ()),
        }
        preliminary_task_context = self.synthesizer_prompt_builder.build_context(
            task,
            tuple(state.get("tool_results") or ()),
            snapshot=snapshot,
            long_term_selection=long_term_selection,
            review=state.get("review_decision"),
            request_constraints=state.get("request_constraints"),
            accepted_working_fact_count=len(state.get("pending_working_facts") or ()),
            post_search_enrichment=state.get("post_search_enrichment_summary"),
            agent_loop_state=state.get("agent_loop_state"),
            **continuity_arguments,
        )
        preliminary_prompt = self.synthesizer_prompt_builder.render_messages(
            static_core=self.service.system_prompt,
            context=preliminary_task_context,
            dynamic_evidence="",
            history=history,
            user_message=state["message"],
        )
        deployment = self.settings.deployment_for_purpose("chat")
        request = deployment.request_config("chat")
        estimator = TokenEstimator(
            deployment=deployment.name,
            model=deployment.model,
            multiplier=self.settings.llm_token_estimate_multiplier,
        )
        output_reservation = estimator.output_reservation(
            task.detail_level,
            request.max_tokens,
            brief_output_tokens=self.settings.synthesizer_brief_output_tokens,
            standard_output_tokens=self.settings.synthesizer_standard_output_tokens,
            deep_output_tokens=self.settings.synthesizer_deep_output_tokens,
        )
        base_messages = list(preliminary_prompt.messages)
        base_estimate = estimator.estimate_messages(base_messages)
        identity = state.get("request_identity")
        current_projections = current_evidence_projections(
            tuple(state.get("tool_results") or ()),
            owner_id=str(getattr(identity, "user_id", "") or ""),
        )
        owner_id = str(getattr(identity, "user_id", "") or "")
        episode_baselines = self.service.memory_store.investigation_baselines(
            state["session_id"],
            context_key,
            owner_id=owner_id,
        )
        historical_baselines = (
            *historical_baseline_projections(structured_long_term_baselines),
            *episodic_baseline_projections(episode_baselines),
        )
        dynamic_context = self.context_composer.compose(
            package,
            request_id=state["request_id"],
            base_input_tokens=base_estimate.calibrated_tokens,
            reserved_output_tokens=output_reservation,
            current_projections=current_projections,
            historical_baselines=historical_baselines,
        )
        structured_reference = getattr(
            state["routing_result"], "structured_result_reference", None
        )
        if getattr(structured_reference, "kind", "none") == "historical_recall":
            structured_context = getattr(
                state["active_entity_state"], "structured_query_context", None
            )
            if structured_context is not None:
                historical_text = (
                    "[SOORIN_HISTORICAL_STRUCTURED_QUERY_CONTINUITY_JSON]\n"
                    + json.dumps(
                        structured_context.historical_context_payload(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n[/SOORIN_HISTORICAL_STRUCTURED_QUERY_CONTINUITY_JSON]"
                )
                maximum = self.context_composer.last_budget.get("max_dynamic_tokens", 0)
                combined = "\n\n".join(part for part in (dynamic_context, historical_text) if part)
                if approx_tokens(combined) <= maximum:
                    dynamic_context = combined
                    logger.info(
                        "event=structured_result_history_context_included request_id=%s "
                        "mode=%s ref_count=%s graph_version_present=%s",
                        state["request_id"],
                        structured_context.mode,
                        len(structured_context.result_refs),
                        bool(structured_context.active_graph_version),
                    )
        results = apply_context_inclusion(
            list(state.get("tool_results") or []),
            self.context_composer.last_inclusion,
        )
        pack = self.service.evidence_reviewer.build_pack(
            task,
            results,
            plan=state.get("execution_plan"),
            request_id=state["request_id"],
            trace_id=state["trace_id"],
            review=state["review_decision"],
            supplemental_history=tuple(
                (state.get("supplemental_retrieval_state") or {}).get("history") or ()
            ),
            post_search_enrichment=state.get("post_search_enrichment_summary"),
        )
        task_context = self.synthesizer_prompt_builder.build_context(
            task,
            tuple(results),
            snapshot=snapshot,
            long_term_selection=long_term_selection,
            review=state.get("review_decision"),
            request_constraints=state.get("request_constraints"),
            accepted_working_fact_count=len(state.get("pending_working_facts") or ()),
            post_search_enrichment=state.get("post_search_enrichment_summary"),
            delta_contexts=self.context_composer.last_delta_contexts,
            baseline_status=self.context_composer.last_baseline_status,
            baseline_present=self.context_composer.last_baseline_present,
            baseline_compatible=self.context_composer.last_baseline_compatible,
            agent_loop_state=state.get("agent_loop_state"),
            **continuity_arguments,
        )
        logger.info(
            "event=synth_continuity_facts request_id=%s previous_set_available=%s "
            "previous_set_used=%s reference_kind=%s base_set_available=%s "
            "current_result_count=%s previous_result_count=%s results_truncated=%s "
            "active_focal_count=%s focal_baseline_available=%s",
            state["request_id"],
            task_context.continuity.previous_structured_set_available,
            task_context.continuity.previous_structured_set_used,
            task_context.continuity.structured_reference_kind,
            task_context.continuity.base_structured_set_available,
            task_context.continuity.current_result_count,
            task_context.continuity.previous_result_count,
            task_context.continuity.structured_results_truncated,
            len(task_context.continuity.active_focal_entities),
            task_context.continuity.focal_baseline_available,
        )
        rendered_prompt = self.synthesizer_prompt_builder.render_messages(
            static_core=self.service.system_prompt,
            context=task_context,
            dynamic_evidence=dynamic_context,
            history=history,
            user_message=state["message"],
        )
        messages = list(rendered_prompt.messages)
        estimate = estimator.estimate_messages(messages)
        budget = estimator.window_budget(
            estimate.calibrated_tokens,
            output_reservation,
            self.settings.llm_context_safety_margin_tokens,
            self.settings.llm_context_window_tokens,
        )
        service_logger.info(
            "event=model_input_prepared request_id=%s deployment=%s provider=%s model=%s "
            "message_count=%s asset_profile_dynamic_approx_tokens=%s "
            "detection_dynamic_approx_tokens=%s graph_dynamic_approx_tokens=%s "
            "knowledge_dynamic_approx_tokens=%s dynamic_context_approx_tokens=%s "
            "calibrated_input_estimate=%s selected_output_reservation=%s "
            "remaining_usable_tokens=%s",
            state["request_id"],
            deployment.name,
            deployment.provider_type,
            deployment.model,
            len(messages),
            approx_tokens(self.context_composer.last_parts.get("asset_profile", "")),
            approx_tokens(self.context_composer.last_parts.get("detection", "")),
            approx_tokens(self.context_composer.last_parts.get("graph", "")),
            approx_tokens(self.context_composer.last_parts.get("knowledge", "")),
            approx_tokens(dynamic_context),
            estimate.calibrated_tokens,
            output_reservation,
            budget.remaining_usable_tokens,
        )
        service_logger.info(
            "event=synth_prompt_rendered request_id=%s synth_prompt_version=%s "
            "static_prompt_chars=%s static_prompt_estimated_tokens=%s "
            "dynamic_prompt_chars=%s dynamic_prompt_estimated_tokens=%s "
            "selected_module_names=%s temporal_mode=%s evidence_mode=%s response_depth=%s "
            "ltm_active_count=%s ltm_candidate_count=%s baseline_available=%s",
            state["request_id"],
            self.synthesizer_prompt_builder.version,
            len(self.service.system_prompt),
            approx_tokens(self.service.system_prompt),
            len(rendered_prompt.dynamic_prompt),
            approx_tokens(rendered_prompt.dynamic_prompt),
            ",".join(rendered_prompt.selected_module_names),
            task_context.temporal_mode,
            task_context.evidence_mode,
            task_context.response_depth,
            task_context.memory.ltm_active_count,
            task_context.memory.ltm_candidate_count,
            task_context.memory.compatible_previous_baseline_available,
        )
        return {
            "tool_results": results,
            "capability_results": results,
            "evidence_pack": pack,
            "composed_context": dynamic_context,
            "model_messages": messages,
            "synthesizer_task_context": task_context,
            "synthesizer_dynamic_prompt": rendered_prompt.dynamic_prompt,
            "synthesizer_module_names": rendered_prompt.selected_module_names,
            "conversation_snapshot": snapshot,
            "synthesis_request": {
                "max_tokens": output_reservation,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "timeout_seconds": request.read_timeout_seconds,
            },
            "context_review": {
                "fits": budget.fits,
                "required_context_missing": self.context_composer.required_context_missing,
                "required_context_missing_reason": self.context_composer.required_context_missing_reason,
                "input_tokens": estimate.calibrated_tokens,
                "remaining_usable_tokens": budget.remaining_usable_tokens,
            },
            "next_edge": "review_context",
        }

    def review_context(self, state: InvestigationState) -> dict[str, Any]:
        review = dict(state.get("context_review") or {})
        if not review.get("fits", False):
            decision = "blocked"
        elif review.get("required_context_missing"):
            decision = "limited"
        else:
            decision = "synthesize"
        review["decision"] = decision
        return {"context_review": review, "next_edge": decision}

    def synthesize(self, state: InvestigationState) -> dict[str, Any]:
        review = state["review_decision"]
        context_review = state.get("context_review") or {}
        warning: list[str] = []
        limitation_reasons: list[str] = []
        for item in state.get("tool_results") or []:
            if item.source_capability == "asset.get_detection" and item.status in {"not_found", "unavailable"}:
                warning.append(
                    "detection_evidence_not_found"
                    if item.status == "not_found"
                    else "detection_evidence_unavailable"
                )
            elif item.source_capability == "asset.get_profile" and item.status in {"not_found", "unavailable"}:
                warning.append(
                    "asset_profile_not_found"
                    if item.status == "not_found"
                    else "asset_profile_unavailable"
                )
            elif item.source_capability.startswith("graph.") and item.status == "unavailable":
                warning.append("graph_evidence_unavailable")
            elif item.source_capability == "knowledge.search" and item.status in {"not_configured", "unavailable", "invalid"}:
                warning.append(f"knowledge_evidence_{item.status}")
        statuses = {
            "graph": self._capability_status(state, "graph."),
            "detection": self._capability_status(state, "asset.get_detection"),
            "asset_profile": self._capability_status(state, "asset.get_profile"),
            "knowledge": self._capability_status(state, "knowledge.search"),
        }
        service_logger.info(
            "event=provider_statuses request_id=%s graph=%s detection=%s asset_profile=%s knowledge=%s",
            state["request_id"],
            statuses["graph"],
            statuses["detection"],
            statuses["asset_profile"],
            statuses["knowledge"],
        )
        if (
            state["task"].routing_unresolved
            and review.outcome in {"safe_failure", "missing_required_evidence"}
        ):
            result = self._deterministic(
                "I couldn't safely determine the requested cybersecurity task, so no evidence-backed answer was generated. Please restate the request with the asset, set, or analysis goal you want.",
                "routing-unresolved-guard",
            )
            warning.append("semantic_routing_unresolved")
            status = "completed_with_limitations"
            limitation_reasons.extend(review.reasons or ("semantic_routing_unresolved",))
        elif context_review.get("decision") == "blocked":
            result = self._deterministic(
                "I cannot safely generate this response because the bounded evidence context "
                "exceeds the model input window after deterministic compaction.",
                "token-window-guard",
            )
            warning.append("model_context_window_unsafe")
            status = "partial_failure"
            limitation_reasons.append("model_context_window_unsafe")
        elif context_review.get("required_context_missing"):
            result = self._deterministic(
                "I cannot safely analyze all requested evidence because the required current "
                "context could not fit within the bounded model context.",
                "context-budget-guard",
            )
            warning.append("required_context_budget_insufficient")
            status = "completed_with_limitations"
            limitation_reasons.append("required_context_budget_insufficient")
        else:
            if review.outcome in {"safe_failure", "missing_required_evidence"}:
                warning.append("required_evidence_unavailable")
                limitation_reasons.extend(
                    review.reasons or ("missing_required_evidence",)
                )
            deterministic_presentation = (
                render_structured_presentation(tuple(state.get("tool_results") or ()))
                if review.outcome in {"sufficient", "answer_with_limitations"}
                and is_structured_presentation_only(
                    state["task"].request,
                    tuple(state.get("tool_results") or ()),
                )
                else None
            )
            request = state["synthesis_request"]
            try:
                if deterministic_presentation is not None:
                    result = self._deterministic(
                        deterministic_presentation,
                        "structured-presentation",
                    )
                    logger.info(
                        "event=synthesis_response_path request_id=%s "
                        "path=deterministic_structured_presentation",
                        state["request_id"],
                    )
                elif self.stream_sink is not None:
                    metrics = {
                        "streaming_requested": True,
                        "streaming_used": False,
                        "first_reasoning_chunk_latency_ms": None,
                        "first_answer_chunk_latency_ms": None,
                        "stream_chunk_count": 0,
                        "reasoning_chunk_count": 0,
                        "answer_chunk_count": 0,
                        "stream_completed": False,
                        "stream_error_type": "",
                    }
                    result = self.service._stream_final_model(
                        state["model_messages"],
                        request_id=state["request_id"],
                        max_tokens=request["max_tokens"],
                        temperature=request["temperature"],
                        top_p=request["top_p"],
                        timeout_seconds=request["timeout_seconds"],
                        sink=self.stream_sink,
                        metrics=metrics,
                        trace_id=state["trace_id"],
                    )
                else:
                    logger.info(
                        "event=synthesis_response_path request_id=%s path=normal_synth",
                        state["request_id"],
                    )
                    result = self.service.llm_client.chat(
                        state["model_messages"],
                        request_id=state["request_id"],
                        max_tokens=request["max_tokens"],
                        temperature=request["temperature"],
                        top_p=request["top_p"],
                        timeout_seconds=request["timeout_seconds"],
                        purpose="chat",
                        trace_id=state["trace_id"],
                    )
                if review.outcome in {
                    "answer_with_limitations",
                    "safe_failure",
                    "missing_required_evidence",
                }:
                    status = "completed_with_limitations"
                    limitation_reasons.extend(review.reasons or review.limitations)
                else:
                    status = "completed"
            except LLMError as exc:
                if bool(exc.details.get("partial_output")):
                    raise
                package = context_package_from_evidence(
                    state["evidence_pack"],
                    state["resolved_entities"],
                )
                fallback = build_evidence_fallback_answer(
                    package.graph,
                    package.detections,
                    package.asset_profiles,
                )
                if not fallback:
                    raise
                result = self._deterministic(fallback, "evidence-fallback")
                warning.append("final_synthesis_fallback_used")
                status = "completed_with_limitations"
                limitation_reasons.append("deterministic_evidence_fallback_used")
        if state.get("fallback_used") and status == "completed":
            status = "completed_with_limitations"
            limitation_reasons.append("validated_plan_fallback_used")
        elif state.get("routing_fallback_used") and status == "completed":
            status = "completed_with_limitations"
            limitation_reasons.append("semantic_router_fallback_used")
        limitation_reasons = list(dict.fromkeys(item for item in limitation_reasons if item))
        if self.stream_sink is not None and result.provider == "deterministic":
            self.stream_sink(LLMStreamEvent("answer_delta", text=result.text))
        response = {
            "session_id": state["session_id"],
            "answer": result.text,
            "provider": result.provider,
            "model": result.model,
            "_warnings": list(dict.fromkeys(warning)),
        }
        return {
            "final_response": response,
            "synthesis_result": {
                "status": status,
                "provider": result.provider,
                "model": result.model,
                "answer": result.text,
                "usage": dict(result.usage or {}),
                "finish_reason": result.finish_reason,
                "latency_ms": int(result.latency_ms or 0),
            },
            "workflow_status": status,
            "limitation_reasons": limitation_reasons,
            "next_edge": "memory",
        }

    @staticmethod
    def _deterministic(answer: str, model: str) -> LLMProviderResult:
        return LLMProviderResult(
            text=answer,
            provider="deterministic",
            model=model,
            deployment="deterministic",
        )

    @staticmethod
    def _capability_status(state: InvestigationState, capability: str) -> str:
        matches = [
            item.status
            for item in state.get("tool_results") or []
            if (
                item.source_capability.startswith(capability)
                if capability.endswith(".")
                else item.source_capability == capability
            )
        ]
        if not matches:
            return "skipped"
        return matches[0] if len(set(matches)) == 1 else "partial"

    @staticmethod
    def _baseline_capture_rejection_reason(
        task: Any,
        results: tuple[Any, ...],
        identity: Any,
        review: Any,
    ) -> str:
        if getattr(task, "evidence_mode", "normal") not in {"normal", "current_verification", "verify_if_stale"}:
            return "ineligible_evidence_mode"
        if identity is None or not getattr(identity, "user_id", None):
            return "missing_owner_identity"
        if getattr(review, "outcome", None) != "sufficient":
            return "evidence_review_not_sufficient"
        operational = tuple(
            item
            for item in results
            if item.source_capability in {"asset.get_profile", "asset.get_detection"}
            or item.source_capability.startswith("graph.")
        )
        if not operational:
            return "no_operational_evidence"
        if any(item.status != "ok" for item in operational):
            return "operational_evidence_not_successful"
        if any(item.completeness != "complete" for item in operational):
            return "operational_evidence_incomplete"
        if any(item.truncated or item.projection_truncated for item in operational):
            return "operational_evidence_truncated"
        if any(not item.source_payload_complete or not item.projection_usable for item in operational):
            return "operational_projection_unusable"
        receipt_results = tuple(
            item for item in operational if getattr(item, "evidence_receipt", None) is not None
        )
        if any(
            receipt.status != "ok"
            or receipt.completeness != "complete"
            or receipt.truncated
            or receipt.projection_truncated
            or not receipt.source_payload_complete
            or not receipt.projection_usable
            for item in receipt_results
            for receipt in (item.evidence_receipt,)
        ):
            return "operational_receipt_incomplete"
        if any(
            item.evidence_receipt is None and not item.context_included
            for item in operational
        ):
            return "operational_evidence_context_excluded"
        return ""

    def update_memory(self, state: InvestigationState) -> dict[str, Any]:
        if (state.get("memory_update_result") or {}).get("completed"):
            return {"next_edge": "terminal"}
        task = state["task"]
        results = state.get("tool_results") or []
        baseline_results = state.get("baseline_results") or results
        synthesis = state["synthesis_result"]
        context_key = state.get("memory_context_key") or MemoryContextKey.from_task(task)
        pending_facts = tuple(state.get("pending_working_facts") or ())
        identity = state.get("request_identity")
        baseline_written = False
        baseline_rejection_reason = self._baseline_capture_rejection_reason(
            task,
            tuple(baseline_results),
            identity,
            state.get("review_decision"),
        )
        if not self.settings.chat_store_history:
            baseline_rejection_reason = "working_memory_disabled"
        if pending_facts:
            self.service.memory_store.upsert_working_facts(
                state["session_id"],
                context_key,
                pending_facts,
                request_id=state["request_id"],
            )
        if self.settings.chat_store_history:
            self.service.memory_store.record_turn(
                state["session_id"],
                state["message"].strip(),
                synthesis["answer"],
                context_key,
                providers=tuple(
                    dict.fromkeys(
                        item.provider or item.source_capability.split(".", 1)[0]
                        for item in results
                    )
                ),
                limitations=tuple(state["evidence_pack"].limitations),
                scope=task.scope,
                request_id=state["request_id"],
                mutate_operational_episode=bool(
                    getattr(state.get("turn_policy"), "operational_state_mutation_allowed", True)
                ),
            )
            if not baseline_rejection_reason:
                baseline = investigation_baseline_from_results(
                    tuple(baseline_results),
                    owner_id=str(identity.user_id),
                    source_request_id=state["request_id"],
                    scope=task.scope,
                    required_capabilities=task.required_capabilities,
                )
                if baseline is not None:
                    baseline_written = self.service.memory_store.set_investigation_baseline(
                        state["session_id"],
                        context_key,
                        baseline,
                        request_id=state["request_id"],
                    )
                    if not baseline_written:
                        baseline_rejection_reason = "working_episode_rejected_baseline"
                else:
                    baseline_rejection_reason = "normalized_projection_set_ineligible"
        if baseline_rejection_reason:
            logger.info(
                "event=investigation_baseline_rejected request_id=%s entity=%s reason=%s",
                state["request_id"],
                ",".join(context_key.entities) or "none",
                baseline_rejection_reason,
            )
        previous = state["active_entity_state"]
        derived_structured_context = structured_query_context_from_state(state)
        structured_query_context = (
            derived_structured_context
            or getattr(previous, "structured_query_context", None)
        )
        structured_query_lineage = tuple(
            getattr(previous, "structured_query_lineage", ()) or ()
        )
        if derived_structured_context is not None:
            reference_kind = getattr(
                getattr(state.get("routing_result"), "structured_result_reference", None),
                "kind",
                "none",
            )
            if reference_kind == "set_query" and structured_query_context is not None:
                candidates = [*structured_query_lineage]
                prior_context = getattr(previous, "structured_query_context", None)
                if prior_context is not None and not any(
                    item.result_fingerprint == prior_context.result_fingerprint
                    for item in candidates
                ):
                    candidates.append(prior_context)
                candidates.append(derived_structured_context)
                unique = list({item.result_fingerprint: item for item in candidates}.values())
                structured_query_lineage = tuple(
                    unique if len(unique) <= 3 else (unique[0], unique[-2], unique[-1])
                )
            else:
                structured_query_lineage = (derived_structured_context,)
        if structured_query_context is not getattr(previous, "structured_query_context", None):
            logger.info(
                "event=structured_query_context_written request_id=%s mode=%s "
                "ref_count=%s graph_version_present=%s continuity_truncated=%s",
                state["request_id"],
                structured_query_context.mode,
                len(structured_query_context.result_refs),
                bool(structured_query_context.active_graph_version),
                structured_query_context.continuity_truncated,
            )
        resolved = state["resolved_entities"]
        route = state["routing_result"]
        active_entities = previous.active_entities
        active_ip = previous.active_ip
        last_resolved = previous.last_resolved_entities
        timeline = previous.entity_timeline
        turn_policy = state.get("turn_policy")
        transition = getattr(turn_policy, "episode_transition", "switch")
        operational_state_mutation_allowed = bool(
            getattr(turn_policy, "operational_state_mutation_allowed", True)
        )
        detached_without_target = (
            transition == "detach"
            and getattr(turn_policy, "operation", "") != "topic_detach"
            and not (resolved.status == "resolved" and bool(resolved.entities))
        )
        if detached_without_target:
            # A detached general turn with no explicit replacement clears the
            # cursor so later resolution cannot inherit stale asset context.
            active_entities = ()
            active_ip = None
            last_resolved = ()
        can_update = (
            operational_state_mutation_allowed
            and (
                not state.get("require_baseline_for_operational_mutation", False)
                or baseline_written
            )
            and
            resolved.status == "resolved"
            and bool(resolved.entities)
            and route.intent
            in {
                "asset_investigation",
                "graph_neighbors",
                "graph_relationships",
                "graph_path",
                "graph_followup",
            }
            and not resolved.reference_suppressed
            and transition != "detach"
        )
        if can_update:
            values = tuple(item.value for item in resolved.entities)
            last_resolved = values
            active_entities = values
            active_ip = values[0] if len(values) == 1 else None
            operation = getattr(state.get("turn_policy"), "operation", "new_task")
            if operation not in {"memory_recall", "memory_write"} and (
                not timeline or timeline[-1].ordered_entity_ids != values
            ):
                repository = getattr(self.service.memory_store, "repository", None)
                working = (
                    repository.get_working(state["session_id"])
                    if repository is not None
                    else None
                )
                timeline = (
                    *timeline,
                    EntityVisit(
                        sequence=(timeline[-1].sequence + 1) if timeline else 1,
                        ordered_entity_ids=values,
                        task_family=getattr(task, "intent", route.intent),
                        episode_id=working.episode_id if working is not None else "",
                    ),
                )
        provider_names = tuple(
            dict.fromkeys(
                item.provider or item.source_capability.split(".", 1)[0]
                for item in results
                if item.status in {"ok", "not_found", "partial"}
            )
        )
        new_state = SessionRoutingState(
            active_ip=active_ip,
            active_entities=active_entities,
            last_resolved_entities=last_resolved,
            previous_entity_count=(
                0 if detached_without_target
                else len(resolved.entities) if can_update
                else previous.previous_entity_count
            ),
            previous_entity_mode=(
                "none" if detached_without_target
                else resolved.entity_mode if can_update
                else previous.previous_entity_mode
            ),
            last_provider=("combined" if len(provider_names) > 1 else provider_names[0] if provider_names else previous.last_provider),
            last_providers=provider_names or previous.last_providers,
            previous_intent=route.intent if provider_names else previous.previous_intent,
            previous_scope=route.scope if provider_names else previous.previous_scope,
            previous_direction=route.direction if provider_names else previous.previous_direction,
            previous_depth=route.depth if provider_names else previous.previous_depth,
            previous_requires_detection=route.use_detection if provider_names else previous.previous_requires_detection,
            previous_requires_asset_profile=route.use_asset_profile if provider_names else previous.previous_requires_asset_profile,
            last_plan_id=state["execution_plan"].plan_id,
            last_review_outcome=state["review_decision"].outcome,
            last_evidence_ids=tuple(item.step_id for item in results if item.step_id),
            last_capability_statuses=tuple(f"{item.source_capability}:{item.status}" for item in results),
            entity_timeline=timeline,
            structured_query_context=structured_query_context,
            structured_query_lineage=structured_query_lineage,
        )
        logger.info(
            "event=operational_state_update request_id=%s "
            "operational_state_mutation_allowed=%s active_entities_before=%s "
            "active_entities_after=%s active_pair_before=%s active_pair_after=%s "
            "episode_transition=%s",
            state["request_id"],
            operational_state_mutation_allowed,
            ",".join(previous.active_entities) or "none",
            ",".join(new_state.active_entities) or "none",
            ",".join(previous.active_entities) if len(previous.active_entities) == 2 else "none",
            ",".join(new_state.active_entities) if len(new_state.active_entities) == 2 else "none",
            bool(getattr(state.get("conversation_snapshot"), "episode_transition", False)),
        )
        if state.get("require_baseline_for_operational_mutation", False) and not baseline_written:
            logger.info(
                "event=operational_state_update_deferred request_id=%s "
                "reason=focal_baseline_unavailable active_entities_preserved=%s",
                state["request_id"],
                ",".join(previous.active_entities) or "none",
            )
        if self.settings.chat_store_history and operational_state_mutation_allowed:
            self.service.memory_store.compact_if_needed(
                state["session_id"],
                self.settings,
                new_state,
                route=route,
                request_id=state["request_id"],
            )
        self.service.routing_state_store.set(state["session_id"], new_state)
        if identity is not None:
            self.service.persist_thread_continuity(identity, new_state)
            self.service.persist_completed_local_turn(
                identity,
                user_content=state["message"].strip(),
                assistant_content=synthesis["answer"],
            )
        proposed_count = self._propose_long_term_candidates(state)
        working_fact_write_count = len(pending_facts)
        thread_state_persistence_attempted = identity is not None
        logger.info(
            "event=memory_update_completed request_id=%s memory_write_count=%s "
            "working_fact_write_count=%s ltm_candidate_processed_count=%s "
            "thread_state_persistence_attempted=%s tool_count=%s episode_transition=%s",
            state["request_id"],
            working_fact_write_count,
            working_fact_write_count,
            proposed_count,
            thread_state_persistence_attempted,
            len(results),
            bool(getattr(state.get("conversation_snapshot"), "episode_transition", False)),
        )
        return {
            "active_entity_state": new_state,
            "memory_update_result": {
                "completed": True,
                "request_id": state["request_id"],
                "memory_write_count": working_fact_write_count,
                "working_fact_write_count": working_fact_write_count,
                "long_term_candidate_count": proposed_count,
                "ltm_candidate_processed_count": proposed_count,
                "thread_state_persistence_attempted": thread_state_persistence_attempted,
                "investigation_baseline_write_count": int(baseline_written),
            },
            "terminal": True,
            "completed_at": state.get("updated_at"),
            "next_edge": "terminal",
        }

    def _propose_long_term_candidates(self, state: InvestigationState) -> int:
        coordinator = getattr(self.service, "long_term_memory_coordinator", None)
        identity = state.get("request_identity")
        requirements = state.get("evidence_requirements")
        if (
            coordinator is None
            or identity is None
            or not identity.user_id
            or requirements is None
        ):
            return 0
        created = 0
        for result in state.get("tool_results") or ():
            if result.structured_asset_set is not None:
                continue
            refs = evidence_refs_from_validated_result(
                requirements,
                result,
                existing_refs=(result.step_id,) if result.step_id else (),
            )
            statement = structured_memory_statement(result, refs)
            if statement is None:
                continue
            try:
                is_product = result.source_capability in {"asset.get_profile", "asset.get_detection"}
                candidate = LongTermMemoryRecord.candidate(
                    memory_type="validated_finding",
                    user_id=identity.user_id,
                    entity_ids=result.entities,
                    statement=statement,
                    source_request_id=identity.request_id,
                    source_conversation_id=identity.thread_key,
                    evidence_refs=refs,
                    provenance_category="product" if is_product else "investigation",
                    epistemic_status="source_validated" if is_product else "candidate",
                )
                if hasattr(coordinator, "process_candidate"):
                    lifecycle = coordinator.process_candidate(
                        candidate,
                        result,
                        request_id=state["request_id"],
                    )
                    promotion_action = lifecycle.decision.action
                    promotion_reason = lifecycle.decision.reason_code
                    final_status = lifecycle.memory.status
                else:
                    stored = coordinator.create_candidate(candidate)
                    promotion_action = "keep_candidate"
                    promotion_reason = "coordinator_lifecycle_not_available"
                    final_status = stored.status
            except (RuntimeError, ValueError, OSError) as exc:
                logger.warning(
                    "event=long_term_memory_candidate_failed request_id=%s capability=%s error_type=%s",
                    state["request_id"],
                    result.source_capability,
                    type(exc).__name__,
                )
                continue
            created += 1
            logger.info(
                "event=long_term_memory_candidate_processed request_id=%s capability=%s "
                "evidence_class_count=%s promotion_action=%s promotion_reason=%s final_status=%s",
                state["request_id"],
                result.source_capability,
                sum(ref.startswith("evidence_class_") for ref in refs),
                promotion_action,
                promotion_reason,
                final_status,
            )
        return created

    def clarification_response(self, state: InvestigationState) -> dict[str, Any]:
        clarification = state.get("clarification") or {}
        answer = str(clarification.get("answer") or "Please clarify the requested entities.")
        if self.stream_sink is not None:
            self.stream_sink(LLMStreamEvent("answer_delta", text=answer))
        return {
            "final_response": {
                "session_id": state["session_id"],
                "answer": answer,
                "provider": "deterministic",
                "model": "clarification-guard",
                "_warnings": [str(clarification.get("code") or "clarification_required")],
            },
            "workflow_status": "clarification_required",
            "terminal": True,
            "next_edge": "terminal",
        }

    def safe_failure_response(self, state: InvestigationState) -> dict[str, Any]:
        metadata = state.get("failure_metadata") or {}
        code = str(metadata.get("safe_error_code") or "workflow_safe_failure")
        if code == "out_of_scope":
            answer = (
                "I can help with cybersecurity, network and asset intelligence, incident response, "
                "threat analysis, and Soorin platform questions. That request is outside this scope."
            )
            model = "scope-guard"
            status = "completed"
        elif code == "semantic_router_technical_failure":
            answer = (
                "I couldn’t safely determine the requested cybersecurity workflow because routing "
                "encountered a technical problem. Please try the request again."
            )
            model = "routing-technical-guard"
            status = "partial_failure"
        else:
            answer = "I cannot safely complete this request because workflow validation failed. No unsupported result was generated."
            model = "workflow-safety-guard"
            status = "failed"
        if self.stream_sink is not None:
            self.stream_sink(LLMStreamEvent("answer_delta", text=answer))
        return {
            "final_response": {
                "session_id": state["session_id"],
                "answer": answer,
                "provider": "deterministic",
                "model": model,
                "_warnings": [code],
            },
            "workflow_status": status,
            "terminal": True,
            "next_edge": "terminal",
        }

    def apply_clarification(self, state: InvestigationState, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            clarification = str(value.get("message") or value.get("answer") or "").strip()
        else:
            clarification = str(value or "").strip()
        if not clarification:
            return {
                "workflow_status": "clarification_required",
                "next_edge": "respond",
            }
        original = state.get("original_message") or state.get("message") or ""
        return {
            "message": f"{original}\nClarification: {clarification}",
            "workflow_status": "running",
            "terminal": False,
            "clarification": {},
            "resumed": True,
            "next_edge": "resume",
        }
