"""Baseline Copilot chat service."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from queue import SimpleQueue
from threading import Thread
from typing import Any
from uuid import uuid4

from src.config.settings import Settings
from src.core.agent.registry import build_capability_registry
from src.core.agent.task_mapping import bounded_plan_placeholder, task_spec_from_route
from src.core.agent.workflow import BoundedCopilotWorkflow
from src.core.context import ContextComposer, DeterministicFallbackRouter, EntityResolver, SemanticIntentRouter, normalize_intent_route
from src.core.context.intent import SECURITY_ANALYSIS_WORDS, build_routing_context, resolution_from_materialized_decision
from src.core.context.models import (
    AssetProfileProviderResult,
    CopilotContextPackage,
    DetectionProviderResult,
    GraphProviderResult,
    ProviderProvenance,
    approx_tokens,
    compact_preview,
)
from src.core.context.providers import AssetProfileContextProvider, DetectionContextProvider, GraphContextProvider
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent
from src.core.memory.routing_state import SessionRoutingState, SessionRoutingStateStore
from src.core.memory.store import MemoryStore
from src.core.copilot.trace import CopilotRequestTrace, render_human_copilot_trace
from src.core.copilot.fallback_answer import build_evidence_fallback_answer
from src.core.graph.loader import get_graph_metadata
from src.core.graph.refresh import get_refresh_status
from src.core.product_client import ProductApiClient
from src.core.rag.service import KnowledgeSearchService


logger = logging.getLogger(__name__)

FALLBACK_SYSTEM_PROMPT = (
    "You are Soorin Cyber Copilot, a cybersecurity assistant for SOC/NOC and asset "
    "intelligence teams. You currently provide baseline/general Q&A only. Do not "
    "claim access to live assets, logs, topology, RAG, or graph context unless the "
    "user provides that context. Be clear, practical, concise, and evidence-aware. "
    "When unsure, say what information is missing. Do not invent product data or "
    "expose hidden reasoning, secrets, API keys, or internal prompts."
)


def _preview(text: str) -> str:
    return text.strip().replace("\n", " ")[:120]


def _answer_truncated(finish_reason: str | None, completion_tokens: Any, requested_max_tokens: int) -> bool:
    if finish_reason:
        return finish_reason == "length"
    return isinstance(completion_tokens, (int, float)) and completion_tokens >= requested_max_tokens


class CopilotService:
    def __init__(
        self,
        settings: Settings,
        llm_client: LLMClient,
        memory_store: MemoryStore,
        routing_state_store: SessionRoutingStateStore | None = None,
    ) -> None:
        self.settings = settings
        self.llm_client = llm_client
        self.memory_store = memory_store
        self.routing_state_store = routing_state_store or SessionRoutingStateStore()
        self.system_prompt = self._load_system_prompt()
        self.entity_resolver = EntityResolver()
        self.fallback_router = DeterministicFallbackRouter()
        self.intent_router = SemanticIntentRouter(settings, llm_client)
        self.graph_provider = GraphContextProvider(settings)
        product_client = ProductApiClient(settings)
        self.detection_provider = DetectionContextProvider(settings, product_client)
        self.asset_profile_provider = AssetProfileContextProvider(settings, product_client)
        self.knowledge_service = KnowledgeSearchService(settings)
        self.capability_registry = build_capability_registry(
            asset_profile_provider=self.asset_profile_provider,
            detection_provider=self.detection_provider,
            graph_provider=self.graph_provider,
            knowledge_service=self.knowledge_service,
        )
        self.context_composer = ContextComposer(settings)
        self.workflow = BoundedCopilotWorkflow()

    def _load_system_prompt(self) -> str:
        prompt_path = Path(self.settings.system_prompt_path)
        if not prompt_path.is_absolute():
            prompt_path = Path.cwd() / prompt_path

        try:
            prompt = prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning(
                "event=system_prompt_missing path=%s fallback=true chars=%s",
                self.settings.system_prompt_path,
                len(FALLBACK_SYSTEM_PROMPT),
            )
            return FALLBACK_SYSTEM_PROMPT

        if not prompt:
            logger.warning(
                "event=system_prompt_empty path=%s fallback=true chars=%s",
                self.settings.system_prompt_path,
                len(FALLBACK_SYSTEM_PROMPT),
            )
            return FALLBACK_SYSTEM_PROMPT

        logger.info(
            "event=system_prompt_loaded path=%s chars=%s",
            self.settings.system_prompt_path,
            len(prompt),
        )
        return prompt

    @staticmethod
    def _trace_product_results(
        trace: CopilotRequestTrace,
        section: str,
        results: list[Any],
        cache_enabled: bool,
    ) -> None:
        """Record transport and size telemetry without copying product content."""
        trace.put(
            section,
            status=", ".join(f"{item.ip}:{item.status}" for item in results) or "skipped",
            target_ip=", ".join(item.ip for item in results),
            result_count=len(results),
            asset_found=", ".join(str(item.asset_found) for item in results),
            http_status=", ".join(str(item.http_status or "") for item in results),
            cache_enabled=cache_enabled,
            cache_hit=", ".join(str(item.cache_hit).lower() for item in results),
            cache_age_seconds=", ".join(str(item.cache_age_seconds if item.cache_age_seconds is not None else "") for item in results),
            stale=", ".join(str(item.stale).lower() for item in results),
            provider_latency_ms=sum(item.latency_ms for item in results),
            raw_json_bytes=sum(item.raw_json_bytes for item in results),
            raw_json_chars=sum(item.raw_json_chars for item in results),
            raw_json_approx_tokens=sum(item.raw_json_approx_tokens for item in results),
            raw_top_level_key_count=sum(item.raw_top_level_key_count for item in results),
            raw_payload_present=all(item.raw_payload_present for item in results) if results else False,
            full_payload_fetched=all(item.full_payload_fetched for item in results) if results else False,
            full_payload_included=False,
            context_chars=0,
            context_approx_tokens=0,
            context_truncated=False,
            context_truncation_reason="",
            source=", ".join(item.provenance.source for item in results if item.provenance),
            fetched_at=", ".join(item.fetched_at or "" for item in results),
            error_type=", ".join(item.error_type or "" for item in results),
            safe_error=", ".join(item.safe_error or "" for item in results),
        )

    @staticmethod
    def _combined_status(results: list[Any]) -> str:
        if not results:
            return "skipped"
        statuses = {item.status for item in results}
        if len(statuses) == 1:
            return next(iter(statuses))
        return "partial"

    def _update_product_inclusion_trace(
        self,
        trace: CopilotRequestTrace,
        section: str,
        provider_name: str,
        results: list[Any],
        context_text: str,
    ) -> None:
        fetched = [item for item in results if item.status in {"available", "not_found"} and item.raw_payload_present]
        inclusion = [self.context_composer.last_inclusion.get(f"{provider_name}:{item.ip}", (False, None)) for item in fetched]
        representations = [
            self.context_composer.last_representation.get(f"{provider_name}:{item.ip}", "excluded")
            for item in fetched
        ]
        reasons = [reason for included, reason in inclusion if not included and reason]
        if any(representation == "compact" for representation in representations):
            reasons.append("route_aware_compaction")
        trace.put(
            section,
            full_payload_included=bool(fetched)
            and all(included for included, _ in inclusion)
            and all(representation == "full" for representation in representations),
            context_representation=", ".join(representations),
            context_chars=len(context_text),
            context_approx_tokens=approx_tokens(context_text),
            context_truncated=bool(reasons),
            context_truncation_reason=", ".join(dict.fromkeys(reasons)),
        )

    def _stream_final_model(
        self,
        messages: list[dict[str, str]],
        *,
        request_id: str,
        max_tokens: int,
        temperature: float | None,
        top_p: float | None,
        timeout_seconds: int,
        sink: Callable[[LLMStreamEvent], None],
        metrics: dict[str, Any],
    ) -> LLMProviderResult:
        """Collect one final-model stream while forwarding safe incremental events."""
        deployment = self.settings.deployment_for_purpose("chat")
        started = time.perf_counter()
        answer_parts: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        done_data: dict[str, Any] = {}

        try:
            stream_chat = getattr(self.llm_client, "stream_chat", None)
            if not callable(stream_chat):
                raise LLMError(
                    "Selected LLM client does not support streaming.",
                    reason="provider_stream_not_supported",
                )
            for event in stream_chat(
                messages,
                request_id=request_id,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                timeout_seconds=timeout_seconds,
                purpose="chat",
            ):
                metrics["stream_chunk_count"] += 1
                if event.type == "reasoning_delta" and event.text:
                    metrics["streaming_used"] = True
                    metrics["reasoning_chunk_count"] += 1
                    if metrics["first_reasoning_chunk_latency_ms"] is None:
                        metrics["first_reasoning_chunk_latency_ms"] = int(
                            (time.perf_counter() - started) * 1000
                        )
                    if self.settings.llm_expose_reasoning:
                        sink(event)
                elif event.type == "answer_delta" and event.text:
                    metrics["streaming_used"] = True
                    metrics["answer_chunk_count"] += 1
                    if metrics["first_answer_chunk_latency_ms"] is None:
                        metrics["first_answer_chunk_latency_ms"] = int(
                            (time.perf_counter() - started) * 1000
                        )
                    answer_parts.append(event.text)
                    sink(event)
                elif event.type == "usage":
                    usage = dict(event.data)
                    sink(event)
                elif event.type == "done":
                    done_data = dict(event.data)
                    finish_reason = done_data.get("finish_reason")
                    usage = dict(done_data.get("usage") or usage)
                elif event.type == "error":
                    raise LLMError(
                        "The main-model stream failed.",
                        reason="provider_stream_error",
                    )
        except LLMError as exc:
            metrics["stream_error_type"] = str(exc.details.get("error_type") or exc.reason)
            if answer_parts:
                logger.warning(
                    "event=main_model_stream_interrupted request_id=%s deployment=%s provider=%s model=%s answer_chunks=%s error_type=%s fallback=false",
                    request_id,
                    deployment.name,
                    deployment.provider_type,
                    deployment.model,
                    len(answer_parts),
                    metrics["stream_error_type"],
                )
                raise LLMError(
                    "The Copilot response stream was interrupted.",
                    reason="provider_stream_interrupted",
                    details={
                        "partial_output": True,
                        "error_type": metrics["stream_error_type"],
                    },
                ) from exc
            logger.warning(
                "event=main_model_stream_fallback request_id=%s deployment=%s provider=%s model=%s error_type=%s fallback=non_stream",
                request_id,
                deployment.name,
                deployment.provider_type,
                deployment.model,
                metrics["stream_error_type"],
            )
            result = self.llm_client.chat(
                messages,
                request_id=request_id,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                timeout_seconds=timeout_seconds,
                purpose="chat",
            )
            sink(LLMStreamEvent("answer_delta", text=result.text))
            return result

        if not answer_parts:
            metrics["stream_error_type"] = "provider_stream_empty_answer"
            logger.warning(
                "event=main_model_stream_fallback request_id=%s deployment=%s provider=%s model=%s error_type=%s fallback=non_stream",
                request_id,
                deployment.name,
                deployment.provider_type,
                deployment.model,
                metrics["stream_error_type"],
            )
            result = self.llm_client.chat(
                messages,
                request_id=request_id,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                timeout_seconds=timeout_seconds,
                purpose="chat",
            )
            sink(LLMStreamEvent("answer_delta", text=result.text))
            return result

        stream_terminated = bool(done_data.get("stream_terminated"))
        if not stream_terminated and not finish_reason:
            metrics["stream_error_type"] = "provider_stream_incomplete"
            raise LLMError(
                "The Copilot response stream ended before completion.",
                reason="provider_stream_interrupted",
                details={"partial_output": True, "error_type": metrics["stream_error_type"]},
            )

        metrics["stream_completed"] = True
        return LLMProviderResult(
            text="".join(answer_parts),
            provider=str(done_data.get("provider") or deployment.provider_type),
            model=str(done_data.get("model") or deployment.model),
            deployment=str(done_data.get("deployment") or deployment.name),
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=int(done_data.get("latency_ms") or ((time.perf_counter() - started) * 1000)),
            status_code=done_data.get("status_code"),
            endpoint=deployment.safe_host,
            reasoning_present=metrics["reasoning_chunk_count"] > 0,
            reasoning_exposed=bool(
                metrics["reasoning_chunk_count"] and self.settings.llm_expose_reasoning
            ),
            payload_format="chat_completions_stream",
        )

    def chat(
        self,
        message: str,
        session_id: str | None = None,
        *,
        ui_context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._chat(
            message,
            session_id,
            ui_context=ui_context,
            request_id=request_id,
        )

    def chat_stream(
        self,
        message: str,
        session_id: str | None = None,
        *,
        ui_context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Iterator[LLMStreamEvent]:
        """Run the existing orchestration once and expose final-model events."""
        event_queue: SimpleQueue[LLMStreamEvent | None] = SimpleQueue()

        def run() -> None:
            try:
                result = self._chat(
                    message,
                    session_id,
                    ui_context=ui_context,
                    request_id=request_id,
                    stream_sink=event_queue.put,
                )
                warnings = list(result.pop("_warnings", []))
                event_queue.put(
                    LLMStreamEvent(
                        "done",
                        data={
                            "session_id": result.get("session_id", ""),
                            "provider": result.get("provider", ""),
                            "model": result.get("model", ""),
                            "warnings": warnings,
                        },
                    )
                )
            except LLMError as exc:
                logger.warning(
                    "event=copilot_stream_error request_id=%s reason=%s error_type=%s",
                    request_id or "",
                    exc.reason,
                    str(exc.details.get("error_type") or exc.reason),
                )
                event_queue.put(
                    LLMStreamEvent(
                        "error",
                        message="The Copilot could not complete the streamed response.",
                        data={"reason": exc.reason},
                    )
                )
            except Exception as exc:
                logger.exception(
                    "event=copilot_stream_exception request_id=%s error_type=%s",
                    request_id or "",
                    type(exc).__name__,
                )
                event_queue.put(
                    LLMStreamEvent(
                        "error",
                        message="The Copilot could not complete the streamed response.",
                        data={"reason": "stream_internal_error"},
                    )
                )
            finally:
                event_queue.put(None)

        Thread(target=run, name="copilot-final-stream", daemon=True).start()
        while True:
            event = event_queue.get()
            if event is None:
                break
            yield event

    def _chat(
        self,
        message: str,
        session_id: str | None = None,
        *,
        ui_context: dict[str, Any] | None = None,
        request_id: str | None = None,
        stream_sink: Callable[[LLMStreamEvent], None] | None = None,
    ) -> dict[str, Any]:
        resolved_request_id = request_id or uuid4().hex[:12]
        return self.workflow.run(
            message=message,
            session_id=session_id,
            ui_context=ui_context,
            request_id=resolved_request_id,
            stream_sink=stream_sink,
            direct_executor=self._chat_direct,
        )

    def _chat_direct(
        self,
        message: str,
        session_id: str | None = None,
        *,
        ui_context: dict[str, Any] | None = None,
        request_id: str | None = None,
        stream_sink: Callable[[LLMStreamEvent], None] | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or uuid4().hex[:12]
        session = (session_id or "").strip() or uuid4().hex
        user_text = message.strip()
        request_started = time.perf_counter()
        trace = CopilotRequestTrace(
            request_id=request_id,
            session_id=session,
            message_preview=compact_preview(user_text),
        )
        ui_selected_ip = (ui_context or {}).get("selected_ip") or ""
        trace.put(
            "REQUEST",
            ui_context_present=bool(ui_context),
            ui_selected_ip=ui_selected_ip,
            streaming_requested=stream_sink is not None,
        )

        routing_state = self.routing_state_store.get(session)
        trace.put(
            "ROUTING STATE",
            active_ip_before=routing_state.active_ip or "",
            active_entities_before=", ".join(routing_state.active_entities),
            active_entity_count_before=len(routing_state.active_entities),
            last_provider_before=routing_state.last_provider or "",
            last_providers_before=", ".join(routing_state.last_providers),
            previous_intent=routing_state.previous_intent or "",
            previous_scope=routing_state.previous_scope or "",
            previous_direction=routing_state.previous_direction or "",
            previous_depth=routing_state.previous_depth if routing_state.previous_depth is not None else "",
            previous_requires_detection=routing_state.previous_requires_detection,
            previous_requires_asset_profile=routing_state.previous_requires_asset_profile,
        )
        logger.info(
            "event=session_routing_state_loaded request_id=%s session_id=%s active_ip=%s last_provider=%s",
            request_id,
            session,
            routing_state.active_ip or "",
            routing_state.last_provider or "",
        )
        entities = self.entity_resolver.resolve(user_text, ui_context, routing_state, request_id=request_id)
        trace.put(
            "ENTITY",
            status=entities.status,
            entity_mode=entities.entity_mode,
            entity_count=len(entities.entities),
            value=entities.primary_entity.value if entities.primary_entity else "",
            source=entities.primary_entity.source if entities.primary_entity else "",
            reference_detected=entities.reference_detected,
            reference_type=entities.reference_type or "",
            reference_suppressed=entities.reference_suppressed,
            suppression_reason=entities.suppression_reason or "",
            explicit_candidates=entities.explicit_candidate_count,
            valid_entities=entities.valid_entity_count,
            subnet_constraints=", ".join(entities.subnet_constraints),
            unsupported_constraints=", ".join(entities.unsupported_constraints),
        )
        routing_context = build_routing_context(user_text, entities, routing_state, ui_context=ui_context)
        trace.put("ROUTER INPUT", **routing_context)
        intent_decision = self.intent_router.classify(
            user_text,
            entities,
            routing_state,
            ui_context=ui_context,
            request_id=request_id,
        )
        if intent_decision.fallback_used:
            route_entities = entities
            route = self.fallback_router.route(
                user_text,
                entities,
                routing_state,
                fallback_reason=intent_decision.fallback_reason or intent_decision.error_reason or "router_failed",
                request_id=request_id,
            )
            route = type(route)(
                **{
                    **route.__dict__,
                    "semantic_router_called": intent_decision.router_called,
                    "semantic_router_latency_ms": intent_decision.latency_ms,
                    "semantic_router_retry_count": intent_decision.retry_count,
                    "semantic_router_finish_reason": intent_decision.finish_reason,
                    "semantic_router_content_present": intent_decision.content_present,
                    "semantic_router_error": intent_decision.error_reason,
                    "fallback_used": True,
                    "fallback_reason": intent_decision.fallback_reason or intent_decision.error_reason,
                }
            )
        else:
            route_entities = resolution_from_materialized_decision(intent_decision, entities)
            route = normalize_intent_route(intent_decision, route_entities)

        if SECURITY_ANALYSIS_WORDS.search(user_text) and "security_or_anomaly" not in route.matched_signals:
            route = type(route)(
                **{
                    **route.__dict__,
                    "matched_signals": [*route.matched_signals, "security_or_anomaly"],
                }
            )

        task_spec = task_spec_from_route(route, user_text)
        plan_placeholder = bounded_plan_placeholder(task_spec)
        logger.info(
            "event=agent_task_mapped request_id=%s decision_source=%s workflow_mode=%s capabilities=%s max_steps=%s plan_steps=%s plan_validated=%s planner_called=false",
            request_id,
            task_spec.semantic_decision_source,
            task_spec.workflow_mode,
            ",".join(task_spec.required_capabilities) or "none",
            task_spec.max_steps,
            len(plan_placeholder.steps),
            plan_placeholder.validated,
        )

        router_deployment = self.settings.deployment_for_purpose("intent_router")
        router_repair_deployment = self.settings.deployment_for_purpose("intent_router_repair")
        trace.put(
            "INTENT",
            router_deployment=router_deployment.name,
            router_provider=router_deployment.provider_type,
            router_model=router_deployment.model,
            router_engine=router_deployment.model,
            router_repair_deployment=router_repair_deployment.name if route.semantic_router_retry_count else "",
            router_repair_provider=router_repair_deployment.provider_type if route.semantic_router_retry_count else "",
            router_repair_model=router_repair_deployment.model if route.semantic_router_retry_count else "",
            router_repair_engine=router_repair_deployment.model if route.semantic_router_retry_count else "",
            decision_source=route.decision_source,
            intent=route.intent,
            scope=route.scope,
            direction=route.direction,
            depth=route.depth,
            requires_graph=route.use_graph,
            requires_detection=route.use_detection,
            requires_asset_profile=route.use_asset_profile,
            requires_knowledge=route.use_knowledge,
            entity_binding=route.entity_binding,
            requires_multiple_entities=route.requires_multiple_entities,
            is_followup=route.followup_detected,
            classification_confidence=route.intent_confidence,
            reason=route.reason,
            semantic_router_called=route.semantic_router_called,
            semantic_router_latency_ms=route.semantic_router_latency_ms,
            semantic_router_retry_count=route.semantic_router_retry_count,
            semantic_router_finish_reason=route.semantic_router_finish_reason or "",
            semantic_router_content_present=route.semantic_router_content_present,
            semantic_router_error=route.semantic_router_error or "",
            exhaustive_connections_requested=route.exhaustive_connections_requested,
            fallback_used=route.fallback_used,
            fallback_reason=route.fallback_reason or "",
            route_normalized=route.route_normalized,
            route_normalization_reason=route.route_normalization_reason or "",
            relationship_mode=route.relationship_mode,
        )
        trace.put(
            "ENTITY BINDING",
            requested_entity_binding=route.requested_entity_binding,
            resolved_entity_binding=route.resolved_entity_binding,
            binding_source=route.binding_source,
            binding_available=route.binding_available,
            binding_normalized=route.binding_normalized,
            binding_normalization_reason=route.binding_normalization_reason or "",
            materialized_entity_count=route.materialized_entity_count,
            materialized_entities=", ".join(route.materialized_entities),
            initial_entity_status=entities.status,
            final_entity_status=route_entities.status,
            entity_recovered=entities.status != "resolved" and route_entities.status == "resolved",
            entity_recovery_source=route.binding_source if entities.status != "resolved" and route_entities.status == "resolved" else "",
        )
        trace.put(
            "ROUTING",
            use_graph=route.use_graph,
            use_detection=route.use_detection,
            use_asset_profile=route.use_asset_profile,
            selected_providers=", ".join(
                provider
                for provider, enabled in [
                    ("graph", route.use_graph),
                    ("detection", route.use_detection),
                    ("asset_profile", route.use_asset_profile),
                    ("knowledge", route.use_knowledge),
                ]
                if enabled
            )
            or "none",
            route_reason=route.reason,
            graph_intent=route.graph_intent_detected,
            asset_investigation=route.asset_investigation_detected,
            followup=route.followup_detected,
            matched_signals=", ".join(route.matched_signals),
        )
        logger.info(
            "event=entity_binding_resolved request_id=%s requested_entity_binding=%s resolved_entity_binding=%s binding_source=%s binding_available=%s binding_normalized=%s binding_normalization_reason=%s materialized_entity_count=%s materialized_entities=%s",
            request_id,
            route.requested_entity_binding,
            route.resolved_entity_binding,
            route.binding_source,
            route.binding_available,
            route.binding_normalized,
            route.binding_normalization_reason or "",
            route.materialized_entity_count,
            ",".join(route.materialized_entities),
        )

        graph_result = None
        detection_results: list[DetectionProviderResult] = []
        asset_profile_results: list[AssetProfileProviderResult] = []
        knowledge_result = None
        provenance: list[ProviderProvenance] = []
        limitations: list[str] = []
        response_warnings: list[str] = []
        if route.use_graph and (route.target_entity or route.target_entities):
            try:
                graph_result = self.graph_provider.provide(route.target_entity, route=route, request_id=request_id)
            except Exception as exc:
                logger.warning(
                    "event=graph_context_provider_failed request_id=%s error_type=%s",
                    request_id,
                    type(exc).__name__,
                )
                graph_result = GraphProviderResult(
                    provider="graph",
                    status="unavailable",
                    target_entity=route.target_entity,
                    provenance=ProviderProvenance(source="observed_communication_graph", status="unavailable"),
                    limitations=["Graph evidence was unavailable for this request."],
                    error_reason=type(exc).__name__,
                )
            if graph_result and graph_result.provenance:
                provenance.append(graph_result.provenance)
            if graph_result:
                limitations.extend(graph_result.limitations)
            if graph_result and graph_result.status == "unavailable":
                response_warnings.append("graph_evidence_unavailable")
        product_targets = route.target_entities[:2]
        if route.use_detection:
            for target_entity in product_targets:
                try:
                    result = self.detection_provider.fetch(target_entity.value, request_id, session_id=session)
                except Exception as exc:
                    logger.warning(
                        "event=detection_provider_failed request_id=%s target_ip=%s status=unavailable error_type=%s stale_fallback=false latency_ms=0",
                        request_id,
                        target_entity.value,
                        type(exc).__name__,
                    )
                    result = DetectionProviderResult(
                        provider="detection",
                        status="unavailable",
                        ip=target_entity.value,
                        error_type=type(exc).__name__,
                        safe_error="Detection provider failed.",
                        limitations=["Detection evidence was unavailable for this request."],
                    )
                detection_results.append(result)
                if result.provenance:
                    provenance.append(result.provenance)
                limitations.extend(result.limitations)
                if result.status in {"not_found", "unavailable"}:
                    response_warnings.append(
                        "detection_evidence_not_found" if result.status == "not_found" else "detection_evidence_unavailable"
                    )
        if route.use_asset_profile:
            for target_entity in product_targets:
                try:
                    result = self.asset_profile_provider.fetch(target_entity.value, request_id, session_id=session)
                except Exception as exc:
                    logger.warning(
                        "event=asset_profile_provider_failed request_id=%s target_ip=%s status=unavailable error_type=%s stale_fallback=false latency_ms=0",
                        request_id,
                        target_entity.value,
                        type(exc).__name__,
                    )
                    result = AssetProfileProviderResult(
                        provider="asset_profile",
                        status="unavailable",
                        ip=target_entity.value,
                        error_type=type(exc).__name__,
                        safe_error="Asset Profile provider failed.",
                        limitations=["Asset Profile evidence was unavailable for this request."],
                    )
                asset_profile_results.append(result)
                if result.provenance:
                    provenance.append(result.provenance)
                limitations.extend(result.limitations)
                if result.status in {"not_found", "unavailable"}:
                    response_warnings.append(
                        "asset_profile_not_found" if result.status == "not_found" else "asset_profile_unavailable"
                    )
        if route.use_knowledge:
            knowledge_result = self.knowledge_service.search(
                user_text,
                request_id=request_id,
            )
            limitations.extend(knowledge_result.limitations)
            if knowledge_result.status in {"not_configured", "unavailable", "invalid"}:
                response_warnings.append(f"knowledge_evidence_{knowledge_result.status}")
        graph_context = graph_result.context if graph_result else {}
        graph_metadata = get_graph_metadata()
        refresh_status = get_refresh_status()
        graph_target_ips = [
            str(item)
            for item in (graph_context.get("target_ips", []) if isinstance(graph_context, dict) else [])
            if item
        ]
        graph_target_label = ", ".join(graph_target_ips)
        if not graph_target_label:
            graph_target_label = (
                graph_result.target_entity.value
                if graph_result and graph_result.target_entity
                else graph_context.get("target_ip", "") if isinstance(graph_context, dict) else ""
            )
        self._trace_product_results(trace, "ASSET DETECTION", detection_results, self.settings.detection_cache_enabled)
        self._trace_product_results(trace, "ASSET PROFILE", asset_profile_results, self.settings.detection_cache_enabled)
        trace.put(
            "GRAPH RETRIEVAL",
            status=graph_result.status if graph_result else "skipped",
            target_ip=graph_target_label,
            target_ips=graph_target_label,
            scope=graph_context.get("scope", route.scope) if isinstance(graph_context, dict) else route.scope,
            direction=graph_context.get("direction", route.direction) if isinstance(graph_context, dict) else route.direction,
            depth=graph_context.get("depth", route.depth) if isinstance(graph_context, dict) else route.depth,
            inbound_total=graph_context.get("inbound_total", "") if isinstance(graph_context, dict) else "",
            inbound_retrieved=graph_context.get("inbound_retrieved", graph_context.get("inbound_returned", "")) if isinstance(graph_context, dict) else "",
            outbound_total=graph_context.get("outbound_total", "") if isinstance(graph_context, dict) else "",
            outbound_retrieved=graph_context.get("outbound_retrieved", graph_context.get("outbound_returned", "")) if isinstance(graph_context, dict) else "",
            bidirectional_total=graph_context.get("bidirectional_total", "") if isinstance(graph_context, dict) else "",
            bidirectional_retrieved=graph_context.get("bidirectional_retrieved", graph_context.get("bidirectional_returned", "")) if isinstance(graph_context, dict) else "",
            candidate_node_count=graph_context.get("candidate_node_count", "") if isinstance(graph_context, dict) else "",
            retrieval_node_count=graph_context.get("retrieved_node_count", graph_context.get("returned_node_count", "")) if isinstance(graph_context, dict) else "",
            candidate_edge_count=graph_context.get("candidate_edge_count", "") if isinstance(graph_context, dict) else "",
            retrieval_edge_count=graph_context.get("retrieved_edge_count", graph_context.get("returned_edge_count", "")) if isinstance(graph_context, dict) else "",
            retrieval_truncated=graph_context.get("retrieval_truncated", graph_context.get("truncated", False)) if isinstance(graph_context, dict) else False,
            retrieval_complete=graph_context.get("retrieval_complete", False) if isinstance(graph_context, dict) else False,
            retrieval_truncation_reasons=graph_context.get("retrieval_truncation_reasons", []) if isinstance(graph_context, dict) else [],
            retrieval_truncation_reason=(graph_context.get("retrieval_truncation_reason") or graph_context.get("truncation_reason") or "") if isinstance(graph_context, dict) else "",
            graph_snapshot_version=graph_metadata.get("active_graph_version", ""),
            graph_loaded_at=graph_metadata.get("active_graph_loaded_at", ""),
            graph_source=graph_metadata.get("active_graph_source", ""),
            graph_refresh_last_success_at=refresh_status.get("last_success_at", ""),
            provider_latency_ms=graph_result.latency_ms if graph_result else 0,
            relationship_source_present=graph_context.get("source_present", "") if isinstance(graph_context, dict) else "",
            relationship_target_present=graph_context.get("target_present", "") if isinstance(graph_context, dict) else "",
            relationship_forward_edge=graph_context.get("forward_edge", "") if isinstance(graph_context, dict) else "",
            relationship_reverse_edge=graph_context.get("reverse_edge", "") if isinstance(graph_context, dict) else "",
            relationship_status=graph_context.get("relationship_status", graph_context.get("relationship", "")) if isinstance(graph_context, dict) else "",
            comparison_shared_peer_total=graph_context.get("shared_peer_total", "") if isinstance(graph_context, dict) else "",
            comparison_entity_a_unique_peer_total=graph_context.get("entity_a_unique_peer_total", "") if isinstance(graph_context, dict) else "",
            comparison_entity_b_unique_peer_total=graph_context.get("entity_b_unique_peer_total", "") if isinstance(graph_context, dict) else "",
            requested_scope_complete=graph_context.get("requested_scope_complete", False) if isinstance(graph_context, dict) else False,
            complete_for_user_request=graph_context.get("complete_for_user_request", False) if isinstance(graph_context, dict) else False,
        )

        context_package = CopilotContextPackage(
            entities=route_entities,
            graph=graph_result,
            detections=detection_results,
            asset_profiles=asset_profile_results,
            knowledge=knowledge_result,
            provenance=provenance,
            limitations=limitations,
        )
        provider_statuses = {
            "graph": graph_result.status if graph_result else "skipped",
            "detection": self._combined_status(detection_results),
            "asset_profile": self._combined_status(asset_profile_results),
            "knowledge": knowledge_result.status if knowledge_result else "skipped",
        }
        logger.info(
            "event=provider_statuses request_id=%s graph=%s detection=%s asset_profile=%s knowledge=%s",
            request_id,
            provider_statuses["graph"],
            provider_statuses["detection"],
            provider_statuses["asset_profile"],
            provider_statuses["knowledge"],
        )
        requested_statuses = [
            provider_statuses[name]
            for name, requested in (("graph", route.use_graph), ("detection", route.use_detection), ("asset_profile", route.use_asset_profile), ("knowledge", route.use_knowledge))
            if requested
        ]
        available_statuses = {"available", "ok"}
        if "partial" in requested_statuses or (
            any(status in available_statuses for status in requested_statuses)
            and any(status not in available_statuses for status in requested_statuses)
        ):
            logger.info(
                "event=partial_provider_result request_id=%s graph=%s detection=%s asset_profile=%s knowledge=%s synthesis_continues=true",
                request_id,
                provider_statuses["graph"],
                provider_statuses["detection"],
                provider_statuses["asset_profile"],
                provider_statuses["knowledge"],
            )
        logger.info(
            "event=context_package_created request_id=%s entity_status=%s entity_count=%s graph_status=%s detection_status=%s asset_profile_status=%s knowledge_status=%s provenance_count=%s limitation_count=%s",
            request_id,
            route_entities.status,
            len(route_entities.entities),
            graph_result.status if graph_result else "skipped",
            provider_statuses["detection"],
            provider_statuses["asset_profile"],
            provider_statuses["knowledge"],
            len(provenance),
            len(limitations),
        )
        conversation_snapshot = (
            self.memory_store.prepare_for_model(session, self.settings, routing_state, request_id=request_id)
            if self.settings.chat_store_history
            else None
        )
        history = conversation_snapshot.messages if conversation_snapshot else []
        history_tokens_before_budget = sum(
            approx_tokens(item.get("content", "")) for item in history
        )
        history_message_count_before_budget = len(history)
        history_budget_tokens = history_tokens_before_budget
        exhaustive_graph_request = bool(
            route.use_graph
            and (
                route.scope == "full_neighbors"
                or route.exhaustive_connections_requested
            )
        )
        if exhaustive_graph_request:
            input_capacity = max(
                0,
                self.settings.llm_context_window_tokens
                - self.settings.llm_reserved_output_tokens
                - self.settings.llm_context_safety_margin_tokens,
            )
            fixed_tokens = approx_tokens(self.system_prompt) + approx_tokens(user_text)
            compact_product_reserve = 384 * (
                len(asset_profile_results) + len(detection_results)
            )
            desired_dynamic_reserve = min(
                max(0, input_capacity - fixed_tokens),
                max(0, self.settings.graph_max_context_tokens)
                + compact_product_reserve
                + 1024,
            )
            history_budget_tokens = max(
                0,
                input_capacity - fixed_tokens - desired_dynamic_reserve,
            )
            history = self.memory_store.fit_messages_to_budget(
                history,
                history_budget_tokens,
                prefer_current_evidence=True,
            )
            logger.info(
                "event=conversation_history_route_budgeted request_id=%s requested_scope=%s history_budget_tokens=%s history_tokens_before=%s history_tokens_after=%s messages_before=%s messages_after=%s assistant_messages_dropped=%s",
                request_id,
                route.scope,
                history_budget_tokens,
                history_tokens_before_budget,
                sum(approx_tokens(item.get("content", "")) for item in history),
                history_message_count_before_budget,
                len(history),
                max(
                    0,
                    sum(1 for item in (conversation_snapshot.messages if conversation_snapshot else []) if item.get("role") == "assistant")
                    - sum(1 for item in history if item.get("role") == "assistant"),
                ),
            )
        base_input_tokens = approx_tokens(self.system_prompt) + approx_tokens(user_text) + sum(
            approx_tokens(item.get("content", "")) for item in history
        )
        dynamic_context = self.context_composer.compose(
            context_package,
            request_id=request_id,
            base_input_tokens=base_input_tokens,
        )
        profile_dynamic_context = self.context_composer.last_parts.get("asset_profile", "")
        graph_dynamic_context = self.context_composer.last_parts.get("graph", "")
        detection_dynamic_context = self.context_composer.last_parts.get("detection", "")
        fusion_dynamic_context = self.context_composer.last_parts.get("fusion", "")
        knowledge_dynamic_context = self.context_composer.last_parts.get("knowledge", "")
        self._update_product_inclusion_trace(trace, "ASSET PROFILE", "asset_profile", asset_profile_results, profile_dynamic_context)
        self._update_product_inclusion_trace(trace, "ASSET DETECTION", "detection", detection_results, detection_dynamic_context)
        if any(
            not self.context_composer.last_inclusion.get(f"detection:{item.ip}", (False, None))[0]
            for item in detection_results
            if item.status in {"available", "not_found"} and item.raw_payload_present
        ):
            response_warnings.append("detection_context_too_large")
        if any(
            not self.context_composer.last_inclusion.get(f"asset_profile:{item.ip}", (False, None))[0]
            for item in asset_profile_results
            if item.status in {"available", "not_found"} and item.raw_payload_present
        ):
            response_warnings.append("asset_profile_context_too_large")
        if route.use_knowledge and not self.context_composer.last_inclusion.get("knowledge", (False, None))[0]:
            response_warnings.append("knowledge_context_not_included")
        trace.put(
            "GRAPH RETRIEVAL",
            inbound_context_included=graph_context.get("inbound_context_included", "") if isinstance(graph_context, dict) else "",
            outbound_context_included=graph_context.get("outbound_context_included", "") if isinstance(graph_context, dict) else "",
            bidirectional_context_included=graph_context.get("bidirectional_context_included", "") if isinstance(graph_context, dict) else "",
            context_node_count=graph_context.get("context_node_count", "") if isinstance(graph_context, dict) else "",
            context_edge_count=graph_context.get("context_edge_count", "") if isinstance(graph_context, dict) else "",
            context_truncated=graph_context.get("context_truncated", False) if isinstance(graph_context, dict) else False,
            context_truncation_reason=graph_context.get("context_truncation_reason", "") if isinstance(graph_context, dict) else "",
            serialized_context_complete_for_retrieved_subset=graph_context.get("serialized_context_complete_for_retrieved_subset", False) if isinstance(graph_context, dict) else False,
            serialized_context_truncated=graph_context.get("serialized_context_truncated", False) if isinstance(graph_context, dict) else False,
            serialized_context_truncation_reason=graph_context.get("serialized_context_truncation_reason", "") if isinstance(graph_context, dict) else "",
            requested_scope_complete=graph_context.get("requested_scope_complete", False) if isinstance(graph_context, dict) else False,
            complete_for_user_request=graph_context.get("complete_for_user_request", False) if isinstance(graph_context, dict) else False,
            context_chars=len(graph_dynamic_context),
            context_tokens_approx=approx_tokens(graph_dynamic_context),
        )

        trace.put(
            "MEMORY",
            conversation_raw_message_count=conversation_snapshot.raw_message_count if conversation_snapshot else 0,
            conversation_recent_message_count=conversation_snapshot.recent_message_count if conversation_snapshot else 0,
            conversation_summary_present=conversation_snapshot.summary_present if conversation_snapshot else False,
            conversation_summary_tokens_approx=conversation_snapshot.summary_tokens_approx if conversation_snapshot else 0,
            conversation_tokens_before_compaction=conversation_snapshot.tokens_before_compaction if conversation_snapshot else 0,
            conversation_tokens_after_compaction=conversation_snapshot.tokens_after_compaction if conversation_snapshot else 0,
            conversation_summary_updated=conversation_snapshot.summary_updated if conversation_snapshot else False,
            conversation_summary_error=conversation_snapshot.summary_error if conversation_snapshot else "",
            route_aware_history_budget_applied=exhaustive_graph_request,
            history_budget_tokens=history_budget_tokens,
            history_message_count_before_budget=history_message_count_before_budget,
            history_message_count_after_budget=len(history),
            history_tokens_before_budget=history_tokens_before_budget,
            history_tokens_after_budget=sum(approx_tokens(item.get("content", "")) for item in history),
        )

        messages = [
            {"role": "system", "content": self.system_prompt},
        ]
        if dynamic_context:
            messages.append({"role": "system", "content": dynamic_context})
        messages.extend([*history, {"role": "user", "content": user_text}])

        chat_deployment = self.settings.deployment_for_purpose("chat")
        chat_request = chat_deployment.request_config("chat")
        system_chars = len(self.system_prompt)
        dynamic_context_chars = len(dynamic_context)
        conversation_chars = sum(len(item.get("content", "")) for item in history) + len(user_text)
        total_chars = sum(len(item.get("content", "")) for item in messages)
        logger.info(
            "event=model_input_prepared request_id=%s deployment=%s provider=%s model=%s message_count=%s roles=%s system_chars=%s system_approx_tokens=%s asset_profile_dynamic_approx_tokens=%s detection_dynamic_approx_tokens=%s graph_dynamic_approx_tokens=%s knowledge_dynamic_approx_tokens=%s fusion_dynamic_approx_tokens=%s dynamic_context_chars=%s dynamic_context_approx_tokens=%s conversation_chars=%s conversation_approx_tokens=%s total_input_chars=%s total_input_approx_tokens=%s",
            request_id,
            chat_deployment.name,
            chat_deployment.provider_type,
            chat_deployment.model,
            len(messages),
            ",".join(item["role"] for item in messages),
            system_chars,
            approx_tokens(self.system_prompt),
            approx_tokens(profile_dynamic_context),
            approx_tokens(detection_dynamic_context),
            approx_tokens(graph_dynamic_context),
            approx_tokens(knowledge_dynamic_context),
            approx_tokens(fusion_dynamic_context),
            dynamic_context_chars,
            approx_tokens(dynamic_context),
            conversation_chars,
            approx_tokens("x" * conversation_chars),
            total_chars,
            approx_tokens("x" * total_chars),
        )
        trace.put(
            "MODEL INPUT",
            deployment=chat_deployment.name,
            provider=chat_deployment.provider_type,
            model=chat_deployment.model,
            messages=len(messages),
            roles=", ".join(item["role"] for item in messages),
            system_tokens_approx=approx_tokens(self.system_prompt),
            asset_profile_dynamic_tokens_approx=approx_tokens(profile_dynamic_context),
            graph_dynamic_tokens_approx=approx_tokens(graph_dynamic_context),
            knowledge_dynamic_tokens_approx=approx_tokens(knowledge_dynamic_context),
            detection_dynamic_tokens_approx=approx_tokens(detection_dynamic_context),
            fusion_dynamic_tokens_approx=approx_tokens(fusion_dynamic_context),
            dynamic_tokens_approx=approx_tokens(dynamic_context),
            conversation_tokens_approx=approx_tokens("x" * conversation_chars),
            total_tokens_approx=approx_tokens("x" * total_chars),
        )
        logger.info(
            "event=conversation_request request_id=%s session_id=%s message_count=%s provider_message_count=%s user_preview=%r",
            request_id,
            session,
            len(history) + 1,
            len(messages),
            _preview(user_text),
        )
        requested_max_tokens = chat_request.max_tokens
        final_synthesis_status = "ok"
        fallback_answer_used = False
        stream_metrics: dict[str, Any] = {
            "streaming_requested": stream_sink is not None,
            "streaming_used": False,
            "first_reasoning_chunk_latency_ms": None,
            "first_answer_chunk_latency_ms": None,
            "stream_chunk_count": 0,
            "reasoning_chunk_count": 0,
            "answer_chunk_count": 0,
            "stream_completed": False,
            "stream_error_type": "",
        }
        try:
            if self.context_composer.required_context_missing:
                safe_answer = (
                    "I cannot safely analyze all requested connections because the current "
                    "graph evidence could not fit within the model context budget. No prior "
                    "assistant peer list was used as current evidence."
                )
                result = LLMProviderResult(
                    text=safe_answer,
                    provider="deterministic",
                    model="context-budget-guard",
                    deployment="deterministic",
                    finish_reason=None,
                )
                final_synthesis_status = "context_insufficient"
                fallback_answer_used = True
                response_warnings.append("graph_context_budget_insufficient")
                if stream_sink is not None:
                    stream_sink(LLMStreamEvent("answer_delta", text=safe_answer))
                logger.error(
                    "event=final_synthesis_skipped request_id=%s reason=required_graph_context_missing stale_history_used=false",
                    request_id,
                )
            elif stream_sink is not None:
                result = self._stream_final_model(
                    messages,
                    request_id=request_id,
                    max_tokens=requested_max_tokens,
                    temperature=chat_request.temperature,
                    top_p=chat_request.top_p,
                    timeout_seconds=chat_request.read_timeout_seconds,
                    sink=stream_sink,
                    metrics=stream_metrics,
                )
            else:
                result = self.llm_client.chat(
                    messages,
                    request_id=request_id,
                    max_tokens=requested_max_tokens,
                    temperature=chat_request.temperature,
                    top_p=chat_request.top_p,
                    timeout_seconds=chat_request.read_timeout_seconds,
                    purpose="chat",
                )
        except LLMError as exc:
            partial_stream = bool(exc.details.get("partial_output"))
            fallback_answer = "" if partial_stream else build_evidence_fallback_answer(
                graph_result,
                detection_results,
                asset_profile_results,
            )
            if not fallback_answer:
                trace.status = "error"
                trace.errors = 1
                trace.total_latency_ms = int((time.perf_counter() - request_started) * 1000)
                trace.put(
                    "MODEL RESPONSE",
                    requested_max_tokens=requested_max_tokens,
                    finish_reason="",
                    completion_tokens="",
                    output_tokens="",
                    answer_truncated=False,
                    final_synthesis_status="failed",
                    fallback_answer_used=False,
                )
                trace.put(
                    "RESULT",
                    status="error",
                    total_latency_ms=trace.total_latency_ms,
                    warnings=trace.warnings,
                    errors=trace.errors,
                    final_synthesis_status="failed",
                    fallback_answer_used=False,
                )
                logger.warning(
                    "event=final_synthesis_failed request_id=%s reason=%s requested_max_tokens=%s fallback_answer_used=false",
                    request_id,
                    exc.reason,
                    requested_max_tokens,
                )
                if self.settings.copilot_human_trace_enabled:
                    render_human_copilot_trace(trace)
                raise
            result = LLMProviderResult(
                text=fallback_answer,
                provider="deterministic",
                model="evidence-fallback",
                deployment="deterministic",
                finish_reason=None,
            )
            final_synthesis_status = "failed"
            fallback_answer_used = True
            response_warnings.append("final_synthesis_fallback_used")
            if stream_sink is not None:
                stream_sink(LLMStreamEvent("answer_delta", text=fallback_answer))
            logger.warning(
                "event=final_synthesis_failed request_id=%s reason=%s requested_max_tokens=%s fallback_answer_used=true",
                request_id,
                exc.reason,
                requested_max_tokens,
            )
        logger.info(
            "event=conversation_response request_id=%s session_id=%s provider=%s model=%s assistant_chars=%s assistant_approx_tokens=%s assistant_preview=%r",
            request_id,
            session,
            result.provider,
            result.model,
            len(result.text),
            approx_tokens(result.text),
            _preview(result.text),
        )
        usage = result.usage or {}
        completion_tokens = usage.get("completion_tokens", "")
        output_tokens = usage.get("output_tokens", "")
        answer_truncated = _answer_truncated(result.finish_reason, completion_tokens, requested_max_tokens)
        if answer_truncated:
            response_warnings.append("final_answer_truncated")
        response_warnings = list(dict.fromkeys(response_warnings))
        trace.warnings = len(response_warnings)
        logger.info(
            "event=final_synthesis_result request_id=%s requested_max_tokens=%s finish_reason=%s completion_tokens=%s output_tokens=%s answer_truncated=%s final_synthesis_status=%s fallback_answer_used=%s",
            request_id,
            requested_max_tokens,
            result.finish_reason or "",
            completion_tokens,
            output_tokens,
            str(answer_truncated).lower(),
            final_synthesis_status,
            str(fallback_answer_used).lower(),
        )
        logger.info(
            "event=main_model_stream_metrics request_id=%s streaming_requested=%s streaming_used=%s first_reasoning_chunk_latency_ms=%s first_answer_chunk_latency_ms=%s stream_chunk_count=%s reasoning_chunk_count=%s answer_chunk_count=%s stream_completed=%s stream_error_type=%s",
            request_id,
            str(stream_metrics["streaming_requested"]).lower(),
            str(stream_metrics["streaming_used"]).lower(),
            stream_metrics["first_reasoning_chunk_latency_ms"]
            if stream_metrics["first_reasoning_chunk_latency_ms"] is not None
            else "",
            stream_metrics["first_answer_chunk_latency_ms"]
            if stream_metrics["first_answer_chunk_latency_ms"] is not None
            else "",
            stream_metrics["stream_chunk_count"],
            stream_metrics["reasoning_chunk_count"],
            stream_metrics["answer_chunk_count"],
            str(stream_metrics["stream_completed"]).lower(),
            stream_metrics["stream_error_type"],
        )
        trace.put(
            "MODEL RESPONSE",
            deployment=result.deployment,
            provider=result.provider,
            model=result.model,
            provider_status=result.status_code or "",
            provider_latency_ms=result.latency_ms,
            prompt_tokens=usage.get("prompt_tokens", ""),
            requested_max_tokens=requested_max_tokens,
            finish_reason=result.finish_reason or "",
            completion_tokens=completion_tokens,
            output_tokens=output_tokens,
            total_tokens=usage.get("total_tokens", ""),
            answer_truncated=answer_truncated,
            final_synthesis_status=final_synthesis_status,
            fallback_answer_used=fallback_answer_used,
            answer_chars=len(result.text),
            answer_tokens_approx=approx_tokens(result.text),
            **stream_metrics,
        )

        if self.settings.chat_store_history:
            self.memory_store.append(session, "user", user_text)
            self.memory_store.append(session, "assistant", result.text)

        updated_active_ip = routing_state.active_ip
        updated_active_entities = routing_state.active_entities
        last_resolved_entities = routing_state.last_resolved_entities
        update_reason = "none"
        entity_state_update_intents = {
            "asset_investigation",
            "graph_neighbors",
            "graph_relationships",
            "graph_path",
            "graph_followup",
        }
        can_update_entity_state = (
            route_entities.status == "resolved"
            and bool(route_entities.entities)
            and route.intent in entity_state_update_intents
            and not route_entities.reference_suppressed
        )
        if can_update_entity_state:
            resolved_values = tuple(entity.value for entity in route_entities.entities)
            last_resolved_entities = resolved_values
            if len(resolved_values) == 1 and route_entities.primary_entity:
                updated_active_ip = resolved_values[0]
                updated_active_entities = ()
                if route_entities.primary_entity.source == "message":
                    update_reason = "explicit_message_entity"
                elif route_entities.primary_entity.source == "ui":
                    update_reason = "ui_selected_reference"
                elif route_entities.primary_entity.source == "conversation":
                    update_reason = "conversation_reference"
            elif len(resolved_values) == 2:
                updated_active_ip = None
                updated_active_entities = resolved_values
                update_reason = "entity_pair_resolved"

        graph_execution_succeeded = bool(graph_result and graph_result.status in {"available", "not_found"})
        detection_execution_succeeded = bool(detection_results) and all(item.status in {"available", "not_found"} for item in detection_results)
        asset_profile_execution_succeeded = bool(asset_profile_results) and all(item.status in {"available", "not_found"} for item in asset_profile_results)
        graph_available = bool(graph_result and graph_result.status == "available")
        detection_available = any(item.status == "available" for item in detection_results)
        asset_profile_available = any(item.status == "available" for item in asset_profile_results)
        knowledge_available = bool(knowledge_result and knowledge_result.status in {"ok", "partial"})
        evidence_execution_succeeded = graph_execution_succeeded or detection_execution_succeeded or asset_profile_execution_succeeded
        used_providers = tuple(
            provider
            for provider, succeeded in (
                ("graph", graph_execution_succeeded and route.use_graph),
                ("detection", detection_execution_succeeded and route.use_detection),
                ("asset_profile", asset_profile_execution_succeeded and route.use_asset_profile),
            )
            if succeeded
        )
        updated_last_provider = (
            "combined"
            if len(used_providers) > 1
            else used_providers[0]
            if used_providers
            else routing_state.last_provider
        )
        updated_last_providers = used_providers or routing_state.last_providers
        updated_previous_intent = route.intent if evidence_execution_succeeded else routing_state.previous_intent
        updated_previous_scope = route.scope if evidence_execution_succeeded else routing_state.previous_scope
        updated_previous_direction = route.direction if evidence_execution_succeeded else routing_state.previous_direction
        updated_previous_depth = route.depth if evidence_execution_succeeded else routing_state.previous_depth
        updated_previous_requires_detection = (
            route.use_detection if evidence_execution_succeeded else routing_state.previous_requires_detection
        )
        updated_previous_requires_asset_profile = (
            route.use_asset_profile if evidence_execution_succeeded else routing_state.previous_requires_asset_profile
        )
        new_routing_state = SessionRoutingState(
            active_ip=updated_active_ip,
            active_entities=updated_active_entities,
            last_resolved_entities=last_resolved_entities,
            previous_entity_count=len(route_entities.entities) if can_update_entity_state else routing_state.previous_entity_count,
            previous_entity_mode=route_entities.entity_mode if can_update_entity_state else routing_state.previous_entity_mode,
            last_provider=updated_last_provider,
            last_providers=updated_last_providers,
            previous_intent=updated_previous_intent,
            previous_scope=updated_previous_scope,
            previous_direction=updated_previous_direction,
            previous_depth=updated_previous_depth,
            previous_requires_detection=updated_previous_requires_detection,
            previous_requires_asset_profile=updated_previous_requires_asset_profile,
        )
        self.routing_state_store.set(session, new_routing_state)
        if new_routing_state != routing_state:
            logger.info(
                "event=session_routing_state_updated request_id=%s session_id=%s previous_active_ip=%s active_ip=%s previous_active_entities=%s active_entities=%s previous_last_provider=%s last_provider=%s last_providers=%s previous_intent=%s previous_scope=%s previous_direction=%s previous_depth=%s update_reason=%s",
                request_id,
                session,
                routing_state.active_ip or "",
                new_routing_state.active_ip or "",
                ",".join(routing_state.active_entities),
                ",".join(new_routing_state.active_entities),
                routing_state.last_provider or "",
                new_routing_state.last_provider or "",
                ",".join(new_routing_state.last_providers),
                new_routing_state.previous_intent or "",
                new_routing_state.previous_scope or "",
                new_routing_state.previous_direction or "",
                new_routing_state.previous_depth if new_routing_state.previous_depth is not None else "",
                update_reason,
            )
        else:
            logger.info(
                "event=session_routing_state_unchanged request_id=%s session_id=%s active_ip=%s last_provider=%s",
                request_id,
                session,
                new_routing_state.active_ip or "",
                new_routing_state.last_provider or "",
            )

        trace.put(
            "STATE UPDATE",
            active_ip_after=new_routing_state.active_ip or "",
            active_entities_after=", ".join(new_routing_state.active_entities),
            active_entity_count_after=len(new_routing_state.active_entities),
            last_provider_after=new_routing_state.last_provider or "",
            last_providers_after=", ".join(new_routing_state.last_providers),
            previous_intent_after=new_routing_state.previous_intent or "",
            previous_scope_after=new_routing_state.previous_scope or "",
            previous_direction_after=new_routing_state.previous_direction or "",
            previous_depth_after=new_routing_state.previous_depth if new_routing_state.previous_depth is not None else "",
            previous_requires_detection_after=new_routing_state.previous_requires_detection,
            previous_requires_asset_profile_after=new_routing_state.previous_requires_asset_profile,
            update_reason=update_reason,
        )
        if self.settings.chat_store_history:
            try:
                post_summary_updated = self.memory_store.compact_if_needed(
                    session,
                    self.settings,
                    new_routing_state,
                    route=route,
                    graph_context=graph_context if isinstance(graph_context, dict) else {},
                    request_id=request_id,
                )
                trace.put("MEMORY", conversation_summary_updated_after_response=post_summary_updated)
            except Exception as exc:
                logger.warning(
                    "event=conversation_summary_failed request_id=%s session_id=%s error_type=%s",
                    request_id,
                    session,
                    type(exc).__name__,
                )
                trace.put("MEMORY", conversation_summary_error=type(exc).__name__)
        trace.status = "ok"
        trace.total_latency_ms = int((time.perf_counter() - request_started) * 1000)
        trace.put(
            "RESULT",
            status="ok",
            total_latency_ms=trace.total_latency_ms,
            warnings=trace.warnings,
            errors=trace.errors,
            providers_requested=", ".join(
                provider
                for provider, enabled in [("graph", route.use_graph), ("detection", route.use_detection), ("asset_profile", route.use_asset_profile), ("knowledge", route.use_knowledge)]
                if enabled
            )
            or "none",
            providers_available=", ".join(
                provider
                for provider, available in [
                    ("graph", graph_available),
                    ("detection", detection_available),
                    ("asset_profile", asset_profile_available),
                    ("knowledge", knowledge_available),
                ]
                if available
            )
            or "none",
            providers_not_found=", ".join(
                provider
                for provider, not_found in [
                    ("graph", bool(route.use_graph and graph_result and graph_result.status == "not_found")),
                    ("detection", bool(route.use_detection and any(item.status == "not_found" for item in detection_results))),
                    ("asset_profile", bool(route.use_asset_profile and any(item.status == "not_found" for item in asset_profile_results))),
                    ("knowledge", bool(route.use_knowledge and knowledge_result and knowledge_result.status == "empty")),
                ]
                if not_found
            )
            or "none",
            providers_unavailable=", ".join(
                provider
                for provider, unavailable in [
                    ("graph", bool(route.use_graph and graph_result and graph_result.status == "unavailable")),
                    ("detection", bool(route.use_detection and any(item.status == "unavailable" for item in detection_results))),
                    ("asset_profile", bool(route.use_asset_profile and any(item.status == "unavailable" for item in asset_profile_results))),
                    ("knowledge", bool(route.use_knowledge and knowledge_result and knowledge_result.status in {"not_configured", "unavailable", "invalid"})),
                ]
                if unavailable
            )
            or "none",
            final_synthesis_status=final_synthesis_status,
            fallback_answer_used=fallback_answer_used,
        )
        if self.settings.copilot_human_trace_enabled:
            render_human_copilot_trace(trace)

        return {
            "session_id": session,
            "answer": result.text,
            "provider": result.provider,
            "model": result.model,
            "_warnings": response_warnings,
        }
