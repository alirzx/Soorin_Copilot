"""Request-scoped implementations for the durable Copilot workflow nodes."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any

from src.core.agent.contracts import InvestigationState
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
from src.core.agent.task_mapping import (
    compile_direct_plan,
    compile_supplemental_plan,
    derive_request_constraints,
    evidence_mode_from_request,
    task_spec_from_route,
)
from src.core.agent.specialists import AssetInvestigationSpecialist, GraphAnalysisSpecialist
from src.core.context import ContextComposer, normalize_intent_route
from src.core.context.intent import (
    SECURITY_ANALYSIS_WORDS,
    resolution_from_materialized_decision,
)
from src.core.context.models import RouteDecision, approx_tokens
from src.core.context.compaction import (
    current_evidence_projections,
    historical_baseline_projections,
)
from src.core.context.synthesizer_prompt import SynthesizerPromptBuilder
from src.core.copilot.fallback_answer import build_evidence_fallback_answer
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent
from src.core.llm.token_estimator import TokenEstimator
from src.core.memory.episodes import MemoryContextKey
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
        resolution = self.service.entity_resolver.resolve(
            state["message"].strip(),
            state.get("ui_context"),
            routing_state,
            recent_messages=recent,
            request_id=state["request_id"],
        )
        constraints = derive_request_constraints(state["message"])
        pending_facts = extract_working_facts(state["message"]) if constraints.memory_write else ()
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
        if constraints.memory_only:
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
                "event=memory_fast_path_selected request_id=%s router_bypassed=true entity_count=%s working_fact_count=%s",
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
        if decision.fallback_used:
            route_entities = entities
            route = self.service.fallback_router.route(
                state["message"].strip(),
                entities,
                routing_state,
                fallback_reason=decision.fallback_reason or decision.error_reason or "router_failed",
                request_id=state["request_id"],
            )
            route = replace(
                route,
                semantic_router_called=decision.router_called,
                semantic_router_latency_ms=decision.latency_ms,
                semantic_router_retry_count=decision.retry_count,
                semantic_router_finish_reason=decision.finish_reason,
                semantic_router_content_present=decision.content_present,
                semantic_router_error=decision.error_reason,
                fallback_used=True,
                fallback_reason=decision.fallback_reason or decision.error_reason,
            )
        else:
            route_entities = resolution_from_materialized_decision(decision, entities)
            route = normalize_intent_route(decision, route_entities)
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
        return {
            "routing_result": route,
            "resolved_entities": route_entities,
            "routing_fallback_used": bool(decision.fallback_used),
            "next_edge": "validate_task",
        }

    def validate_task(self, state: InvestigationState) -> dict[str, Any]:
        route = state["routing_result"]
        entities = state["resolved_entities"]
        distinct = tuple(dict.fromkeys(route.materialized_entities))
        if len(distinct) > self.settings.agent_max_entities:
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
        planner_selected = (
            task.workflow_mode == "multi_step"
            and bool((state.get("request_constraints") or derive_request_constraints(state["message"])).allow_live)
            and self.settings.planner_enabled
        )
        requirements = self.evidence_requirement_policy.derive(task)
        selection = state.get("long_term_memory_selection")
        memories = tuple(getattr(selection, "memories", ()) or ())
        decisions = self.memory_sufficiency_gate.evaluate(requirements, memories)
        gap_plan = build_gap_plan(requirements, decisions)
        log_gap_plan(state["request_id"], gap_plan)
        return {
            "task": task,
            "workflow_mode": task.workflow_mode,
            "planner_called": planner_selected,
            "memory_context_key": MemoryContextKey.from_task(task),
            "evidence_requirements": requirements,
            "evidence_gap_plan": gap_plan,
            "memory_tool_results": [],
            "next_edge": "planner" if planner_selected else "direct",
        }

    @staticmethod
    def _clarification(answer: str, code: str) -> dict[str, Any]:
        return {
            "workflow_status": "clarification_required",
            "terminal": True,
            "clarification": {"answer": answer, "code": code},
            "next_edge": "clarification",
        }

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
        )
        return {"evidence_pack": pack, "next_edge": "review_retrieval"}

    def review_retrieval(self, state: InvestigationState) -> dict[str, Any]:
        allow = (
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
        snapshot = (
            self.service.memory_store.prepare_for_model(
                state["session_id"],
                self.settings,
                state["active_entity_state"],
                context_key=context_key,
                request_id=state["request_id"],
                long_term_memories=long_term_memories,
            )
            if self.settings.chat_store_history or long_term_memories
            else None
        )
        history = list(snapshot.messages if snapshot else [])
        preliminary_task_context = self.synthesizer_prompt_builder.build_context(
            task,
            tuple(state.get("tool_results") or ()),
            snapshot=snapshot,
            long_term_selection=long_term_selection,
            review=state.get("review_decision"),
            request_constraints=state.get("request_constraints"),
            accepted_working_fact_count=len(state.get("pending_working_facts") or ()),
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
        output_reservation = estimator.output_reservation(task.detail_level, request.max_tokens)
        base_messages = list(preliminary_prompt.messages)
        base_estimate = estimator.estimate_messages(base_messages)
        identity = state.get("request_identity")
        current_projections = current_evidence_projections(
            tuple(state.get("tool_results") or ()),
            owner_id=str(getattr(identity, "user_id", "") or ""),
        )
        historical_baselines = historical_baseline_projections(long_term_memories)
        dynamic_context = self.context_composer.compose(
            package,
            request_id=state["request_id"],
            base_input_tokens=base_estimate.calibrated_tokens,
            reserved_output_tokens=output_reservation,
            current_projections=current_projections,
            historical_baselines=historical_baselines,
        )
        results = apply_context_inclusion(
            list(state.get("tool_results") or []),
            self.context_composer.last_inclusion,
        )
        pack = self.service.evidence_reviewer.build_pack(
            task,
            results,
            plan=state["execution_plan"],
            request_id=state["request_id"],
            trace_id=state["trace_id"],
            review=state["review_decision"],
            supplemental_history=tuple(
                (state.get("supplemental_retrieval_state") or {}).get("history") or ()
            ),
        )
        task_context = self.synthesizer_prompt_builder.build_context(
            task,
            tuple(results),
            snapshot=snapshot,
            long_term_selection=long_term_selection,
            review=state.get("review_decision"),
            request_constraints=state.get("request_constraints"),
            accepted_working_fact_count=len(state.get("pending_working_facts") or ()),
            delta_contexts=self.context_composer.last_delta_contexts,
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
        if context_review.get("decision") == "blocked":
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
            request = state["synthesis_request"]
            try:
                if self.stream_sink is not None:
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

    def update_memory(self, state: InvestigationState) -> dict[str, Any]:
        if (state.get("memory_update_result") or {}).get("completed"):
            return {"next_edge": "terminal"}
        task = state["task"]
        results = state.get("tool_results") or []
        synthesis = state["synthesis_result"]
        context_key = state.get("memory_context_key") or MemoryContextKey.from_task(task)
        pending_facts = tuple(state.get("pending_working_facts") or ())
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
            )
        previous = state["active_entity_state"]
        resolved = state["resolved_entities"]
        route = state["routing_result"]
        active_entities = previous.active_entities
        active_ip = previous.active_ip
        last_resolved = previous.last_resolved_entities
        can_update = (
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
        )
        if can_update:
            values = tuple(item.value for item in resolved.entities)
            last_resolved = values
            active_entities = values
            active_ip = values[0] if len(values) == 1 else None
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
            previous_entity_count=len(resolved.entities) if can_update else previous.previous_entity_count,
            previous_entity_mode=resolved.entity_mode if can_update else previous.previous_entity_mode,
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
        )
        self.service.routing_state_store.set(state["session_id"], new_state)
        identity = state.get("request_identity")
        if identity is not None:
            self.service.persist_thread_continuity(identity, new_state)
            self.service.persist_completed_local_turn(
                identity,
                user_content=state["message"].strip(),
                assistant_content=synthesis["answer"],
            )
        proposed_count = self._propose_long_term_candidates(state)
        logger.info(
            "event=memory_update_completed request_id=%s memory_write_count=%s tool_count=%s episode_transition=%s persistence_attempted=%s",
            state["request_id"],
            len(pending_facts),
            len(results),
            bool(getattr(state.get("conversation_snapshot"), "episode_transition", False)),
            identity is not None,
        )
        return {
            "active_entity_state": new_state,
            "memory_update_result": {
                "completed": True,
                "request_id": state["request_id"],
                "long_term_candidate_count": proposed_count,
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
            refs = evidence_refs_from_validated_result(
                requirements,
                result,
                existing_refs=(result.step_id,) if result.step_id else (),
            )
            statement = structured_memory_statement(result, refs)
            if statement is None:
                continue
            try:
                coordinator.create_candidate(LongTermMemoryRecord.candidate(
                    memory_type="validated_finding",
                    user_id=identity.user_id,
                    entity_ids=result.entities,
                    statement=statement,
                    source_request_id=identity.request_id,
                    source_conversation_id=identity.thread_key,
                    evidence_refs=refs,
                    provenance_category="investigation",
                ))
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
                "event=long_term_memory_candidate_proposed request_id=%s capability=%s evidence_class_count=%s",
                state["request_id"],
                result.source_capability,
                sum(ref.startswith("evidence_class_") for ref in refs),
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
        answer = "I cannot safely complete this request because workflow validation failed. No unsupported result was generated."
        if self.stream_sink is not None:
            self.stream_sink(LLMStreamEvent("answer_delta", text=answer))
        return {
            "final_response": {
                "session_id": state["session_id"],
                "answer": answer,
                "provider": "deterministic",
                "model": "workflow-safety-guard",
                "_warnings": [str(metadata.get("safe_error_code") or "workflow_safe_failure")],
            },
            "workflow_status": "failed",
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
