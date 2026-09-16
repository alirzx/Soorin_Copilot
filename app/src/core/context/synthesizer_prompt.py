"""Typed, deterministic prompt context for final Copilot synthesis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from src.core.agent.contracts import (
    EvidenceMode,
    RequestConstraints,
    ResponseDepth,
    ReviewDecision,
    TaskSpec,
    TemporalMode,
    ToolResult,
)


ProviderStateStatus = Literal[
    "available", "partial", "empty", "unavailable", "not_configured", "not_requested"
]


@dataclass(frozen=True)
class SynthesizerMemoryState:
    working_available: bool = False
    episodic_available: bool = False
    ltm_status: str = "not_supplied"
    ltm_candidate_count: int = 0
    ltm_active_count: int = 0
    ltm_selected_count: int = 0
    historical_only: bool = False
    thread_recall_requested: bool = False
    thread_recall_complete: bool = False
    sources_considered: tuple[str, ...] = ()
    sources_omitted: tuple[str, ...] = ()
    memory_context_truncated: bool = False
    inventory_available: bool | None = None
    compatible_previous_baseline_available: bool = False
    baseline_status: str = "absent"
    baseline_present: bool = False
    baseline_compatible: bool = False


@dataclass(frozen=True)
class SynthesizerProviderState:
    status: ProviderStateStatus = "not_requested"
    freshness: str = "not_applicable"
    completeness: str = "not_applicable"
    truncated: bool = False
    context_included: bool = False


@dataclass(frozen=True)
class SynthesizerExecutionState:
    current_retrieval_requested: bool = False
    live_retrieval_performed: bool = False
    capability_call_count: int = 0
    successful_current_evidence_count: int = 0
    current_evidence_timestamps: tuple[str, ...] = ()
    memory_write_requested: bool = False
    accepted_working_fact_count: int = 0


@dataclass(frozen=True)
class SynthesizerContinuityState:
    previous_structured_set_available: bool = False
    previous_structured_set_used: bool = False
    structured_reference_kind: str = "none"
    base_structured_set_available: bool = False
    current_result_count: int | None = None
    previous_result_count: int | None = None
    structured_results_truncated: bool = False
    active_focal_entities: tuple[str, ...] = ()
    focal_baseline_available: bool = False


@dataclass(frozen=True)
class SynthesizerTaskContext:
    task_category: str
    intent: str
    entities: tuple[str, ...]
    temporal_mode: TemporalMode
    evidence_mode: EvidenceMode
    response_depth: ResponseDepth
    execution: SynthesizerExecutionState
    continuity: SynthesizerContinuityState
    memory: SynthesizerMemoryState
    profile: SynthesizerProviderState
    detection: SynthesizerProviderState
    graph: SynthesizerProviderState
    knowledge: SynthesizerProviderState
    contradiction_count: int
    current_vs_historical_relationship: str
    deterministic_delta_available: bool
    selected_analytical_lenses: tuple[str, ...]
    limitations: tuple[str, ...]
    output_constraints: tuple[str, ...]
    structured_query_scope: dict[str, Any]


@dataclass(frozen=True)
class RenderedSynthesizerPrompt:
    messages: tuple[dict[str, str], ...]
    dynamic_prompt: str
    selected_module_names: tuple[str, ...]


class PromptModuleRegistry:
    """Cached, deterministic Markdown prompt-module loader."""

    _FILES = {
        "tasks": "tasks.md",
        "temporal": "temporal.md",
        "evidence_mode": "evidence_modes.md",
        "execution": "execution.md",
        "evidence": "evidence.md",
        "memory": "memory.md",
        "analysis": "analysis.md",
        "response": "response.md",
        "output_constraints": "output_constraints.md",
    }
    _REQUIRED = {
        "tasks": frozenset({"asset_investigation", "asset_search", "asset_aggregate", "detection_explanation", "graph_summary", "relationship", "path", "comparison", "memory_recall", "general_security", "knowledge_explanation"}),
        "temporal": frozenset({"current", "historical", "mixed", "compare_previous_current"}),
        "evidence_mode": frozenset({"normal", "memory_only", "no_live_refresh", "current_verification", "verify_if_stale"}),
        "execution": frozenset({"current_retrieval_completed", "current_retrieval_partial", "current_retrieval_not_performed", "baseline_available", "baseline_unavailable", "working_memory_write"}),
        "evidence": frozenset({"profile", "detection", "graph", "knowledge", "incomplete_evidence", "contradictory_evidence"}),
        "memory": frozenset({"working", "episodic", "ltm_available", "no_active_ltm", "historical_memory_only"}),
        "analysis": frozenset({"threat", "defender_ir", "noc_operational", "strategic", "disposition"}),
        "response": frozenset({"brief", "standard", "deep", "report"}),
        "output_constraints": frozenset({"core"}),
    }

    def __init__(self, prompt_directory: Path | None = None) -> None:
        self.prompt_directory = prompt_directory or Path(__file__).resolve().parents[3] / "prompts" / "synthesizer"
        self._modules = self._load()

    def _load(self) -> dict[tuple[str, str], str]:
        modules: dict[tuple[str, str], str] = {}
        for category, filename in self._FILES.items():
            path = self.prompt_directory / filename
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"synthesizer_prompt_module_file_missing:{path}") from exc
            sections = self._parse(path, text)
            missing = self._REQUIRED[category] - set(sections)
            if missing:
                raise ValueError(
                    f"synthesizer_prompt_module_missing:{category}:{','.join(sorted(missing))}"
                )
            modules.update({(category, name): content for name, content in sections.items()})
        return modules

    @staticmethod
    def _parse(path: Path, text: str) -> dict[str, str]:
        sections: dict[str, str] = {}
        current: str | None = None
        lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("# "):
                if current is not None:
                    content = "\n".join(lines).strip()
                    if not content:
                        raise ValueError(f"synthesizer_prompt_module_empty:{path}:{current}")
                    sections[current] = content
                current = line[2:].strip()
                if not current or current in sections:
                    raise ValueError(f"synthesizer_prompt_module_duplicate:{path}:{current or 'blank'}")
                lines = []
            elif current is not None:
                lines.append(line)
        if current is not None:
            content = "\n".join(lines).strip()
            if not content:
                raise ValueError(f"synthesizer_prompt_module_empty:{path}:{current}")
            sections[current] = content
        return sections

    def get(self, category: str, name: str) -> str:
        try:
            return self._modules[(category, name)]
        except KeyError as exc:
            raise ValueError(f"synthesizer_prompt_module_unknown:{category}:{name}") from exc


class SynthesizerPromptBuilder:
    """Select prompt modules from validated state and render provider-neutral messages."""

    version = "synth-context-v2"

    def __init__(self, registry: PromptModuleRegistry | None = None) -> None:
        self.registry = registry or PromptModuleRegistry()
        self.template = ChatPromptTemplate.from_messages(
            [
                ("system", "{static_core}\n\n{runtime_contract}"),
                MessagesPlaceholder("evidence_context"),
                MessagesPlaceholder("history"),
                ("human", "{user_message}"),
            ]
        )

    def build_context(
        self,
        task: TaskSpec,
        results: tuple[ToolResult, ...],
        *,
        snapshot: Any = None,
        long_term_selection: Any = None,
        review: ReviewDecision | None = None,
        request_constraints: RequestConstraints | None = None,
        accepted_working_fact_count: int = 0,
        delta_contexts: tuple[dict[str, Any], ...] = (),
        baseline_status: str = "absent",
        baseline_present: bool = False,
        baseline_compatible: bool = False,
        structured_context: Any = None,
        structured_lineage: tuple[Any, ...] = (),
        structured_reference_kind: str = "none",
        active_focal_entities: tuple[str, ...] = (),
    ) -> SynthesizerTaskContext:
        memory_package = getattr(snapshot, "memory_context", None)
        selected_count = max(
            int(getattr(long_term_selection, "selected_count", 0) or 0),
            len(getattr(long_term_selection, "memories", ()) or ()),
        )
        memory = SynthesizerMemoryState(
            working_available=bool(
                getattr(memory_package, "working_summary", "")
                or getattr(memory_package, "working_facts", ())
                or getattr(memory_package, "relevant_turns", ())
                or int(getattr(snapshot, "recent_message_count", 0) or 0)
            ),
            episodic_available=bool(getattr(memory_package, "episode_summaries", ()) or ()),
            ltm_status=str(getattr(long_term_selection, "status", "not_supplied") or "not_supplied"),
            ltm_candidate_count=int(getattr(long_term_selection, "candidate_record_count", 0) or 0),
            ltm_active_count=int(getattr(long_term_selection, "active_record_count", 0) or 0),
            ltm_selected_count=selected_count,
            historical_only=task.evidence_mode == "memory_only",
            thread_recall_requested=bool(getattr(memory_package, "thread_recall_requested", False)),
            thread_recall_complete=bool(getattr(memory_package, "thread_recall_complete", False)),
            sources_considered=tuple(getattr(memory_package, "sources_considered", ()) or ()),
            sources_omitted=tuple(getattr(memory_package, "omitted", ()) or ()),
            memory_context_truncated=bool(getattr(memory_package, "omitted", ()) or ()),
            inventory_available=getattr(long_term_selection, "inventory_available", None),
            compatible_previous_baseline_available=baseline_compatible,
            baseline_status=baseline_status,
            baseline_present=baseline_present,
            baseline_compatible=baseline_compatible,
        )
        provider_states = {
            "profile": self._provider_state(results, "asset.get_profile"),
            "detection": self._provider_state(results, "asset.get_detection"),
            "graph": self._provider_state(results, "graph."),
            "knowledge": self._provider_state(results, "knowledge.search"),
        }
        live_results = tuple(item for item in results if item.provider != "long_term_memory")
        successful_current = tuple(
            item
            for item in live_results
            if item.status in {"ok", "partial"} and item.freshness == "current"
        )
        constraints = request_constraints or RequestConstraints(
            require_current=task.evidence_mode == "current_verification"
        )
        execution = SynthesizerExecutionState(
            current_retrieval_requested=bool(constraints.require_current),
            live_retrieval_performed=bool(live_results),
            capability_call_count=len(live_results),
            successful_current_evidence_count=len(successful_current),
            current_evidence_timestamps=tuple(
                dict.fromkeys(
                    str(item.valid_at or item.retrieved_at)
                    for item in successful_current
                    if item.valid_at or item.retrieved_at
                )
            )[:8],
            memory_write_requested=bool(constraints.memory_write),
            accepted_working_fact_count=max(0, int(accepted_working_fact_count)),
        )
        limitations = tuple(
            dict.fromkeys(
                [
                    *(review.limitations if review else ()),
                    *(review.reasons if review else ()),
                    *(item for result in results for item in result.limitations),
                ]
            )
        )[:20]
        contradiction_count = sum(len(result.contradictions) for result in results)
        current_structured = next(
            (
                item.structured_asset_set
                for item in results
                if item.structured_asset_set is not None
            ),
            None,
        )
        current_count = None
        if current_structured is not None:
            current_count = int(
                getattr(current_structured, "matched_total", None)
                if getattr(current_structured, "mode", "") == "search"
                else getattr(current_structured, "count", 0)
            )
        previous_count = None
        if structured_context is not None:
            previous_count = (
                getattr(structured_context, "matched_total", None)
                if getattr(structured_context, "mode", "") == "search"
                else getattr(structured_context, "count", None)
            )
        continuity = SynthesizerContinuityState(
            previous_structured_set_available=structured_context is not None,
            previous_structured_set_used=structured_reference_kind == "set_query",
            structured_reference_kind=structured_reference_kind,
            base_structured_set_available=len(structured_lineage) > 1,
            current_result_count=current_count,
            previous_result_count=previous_count,
            structured_results_truncated=bool(
                getattr(current_structured, "truncated", False)
            ),
            active_focal_entities=tuple(active_focal_entities[:2]),
            focal_baseline_available=baseline_compatible,
        )
        structured_query_scope: dict[str, Any] = {}
        if task.structured_query is not None:
            query = task.structured_query
            structured_query_scope = {
                "mode": query.mode.value,
                "filters": query.filters.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                    exclude_defaults=True,
                ),
                "requested_output_fields": [
                    item.value for item in query.requested_output_fields
                ],
                "semantic_class": query.semantic_class,
                "class_mapping_mode": query.class_mapping_mode,
                "exact_zero_scope_only": True,
            }
        return SynthesizerTaskContext(
            task_category=self._task_category(task),
            intent=task.intent,
            entities=task.entities,
            temporal_mode=task.temporal_mode,
            evidence_mode=task.evidence_mode,
            response_depth=self._response_depth(task),
            execution=execution,
            continuity=continuity,
            memory=memory,
            profile=provider_states["profile"],
            detection=provider_states["detection"],
            graph=provider_states["graph"],
            knowledge=provider_states["knowledge"],
            contradiction_count=contradiction_count,
            current_vs_historical_relationship=(
                "compatible_deterministic_delta_supplied"
                if delta_contexts
                else "compatible_baseline_unavailable"
            ),
            deterministic_delta_available=bool(delta_contexts),
            selected_analytical_lenses=self._analytical_lenses(task),
            limitations=limitations,
            output_constraints=(),
            structured_query_scope=structured_query_scope,
        )

    def render_contract(self, context: SynthesizerTaskContext) -> tuple[str, tuple[str, ...]]:
        module_ids: list[tuple[str, str]] = [
            ("tasks", context.task_category),
            ("temporal", context.temporal_mode),
            ("evidence_mode", context.evidence_mode),
        ]
        if context.execution.current_retrieval_requested:
            if not context.execution.live_retrieval_performed:
                module_ids.append(("execution", "current_retrieval_not_performed"))
            elif (
                context.execution.successful_current_evidence_count == 0
                or any(state.status in {"partial", "empty", "unavailable", "not_configured"} for state in (context.profile, context.detection, context.graph, context.knowledge))
            ):
                module_ids.append(("execution", "current_retrieval_partial"))
            else:
                module_ids.append(("execution", "current_retrieval_completed"))
        module_ids.append(("execution", "baseline_available" if context.deterministic_delta_available else "baseline_unavailable"))
        if context.execution.memory_write_requested and context.execution.accepted_working_fact_count:
            module_ids.append(("execution", "working_memory_write"))
        for name, state in (
            ("profile", context.profile),
            ("detection", context.detection),
            ("graph", context.graph),
            ("knowledge", context.knowledge),
        ):
            if state.status != "not_requested":
                module_ids.append(("evidence", name))
            if state.status in {"partial", "empty", "unavailable", "not_configured"} or state.truncated:
                module_ids.append(("evidence", "incomplete_evidence"))
        if context.contradiction_count:
            module_ids.append(("evidence", "contradictory_evidence"))
        if context.memory.working_available:
            module_ids.append(("memory", "working"))
        if context.memory.episodic_available:
            module_ids.append(("memory", "episodic"))
        if context.memory.ltm_selected_count:
            module_ids.append(("memory", "ltm_available"))
        else:
            module_ids.append(("memory", "no_active_ltm"))
        if context.memory.historical_only:
            module_ids.append(("memory", "historical_memory_only"))
        module_ids.extend(("analysis", name) for name in context.selected_analytical_lenses)
        module_ids.append(("response", context.response_depth))
        module_ids.append(("output_constraints", "core"))
        deduplicated_ids = tuple(dict.fromkeys(module_ids))
        modules = tuple(
            (f"{'task' if category == 'tasks' else category}.{name}", self.registry.get(category, name))
            for category, name in deduplicated_ids
        )
        metadata = {
            "intent": context.intent,
            "task_category": context.task_category,
            "entities": list(context.entities),
            "temporal_mode": context.temporal_mode,
            "evidence_mode": context.evidence_mode,
            "response_depth": context.response_depth,
            "execution": context.execution.__dict__,
            "continuity": context.continuity.__dict__,
            "memory": context.memory.__dict__,
            "providers": {
                "profile": context.profile.__dict__,
                "detection": context.detection.__dict__,
                "graph": context.graph.__dict__,
                "knowledge": context.knowledge.__dict__,
            },
            "contradiction_count": context.contradiction_count,
            "current_vs_historical_relationship": context.current_vs_historical_relationship,
            "deterministic_delta_available": context.deterministic_delta_available,
            "limitations": list(context.limitations),
            "structured_query_scope": context.structured_query_scope,
        }
        text = (
            "[SOORIN SYNTHESIZER TASK CONTRACT]\n"
            + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n[SELECTED INSTRUCTIONS]\n"
            + "\n".join(f"- {instruction}" for name, instruction in modules if name != "output_constraints.core")
            + "\n[OUTPUT CONSTRAINTS]\n"
            + self.registry.get("output_constraints", "core")
        )
        return text, tuple(name for name, _ in modules)

    def render_messages(
        self,
        *,
        static_core: str,
        context: SynthesizerTaskContext,
        dynamic_evidence: str,
        history: list[dict[str, str]],
        user_message: str,
    ) -> RenderedSynthesizerPrompt:
        if not static_core.strip():
            raise ValueError("synthesizer_static_core_required")
        if not user_message.strip():
            raise ValueError("synthesizer_user_message_required")
        runtime_contract, module_names = self.render_contract(context)
        evidence_messages = (
            [
                HumanMessage(
                    content=(
                        "[BEGIN UNTRUSTED EVIDENCE CONTEXT]\n"
                        f"{dynamic_evidence}\n"
                        "[END UNTRUSTED EVIDENCE CONTEXT]"
                    )
                )
            ]
            if dynamic_evidence
            else []
        )
        rendered = self.template.invoke(
            {
                "static_core": static_core,
                "runtime_contract": runtime_contract,
                "evidence_context": evidence_messages,
                "history": [self._message(item) for item in history],
                "user_message": user_message.strip(),
            }
        )
        return RenderedSynthesizerPrompt(
            messages=tuple(self._provider_message(item) for item in rendered.messages),
            dynamic_prompt=runtime_contract,
            selected_module_names=module_names,
        )

    @staticmethod
    def _message(message: dict[str, str]) -> BaseMessage:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "system":
            return SystemMessage(content=content)
        if role == "assistant":
            return AIMessage(content=content)
        if role == "user":
            return HumanMessage(content=content)
        raise ValueError("unsupported_synthesizer_history_role")

    @staticmethod
    def _provider_message(message: BaseMessage) -> dict[str, str]:
        role = "system" if isinstance(message, SystemMessage) else "assistant" if isinstance(message, AIMessage) else "user"
        return {"role": role, "content": str(message.content)}

    @staticmethod
    def _provider_state(results: tuple[ToolResult, ...], prefix: str) -> SynthesizerProviderState:
        matched = tuple(
            item for item in results
            if item.source_capability == prefix or item.source_capability.startswith(prefix)
        )
        if not matched:
            return SynthesizerProviderState()
        statuses = {item.status for item in matched}
        status: ProviderStateStatus
        if statuses == {"ok"}:
            status = "available"
        elif "partial" in statuses or "ok" in statuses:
            # A successful sub-capability cannot conceal failed or incomplete peers.
            status = "partial"
        elif statuses <= {"empty", "not_found"}:
            status = "empty"
        elif statuses == {"not_configured"}:
            status = "not_configured"
        else:
            status = "unavailable"
        freshness_values = tuple(dict.fromkeys(item.freshness for item in matched))
        completeness_values = tuple(dict.fromkeys(item.completeness for item in matched))
        return SynthesizerProviderState(
            status=status,
            freshness=freshness_values[0] if len(freshness_values) == 1 else "mixed",
            completeness=completeness_values[0] if len(completeness_values) == 1 else "mixed",
            truncated=any(item.truncated or item.projection_truncated for item in matched),
            context_included=any(item.context_included for item in matched),
        )

    @staticmethod
    def _task_category(task: TaskSpec) -> str:
        if task.evidence_mode == "memory_only" or task.intent == "memory_recall":
            return "memory_recall"
        if task.intent == "asset_search":
            return "asset_search"
        if task.intent == "asset_aggregate":
            return "asset_aggregate"
        if task.scope == "path":
            return "path"
        if task.scope == "multi_entity_comparison" or task.relationship_mode == "compare":
            return "comparison"
        if task.relationship_mode == "direct":
            return "relationship"
        if task.scope in {"node_summary", "one_hop", "two_hop", "full_neighbors"} and task.required_capabilities == ("graph.get_summary",):
            return "graph_summary"
        capabilities = set((*task.required_capabilities, *task.optional_capabilities))
        if (
            "asset.get_detection" in capabilities
            and capabilities <= {"asset.get_detection", "knowledge.search"}
        ):
            return "detection_explanation"
        if task.intent in {"general_knowledge", "general_conversation", "unclear"}:
            return "knowledge_explanation" if "knowledge.search" in (*task.required_capabilities, *task.optional_capabilities) else "general_security"
        return "asset_investigation"

    @staticmethod
    def _analytical_lenses(task: TaskSpec) -> tuple[str, ...]:
        capabilities = set((*task.required_capabilities, *task.optional_capabilities))
        text = task.request.casefold()
        lenses: list[str] = []
        if task.intent == "asset_investigation" and "asset.get_detection" in capabilities:
            lenses.append("defender_ir")
        if task.scope in {"node_summary", "one_hop", "two_hop", "full_neighbors", "path", "multi_entity_comparison"}:
            lenses.append("noc_operational")
        if task.response_depth == "report":
            lenses.append("strategic")
        if (
            task.intent == "asset_investigation"
            and capabilities == {"asset.get_detection"}
        ):
            lenses.extend(("threat", "disposition"))
        # Free-text signals supplement, but never define, normalized task semantics.
        if any(word in text for word in ("threat", "attack", "suspicious", "anomaly", "risk", "malicious")):
            lenses.append("threat")
        if any(word in text for word in ("investigate", "incident", "contain", "response", "detection")):
            lenses.append("defender_ir")
        if any(word in text for word in ("impact", "priority", "strategic")):
            lenses.append("strategic")
        if any(word in text for word in ("disposition", "benign", "malicious", "classify")):
            lenses.append("disposition")
        return tuple(dict.fromkeys(lenses))

    @staticmethod
    def _response_depth(task: TaskSpec) -> ResponseDepth:
        if task.response_depth != "standard":
            return task.response_depth
        if task.detail_level in {"deep", "detailed"}:
            return "deep"
        if task.detail_level == "report":
            return "report"
        if task.detail_level == "brief":
            return "brief"
        return "standard"
