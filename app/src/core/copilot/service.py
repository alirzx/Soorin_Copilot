"""Bounded evidence-driven Copilot chat service."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from queue import SimpleQueue
from threading import RLock, Thread
from typing import Any
from uuid import uuid4

from src.config.settings import Settings
from src.core.agent.executor import CapabilityExecutor
from src.core.agent.plan_validator import PlanValidator
from src.core.agent.planner import BoundedPlanner
from src.core.agent.registry import build_capability_registry
from src.core.agent.reviewer import EvidenceReviewer
from src.core.agent.workflow import BoundedCopilotWorkflow
from src.core.agent.nodes import CopilotWorkflowNodes
from src.core.context import ContextComposer, DeterministicFallbackRouter, EntityResolver, SemanticIntentRouter
from src.core.context.providers import AssetProfileContextProvider, DetectionContextProvider, GraphContextProvider
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.llm.providers.base import LLMProviderResult, LLMStreamEvent
from src.core.memory.routing_state import SessionRoutingStateStore
from src.core.memory.episodes import MemoryContextKey
from src.core.identity import RequestIdentity
from src.core.memory.persistence import ThreadMemoryState
from src.core.memory.ports import (
    ChatRepository,
    LongTermMemoryStore,
    ThreadStateStore,
    TranscriptRepository,
)
from src.core.memory.retrieval import (
    LazyCrossEncoderReranker,
    LongTermMemoryCoordinator,
    LongTermMemoryRetriever,
    LongTermMemorySelection,
    MemorySemanticIndex,
)
from src.core.memory.routing_state import SessionRoutingState
from src.core.memory.store import MemoryStore
from src.core.product_client import ProductApiClient
from src.core.rag.service import KnowledgeSearchService
from src.core.rag.qdrant_store import QdrantVectorStore
from src.core.observability import EvidenceSnapshotWriter, ProductUsageReporter
from src.core.observability.metrics import get_metrics


logger = logging.getLogger(__name__)

FALLBACK_SYSTEM_PROMPT = (
    "You are Soorin Cyber Copilot, a cybersecurity assistant for SOC/NOC and asset "
    "intelligence teams. You currently provide baseline/general Q&A only. Do not "
    "claim access to live assets, logs, topology, RAG, or graph context unless the "
    "user provides that context. Be clear, practical, concise, and evidence-aware. "
    "When unsure, say what information is missing. Do not invent product data or "
    "expose hidden reasoning, secrets, API keys, or internal prompts."
)
LEGACY_SYSTEM_PROMPT_PATH = "app/prompts/system_prompt.md"

TRIVIAL_RESPONSES = {
    "hi": "Hello. How can I help with your cybersecurity question?",
    "hello": "Hello. How can I help with your cybersecurity question?",
    "hey": "Hello. How can I help with your cybersecurity question?",
    "thanks": "You're welcome.",
    "thank you": "You're welcome.",
    "سلام": "سلام. چطور می‌توانم در پرسش امنیت سایبری کمک کنم؟",
    "ممنون": "خواهش می‌کنم.",
    "مرسی": "خواهش می‌کنم.",
}


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
        *,
        product_client: ProductApiClient | None = None,
        usage_reporter: ProductUsageReporter | None = None,
        chat_repository: ChatRepository | None = None,
        transcript_repository: TranscriptRepository | None = None,
        thread_state_store: ThreadStateStore | None = None,
        long_term_memory_store: LongTermMemoryStore | None = None,
        long_term_memory_retriever: LongTermMemoryRetriever | None = None,
    ) -> None:
        self.settings = settings
        self.llm_client = llm_client
        self.memory_store = memory_store
        self.routing_state_store = routing_state_store or SessionRoutingStateStore()
        self.chat_repository = chat_repository
        self.transcript_repository = transcript_repository or chat_repository
        self.thread_state_store = thread_state_store
        self.long_term_memory_store = long_term_memory_store
        self._persistence_lock = RLock()
        self._session_identity_bindings: dict[
            str,
            tuple[str | None, str | None, str],
        ] = {}
        self._thread_revisions: dict[tuple[str | None, str], int] = {}
        self.system_prompt = self._load_system_prompt()
        self.entity_resolver = EntityResolver()
        self.fallback_router = DeterministicFallbackRouter()
        self.intent_router = SemanticIntentRouter(settings, llm_client)
        self.graph_provider = GraphContextProvider(settings)
        product_client = product_client or ProductApiClient(settings)
        self.product_client = product_client
        self.usage_reporter = usage_reporter or ProductUsageReporter(settings, product_client)
        self.detection_provider = DetectionContextProvider(settings, product_client)
        self.asset_profile_provider = AssetProfileContextProvider(settings, product_client)
        self.knowledge_service = KnowledgeSearchService(settings)
        self.long_term_memory_retriever = long_term_memory_retriever
        self.long_term_memory_coordinator: LongTermMemoryCoordinator | None = None
        if (
            self.long_term_memory_retriever is None
            and bool(getattr(settings, "long_term_memory_enabled", False))
            and self.long_term_memory_store is not None
        ):
            semantic_index = None
            if bool(getattr(settings, "memory_vector_index_enabled", False)):
                shared_client_factory = None
                knowledge_vector_store = self.knowledge_service.vector_store
                if isinstance(knowledge_vector_store, QdrantVectorStore):
                    shared_client_factory = knowledge_vector_store._get_client
                memory_vector_store = QdrantVectorStore(
                    mode=settings.rag_qdrant_mode,
                    path=settings.rag_qdrant_path,
                    url=settings.rag_qdrant_url,
                    collection=getattr(settings, "memory_qdrant_collection", "soorin_copilot_memory_v1"),
                    dimension=settings.rag_embedding_dimension,
                    distance=settings.rag_distance,
                    api_key=settings.rag_qdrant_api_key,
                    timeout_seconds=settings.rag_qdrant_timeout_seconds,
                    batch_size=settings.rag_upsert_batch_size,
                    embedding_model=settings.rag_embedding_model,
                    client_factory=shared_client_factory,
                )
                semantic_index = MemorySemanticIndex(
                    self.knowledge_service.embedder,
                    memory_vector_store,
                )
            reranker = (
                LazyCrossEncoderReranker(
                    getattr(settings, "memory_rerank_model", ""),
                    timeout_seconds=getattr(settings, "memory_rerank_timeout_seconds", 10.0),
                )
                if bool(getattr(settings, "memory_rerank_enabled", False))
                else None
            )
            self.long_term_memory_retriever = LongTermMemoryRetriever(
                self.long_term_memory_store,
                semantic_index,
                candidate_k=getattr(settings, "memory_retrieval_candidate_k", 20),
                top_k=getattr(settings, "memory_retrieval_top_k", 5),
                min_score=getattr(settings, "memory_min_score", 0.35),
                context_token_budget=getattr(settings, "memory_context_long_term_token_budget", 500),
                reranker=reranker,
            )
            self.long_term_memory_coordinator = LongTermMemoryCoordinator(
                self.long_term_memory_store,
                semantic_index,
                auto_promotion_enabled=getattr(
                    settings, "memory_auto_promotion_enabled", True
                ),
                policy_version=getattr(
                    settings, "memory_promotion_policy_version", "ltm-promotion-v1"
                ),
                active_validity_seconds=getattr(
                    settings, "memory_active_validity_seconds", 86_400
                ),
            )
        self._capability_runtime_lock = RLock()
        self.capability_registry = build_capability_registry(
            asset_profile_provider=self.asset_profile_provider,
            detection_provider=self.detection_provider,
            graph_provider=self.graph_provider,
            knowledge_service=self.knowledge_service,
        )
        self.plan_validator = PlanValidator(
            self.capability_registry,
            max_calls=settings.agent_max_capability_calls,
            max_entities=settings.agent_max_entities,
            max_graph_depth=settings.agent_max_graph_depth,
        )
        self.capability_executor = CapabilityExecutor(
            self.capability_registry,
            max_concurrency=settings.agent_executor_max_concurrency,
            max_calls=settings.agent_max_capability_calls,
            total_timeout_seconds=settings.agent_request_timeout_seconds,
        )
        self.planner = BoundedPlanner(
            llm_client,
            repair_enabled=settings.planner_repair_enabled,
            system_prompt_path=settings.planner_system_prompt_path,
        )
        self.evidence_reviewer = EvidenceReviewer()
        self._capability_provider_ids = self._current_capability_provider_ids()
        self.context_composer = ContextComposer(settings)
        self.snapshot_writer = EvidenceSnapshotWriter(settings)
        self.workflow = BoundedCopilotWorkflow(settings)

    def retrieve_long_term_memory(
        self,
        *,
        identity: RequestIdentity,
        message: str,
        entity_ids: tuple[str, ...],
        required_evidence_classes: tuple[str, ...] = (),
    ) -> LongTermMemorySelection:
        """Retrieve owner-scoped context without influencing route or tool selection."""
        if self.long_term_memory_retriever is None or not identity.user_id:
            return LongTermMemorySelection(status="disabled")
        query = message
        if entity_ids:
            query = f"{message}\nResolved entities: {', '.join(entity_ids)}"
        try:
            selection = self.long_term_memory_retriever.retrieve(
                query=query,
                user_id=identity.user_id,
                entity_ids=entity_ids,
                required_evidence_classes=required_evidence_classes,
                request_id=identity.request_id,
            )
            if self.long_term_memory_store is None:
                return selection
            try:
                candidates = self.long_term_memory_store.list(
                    user_id=identity.user_id,
                    entity_ids=entity_ids,
                    statuses=("candidate",),
                    limit=100,
                    request_id=identity.request_id,
                    purpose="candidate_inventory",
                )
                active = self.long_term_memory_store.list(
                    user_id=identity.user_id,
                    entity_ids=entity_ids,
                    statuses=("active",),
                    limit=100,
                    request_id=identity.request_id,
                    purpose="active_inventory",
                )
            except Exception as exc:
                logger.warning(
                    "event=memory_inventory_unavailable request_id=%s error_type=%s",
                    identity.request_id,
                    type(exc).__name__,
                )
                return replace(
                    selection,
                    selected_count=len(selection.memories),
                    limitations=tuple(dict.fromkeys((*selection.limitations, "long_term_memory_inventory_unavailable"))),
                )
            return replace(
                selection,
                candidate_record_count=len(candidates),
                active_record_count=len(active),
                selected_count=len(selection.memories),
            )
        except Exception as exc:
            logger.warning(
                "event=memory_retrieval_completed request_id=%s status=unavailable error_type=%s",
                identity.request_id,
                type(exc).__name__,
            )
            return LongTermMemorySelection(
                status="unavailable",
                limitations=("long_term_memory_unavailable",),
            )

    @staticmethod
    def _local_chat_identity(
        identity: RequestIdentity,
    ) -> tuple[str, str] | None:
        if not identity.user_id or not identity.conversation_id:
            return None
        return identity.user_id, identity.conversation_id

    def restore_thread_continuity(self, identity: RequestIdentity) -> None:
        """Load approved routing and bounded conversation continuity."""
        if self.thread_state_store is None:
            return
        binding = (identity.user_id, identity.conversation_id, identity.thread_key)
        revision_key = (identity.user_id, identity.thread_key)
        with self._persistence_lock:
            previous_binding = self._session_identity_bindings.get(identity.session_id)
            if previous_binding is not None and previous_binding != binding:
                self.memory_store.clear_session(identity.session_id)
                self.routing_state_store.clear(identity.session_id)
            self._session_identity_bindings[identity.session_id] = binding
            try:
                persisted = self.thread_state_store.load(identity=identity)
            except Exception as exc:
                self.memory_store.clear_session(identity.session_id)
                self.routing_state_store.clear(identity.session_id)
                self._thread_revisions.pop(revision_key, None)
                logger.warning(
                    "event=thread_state_load_failed request_id=%s error_type=%s",
                    identity.request_id,
                    type(exc).__name__,
                )
                return
            if persisted is None:
                self._thread_revisions[revision_key] = 0
                return
            self.routing_state_store.set(
                identity.session_id,
                persisted.to_routing_state(),
            )
            transcript: tuple[Any, ...] = ()
            local_identity = self._local_chat_identity(identity)
            transcript_repository = getattr(
                self, "transcript_repository", self.chat_repository
            )
            if transcript_repository is not None and local_identity is not None:
                try:
                    transcript = transcript_repository.recent(
                        user_id=local_identity[0],
                        conversation_id=local_identity[1],
                        limit=max(3, self.settings.memory_relevant_turn_limit * 2 + 1),
                        request_id=identity.request_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "event=memory_persistence_failed request_id=%s operation=transcript_restore error_type=%s",
                        identity.request_id,
                        type(exc).__name__,
                    )
            settings = getattr(self, "settings", None)
            if settings is not None and settings.durable_working_memory_enabled:
                self.memory_store.restore_durable_state(persisted, transcript)
            self._thread_revisions[revision_key] = persisted.revision
            logger.info(
                "event=thread_memory_loaded request_id=%s revision=%s active_entity_count=%s episode_count=%s",
                identity.request_id,
                persisted.revision,
                len(persisted.active_entities),
                len(persisted.recent_episodes),
            )
            if persisted.working_memory and persisted.working_memory.compact_summary:
                logger.info(
                    "event=working_summary_restored request_id=%s summary_tokens=%s",
                    identity.request_id,
                    persisted.summary_size_tokens,
                )

    def persist_thread_continuity(
        self,
        identity: RequestIdentity,
        routing_state: SessionRoutingState,
    ) -> None:
        """Persist compact terminal continuity without affecting the response."""
        if self.thread_state_store is None:
            return
        revision_key = (identity.user_id, identity.thread_key)
        with self._persistence_lock:
            expected_revision = self._thread_revisions.get(revision_key, 0)
            settings = getattr(self, "settings", None)
            components = (
                self.memory_store.durable_components(
                    identity.session_id,
                    turn_limit=settings.memory_relevant_turn_limit,
                    episode_limit=settings.memory_episode_retention_limit,
                )
                if settings is not None and settings.durable_working_memory_enabled
                else {}
            )
            state = ThreadMemoryState.from_routing_state(
                identity,
                routing_state,
                revision=expected_revision,
                **components,
            )
            try:
                saved = self.thread_state_store.save(
                    identity=identity,
                    state=state,
                    expected_revision=expected_revision,
                )
            except Exception as exc:
                logger.warning(
                    "event=thread_state_save_failed request_id=%s error_type=%s",
                    identity.request_id,
                    type(exc).__name__,
                )
                return
            self._thread_revisions[revision_key] = saved.revision
            logger.info(
                "event=thread_memory_saved request_id=%s revision=%s active_entity_count=%s episode_count=%s",
                identity.request_id,
                saved.revision,
                len(saved.active_entities),
                len(saved.recent_episodes),
            )

    def begin_local_request(self, identity: RequestIdentity) -> None:
        local_identity = self._local_chat_identity(identity)
        if self.chat_repository is None or local_identity is None:
            return
        user_id, conversation_id = local_identity
        try:
            self.chat_repository.begin_request(
                user_id=user_id,
                conversation_id=conversation_id,
                request_id=identity.request_id,
            )
        except Exception as exc:
            logger.warning(
                "event=local_chat_request_begin_failed request_id=%s error_type=%s",
                identity.request_id,
                type(exc).__name__,
            )

    def persist_completed_local_turn(
        self,
        identity: RequestIdentity,
        *,
        user_content: str,
        assistant_content: str,
    ) -> None:
        local_identity = self._local_chat_identity(identity)
        if self.chat_repository is None or local_identity is None:
            return
        user_id, conversation_id = local_identity
        try:
            self.chat_repository.commit_turn(
                user_id=user_id,
                conversation_id=conversation_id,
                request_id=identity.request_id,
                user_content=user_content,
                assistant_content=assistant_content,
            )
        except Exception as exc:
            logger.warning(
                "event=local_chat_turn_commit_failed request_id=%s error_type=%s",
                identity.request_id,
                type(exc).__name__,
            )

    def finalize_uncommitted_local_request(
        self,
        identity: RequestIdentity,
        *,
        request_success: bool,
    ) -> None:
        local_identity = self._local_chat_identity(identity)
        if self.chat_repository is None or local_identity is None:
            return
        user_id, conversation_id = local_identity
        try:
            self.chat_repository.mark_request_status(
                user_id=user_id,
                conversation_id=conversation_id,
                request_id=identity.request_id,
                status="interrupted" if request_success else "failed",
            )
        except Exception as exc:
            logger.warning(
                "event=local_chat_request_finalize_failed request_id=%s error_type=%s",
                identity.request_id,
                type(exc).__name__,
            )

    def _current_capability_provider_ids(self) -> tuple[int, int, int, int]:
        return (
            id(self.asset_profile_provider),
            id(self.detection_provider),
            id(self.graph_provider),
            id(self.knowledge_service),
        )

    def _capability_runtime_snapshot(self) -> tuple[Any, PlanValidator, CapabilityExecutor]:
        """Return one internally consistent runtime, rebinding injected providers atomically."""
        with self._capability_runtime_lock:
            current_ids = self._current_capability_provider_ids()
            if current_ids != self._capability_provider_ids:
                registry = build_capability_registry(
                    asset_profile_provider=self.asset_profile_provider,
                    detection_provider=self.detection_provider,
                    graph_provider=self.graph_provider,
                    knowledge_service=self.knowledge_service,
                )
                self.capability_registry = registry
                self.plan_validator = PlanValidator(
                    registry,
                    max_calls=self.settings.agent_max_capability_calls,
                    max_entities=self.settings.agent_max_entities,
                    max_graph_depth=self.settings.agent_max_graph_depth,
                )
                self.capability_executor = CapabilityExecutor(
                    registry,
                    max_concurrency=self.settings.agent_executor_max_concurrency,
                    max_calls=self.settings.agent_max_capability_calls,
                    total_timeout_seconds=self.settings.agent_request_timeout_seconds,
                )
                self._capability_provider_ids = current_ids
                logger.info("event=capability_runtime_rebound reason=provider_injection")
            return self.capability_registry, self.plan_validator, self.capability_executor

    def _load_system_prompt(self) -> str:
        configured_path = self.settings.system_prompt_path
        prompt_path = self._resolve_prompt_path(configured_path)

        try:
            prompt = prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            prompt = ""

        if not prompt:
            legacy_path = self._resolve_prompt_path(LEGACY_SYSTEM_PROMPT_PATH)
            if prompt_path != legacy_path:
                try:
                    legacy_prompt = legacy_path.read_text(encoding="utf-8").strip()
                except OSError:
                    legacy_prompt = ""
                if legacy_prompt:
                    logger.warning(
                        "event=system_prompt_legacy_fallback configured_path=%s fallback_path=%s chars=%s",
                        configured_path,
                        LEGACY_SYSTEM_PROMPT_PATH,
                        len(legacy_prompt),
                    )
                    return legacy_prompt
            logger.warning(
                "event=system_prompt_unavailable path=%s fallback=builtin chars=%s",
                configured_path,
                len(FALLBACK_SYSTEM_PROMPT),
            )
            return FALLBACK_SYSTEM_PROMPT

        logger.info(
            "event=system_prompt_loaded path=%s chars=%s",
            configured_path,
            len(prompt),
        )
        return prompt

    @staticmethod
    def _resolve_prompt_path(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else Path.cwd() / path

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
        trace_id: str = "",
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
                trace_id=trace_id,
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
        request_identity: RequestIdentity | None = None,
    ) -> dict[str, Any]:
        return self._chat(
            message,
            session_id,
            ui_context=ui_context,
            request_id=request_id,
            request_identity=request_identity,
        )

    def chat_stream(
        self,
        message: str,
        session_id: str | None = None,
        *,
        ui_context: dict[str, Any] | None = None,
        request_id: str | None = None,
        request_identity: RequestIdentity | None = None,
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
                    request_identity=request_identity,
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
        request_identity: RequestIdentity | None = None,
        stream_sink: Callable[[LLMStreamEvent], None] | None = None,
        ) -> dict[str, Any]:
        request_started = time.perf_counter()
        identity = request_identity or RequestIdentity.resolve(
            session_id=session_id,
            request_id=request_id,
        )
        resolved_request_id = identity.request_id
        workflow_trace_id = uuid4().hex[:16]
        resolved_session_id = identity.session_id
        self.restore_thread_continuity(identity)
        self.begin_local_request(identity)
        trivial = self._trivial_response(message)
        if trivial is not None:
            session = resolved_session_id
            if self.settings.chat_store_history:
                general_context = MemoryContextKey()
                self.memory_store.prepare_for_model(
                    session,
                    self.settings,
                    self.routing_state_store.get(session),
                    context_key=general_context,
                    request_id=resolved_request_id,
                )
                self.memory_store.record_turn(
                    session,
                    message.strip(),
                    trivial,
                    general_context,
                    providers=("deterministic",),
                    request_id=resolved_request_id,
                )
            self.persist_completed_local_turn(
                identity,
                user_content=message.strip(),
                assistant_content=trivial,
            )
            if stream_sink is not None:
                stream_sink(LLMStreamEvent("answer_delta", text=trivial))
            logger.info(
                "event=trivial_message_fast_path request_id=%s session_id=%s streaming=%s router_called=false planner_called=false provider_called=false routing_state_mutated=false",
                resolved_request_id,
                session,
                stream_sink is not None,
            )
            get_metrics().observe_copilot(
                "completed",
                "direct",
                time.perf_counter() - request_started,
            )
            return {
                "session_id": session,
                "answer": trivial,
                "provider": "deterministic",
                "model": "trivial-message-fast-path",
                "_warnings": [],
            }
        usage_scope = self.usage_reporter.start_request(
            resolved_request_id,
            workflow_trace_id,
            resolved_session_id,
        )
        request_success = False
        try:
            result = self.workflow.run(
                message=message,
                session_id=resolved_session_id,
                ui_context=ui_context,
                request_id=resolved_request_id,
                trace_id=workflow_trace_id,
                request_identity=identity,
                stream_sink=stream_sink,
                node_runtime=CopilotWorkflowNodes(self, stream_sink=stream_sink),
            )
            request_success = True
            return result
        finally:
            self.usage_reporter.finish_request(usage_scope, request_success=request_success)
            self.finalize_uncommitted_local_request(
                identity,
                request_success=request_success,
            )

    def close(self) -> None:
        """Release owned local resources during application shutdown."""
        self.knowledge_service.close()
        self.workflow.close()

    @staticmethod
    def _trivial_response(message: str) -> str | None:
        normalized = " ".join(message.strip().casefold().split())
        return TRIVIAL_RESPONSES.get(normalized)
