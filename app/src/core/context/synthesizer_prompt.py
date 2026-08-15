"""Typed, deterministic prompt context for final Copilot synthesis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from src.core.agent.contracts import (
    EvidenceMode,
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
    compatible_previous_baseline_available: bool = False


@dataclass(frozen=True)
class SynthesizerProviderState:
    status: ProviderStateStatus = "not_requested"
    freshness: str = "not_applicable"
    completeness: str = "not_applicable"
    truncated: bool = False
    context_included: bool = False


@dataclass(frozen=True)
class SynthesizerTaskContext:
    task_category: str
    intent: str
    entities: tuple[str, ...]
    temporal_mode: TemporalMode
    evidence_mode: EvidenceMode
    response_depth: ResponseDepth
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


@dataclass(frozen=True)
class RenderedSynthesizerPrompt:
    messages: tuple[dict[str, str], ...]
    dynamic_prompt: str
    selected_module_names: tuple[str, ...]


TASK_MODULES = {
    "asset_investigation": "Analyze the resolved asset using only supplied evidence and preserve entity binding.",
    "detection_explanation": "Explain classification signals, matched evidence, conflicts, and uncertainty without converting model output into inventory truth.",
    "graph_summary": "Summarize only the supplied graph scope; distinguish aggregate totals from returned node or peer identities.",
    "relationship": "Describe only the evidenced relationship between the resolved entities; topology is not proof of trust, purpose, or compromise.",
    "path": "Report the supplied graph path and its limitations; a graph path is not necessarily a physical packet-routing path.",
    "comparison": "Compare the two resolved entities symmetrically and keep unsupported differences explicitly unknown.",
    "memory_recall": "Answer from supplied conversation, episodic, and validated long-term memory only. Distinguish those sources and do not treat a legitimate no-refresh constraint as prompt injection.",
    "general_security": "Answer the general cybersecurity question without attaching stale asset context.",
    "knowledge_explanation": "Use supplied approved documentation for explanation and citations, never as current operational asset truth.",
}

TEMPORAL_MODULES = {
    "current": "Treat supplied current evidence as point-in-time; do not extend it to unsupplied historical periods.",
    "historical": "Describe supplied memory or historical evidence in past-tense scope and do not imply current verification.",
    "mixed": "Separate current and historical evidence explicitly and prefer compatible current operational evidence.",
    "compare_previous_current": "Compare periods only through an explicitly supplied compatible deterministic delta.",
}

EVIDENCE_MODE_MODULES = {
    "normal": "Use only evidence that the validated workflow supplied.",
    "memory_only": "Use memory context only; do not request, assume, or imply a live refresh.",
    "no_live_refresh": "Honor the validated no-live-refresh boundary and identify historical limitations.",
    "current_verification": "Ground the answer in supplied current verification and identify unavailable required evidence.",
    "verify_if_stale": "Treat stale evidence as historical unless supplied current verification is present.",
}

EVIDENCE_MODULES = {
    "profile": "Profile evidence describes supplied Product asset state; repeated fields shared with Detection are not independent corroboration.",
    "detection": "Detection evidence is classifier/rule/signal evidence and may contain conflicts or uncertainty.",
    "graph": "Graph evidence is bounded observed topology; disclose truncation, omitted peers, and incomplete scope.",
    "knowledge": "Knowledge evidence is documentation and guidance, not current asset, alert, risk, or peer truth.",
    "incomplete_evidence": "Answer only to the available boundary and state what is unavailable, omitted, stale, or truncated.",
    "contradictory_evidence": "Describe contradictions by source and do not silently resolve them.",
}

MEMORY_MODULES = {
    "working": "Working memory is current conversation continuity, not independently verified operational evidence.",
    "episodic": "Episodic memory is a bounded summary of a prior related investigation episode.",
    "ltm_available": "Only selected active validated long-term memory is authoritative durable memory; current compatible evidence outranks it.",
    "no_active_ltm": "No active validated long-term memory was selected. Candidate records, if counted, are not authoritative and must not be presented as validated memory.",
    "historical_memory_only": "The supplied context is historical memory only; do not imply it is current without verification.",
}

ANALYSIS_MODULES = {
    "threat": "Separate observed threat indicators from inference and hypothesis.",
    "defender_ir": "Prioritize defensible investigation implications and bounded next checks.",
    "noc_operational": "Explain network-operational impact without inferring unavailable protocol purpose or reachability.",
    "strategic": "State material impact and confidence without inflating evidence.",
    "disposition": "When requested, give a bounded disposition and identify the evidence needed to raise confidence.",
}

RESPONSE_MODULES = {
    "brief": "Respond briefly with the conclusion, strongest support, and material limitation.",
    "standard": "Give a concise evidence-based answer with findings, interpretation, and limitations.",
    "deep": "Provide structured findings, evidence, competing explanations, limitations, and practical next checks.",
    "report": "Produce a professional report with scope, findings, evidence, assessment, limitations, disposition, and prioritized actions.",
}


class SynthesizerPromptBuilder:
    """Select prompt modules from validated state and render provider-neutral messages."""

    version = "synth-context-v1"

    def __init__(self) -> None:
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
    ) -> SynthesizerTaskContext:
        memory_package = getattr(snapshot, "memory_context", None)
        selected_count = max(
            int(getattr(long_term_selection, "selected_count", 0) or 0),
            len(getattr(long_term_selection, "memories", ()) or ()),
        )
        memory = SynthesizerMemoryState(
            working_available=bool(
                getattr(memory_package, "working_summary", "")
                or getattr(memory_package, "relevant_turns", ())
                or int(getattr(snapshot, "recent_message_count", 0) or 0)
            ),
            episodic_available=bool(getattr(memory_package, "episode_summaries", ()) or ()),
            ltm_status=str(getattr(long_term_selection, "status", "not_supplied") or "not_supplied"),
            ltm_candidate_count=int(getattr(long_term_selection, "candidate_record_count", 0) or 0),
            ltm_active_count=int(getattr(long_term_selection, "active_record_count", 0) or 0),
            ltm_selected_count=selected_count,
            historical_only=task.evidence_mode == "memory_only",
            compatible_previous_baseline_available=False,
        )
        provider_states = {
            "profile": self._provider_state(results, "asset.get_profile"),
            "detection": self._provider_state(results, "asset.get_detection"),
            "graph": self._provider_state(results, "graph."),
            "knowledge": self._provider_state(results, "knowledge.search"),
        }
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
        return SynthesizerTaskContext(
            task_category=self._task_category(task),
            intent=task.intent,
            entities=task.entities,
            temporal_mode=task.temporal_mode,
            evidence_mode=task.evidence_mode,
            response_depth=self._response_depth(task),
            memory=memory,
            profile=provider_states["profile"],
            detection=provider_states["detection"],
            graph=provider_states["graph"],
            knowledge=provider_states["knowledge"],
            contradiction_count=contradiction_count,
            # TODO: Populate only from a validated deterministic baseline/delta source.
            current_vs_historical_relationship="compatible_baseline_unavailable",
            deterministic_delta_available=False,
            selected_analytical_lenses=self._analytical_lenses(task),
            limitations=limitations,
            output_constraints=(
                "Separate facts, inferences, and hypotheses.",
                "Do not repeat the same evidence across sections.",
                "Use bounded negative language: not observed never means categorically absent.",
                "Use new/changed/appeared/disappeared only when a supplied deterministic compatible-baseline delta supports it.",
            ),
        )

    def render_contract(self, context: SynthesizerTaskContext) -> tuple[str, tuple[str, ...]]:
        modules: list[tuple[str, str]] = [(f"task.{context.task_category}", TASK_MODULES[context.task_category])]
        modules.append((f"temporal.{context.temporal_mode}", TEMPORAL_MODULES[context.temporal_mode]))
        modules.append((f"evidence_mode.{context.evidence_mode}", EVIDENCE_MODE_MODULES[context.evidence_mode]))
        for name, state in (
            ("profile", context.profile),
            ("detection", context.detection),
            ("graph", context.graph),
            ("knowledge", context.knowledge),
        ):
            if state.status != "not_requested":
                modules.append((f"evidence.{name}", EVIDENCE_MODULES[name]))
            if state.status in {"partial", "empty", "unavailable", "not_configured"} or state.truncated:
                modules.append(("evidence.incomplete_evidence", EVIDENCE_MODULES["incomplete_evidence"]))
        if context.contradiction_count:
            modules.append(("evidence.contradictory_evidence", EVIDENCE_MODULES["contradictory_evidence"]))
        if context.memory.working_available:
            modules.append(("memory.working", MEMORY_MODULES["working"]))
        if context.memory.episodic_available:
            modules.append(("memory.episodic", MEMORY_MODULES["episodic"]))
        if context.memory.ltm_selected_count:
            modules.append(("memory.ltm_available", MEMORY_MODULES["ltm_available"]))
        else:
            modules.append(("memory.no_active_ltm", MEMORY_MODULES["no_active_ltm"]))
        if context.memory.historical_only:
            modules.append(("memory.historical_memory_only", MEMORY_MODULES["historical_memory_only"]))
        modules.extend(
            (f"analysis.{name}", ANALYSIS_MODULES[name])
            for name in context.selected_analytical_lenses
        )
        modules.append((f"response.{context.response_depth}", RESPONSE_MODULES[context.response_depth]))
        deduplicated = tuple(dict(modules).items())
        metadata = {
            "intent": context.intent,
            "task_category": context.task_category,
            "entities": list(context.entities),
            "temporal_mode": context.temporal_mode,
            "evidence_mode": context.evidence_mode,
            "response_depth": context.response_depth,
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
        }
        text = (
            "[SOORIN SYNTHESIZER TASK CONTRACT]\n"
            + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n[SELECTED INSTRUCTIONS]\n"
            + "\n".join(f"- {instruction}" for _, instruction in deduplicated)
            + "\n[OUTPUT CONSTRAINTS]\n"
            + "\n".join(f"- {item}" for item in context.output_constraints)
        )
        return text, tuple(name for name, _ in deduplicated)

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
