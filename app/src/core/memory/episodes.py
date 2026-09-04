"""Typed in-process working and episodic memory contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import uuid4

from src.core.agent.contracts import TaskSpec
from src.core.memory.baselines import BaselineProjection, InvestigationBaseline
from src.core.memory.long_term import RetrievedLongTermMemory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WorkingFact:
    """Bounded explicit user fact scoped to one conversation."""

    key: str
    value: str
    fact_type: str = "user_provided"
    scope: Literal["conversation", "entity"] = "conversation"
    entity_ids: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)
    source_request_id: str = ""

    def __post_init__(self) -> None:
        if self.scope not in {"conversation", "entity"}:
            raise ValueError("Unsupported working fact scope")
        if self.scope == "entity" and not self.entity_ids:
            raise ValueError("Entity-scoped working facts require an entity binding")


@dataclass(frozen=True)
class EntityVisit:
    """One bounded chronological investigation visit; never an active cursor."""

    sequence: int
    ordered_entity_ids: tuple[str, ...]
    task_family: str
    episode_id: str = ""
    created_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("Entity visit sequence must be positive")
        if not 1 <= len(self.ordered_entity_ids) <= 2:
            raise ValueError("Entity visits require one or two ordered entities")


@dataclass(frozen=True)
class MemoryContextKey:
    """Stable investigation identity without using the literal user message."""

    entities: tuple[str, ...] = ()
    topic_family: str = "general"
    relationship_mode: str = "none"
    scope_family: str = "none"

    @classmethod
    def from_task(cls, task: TaskSpec) -> "MemoryContextKey":
        entities = tuple(task.entities)
        relationship = task.relationship_mode or "none"
        if task.intent == "graph_relationships" and relationship == "compare":
            entities = tuple(sorted(entities))
        topic = (
            "asset_comparison"
            if task.intent == "graph_relationships" and relationship == "compare"
            else "asset_investigation"
            if task.intent in {"asset_investigation", "graph_neighbors", "graph_followup", "memory_recall"}
            else "general"
            if task.intent in {"general_knowledge", "general_conversation", "unclear"}
            else task.intent
        )
        scope = (
            "asset"
            if topic == "asset_investigation"
            else "topology"
            if task.scope in {"node_summary", "one_hop", "two_hop", "full_neighbors"}
            else task.scope or "none"
        )
        return cls(
            entities=entities,
            topic_family=topic,
            relationship_mode=relationship,
            scope_family=scope,
        )

    @property
    def identity(self) -> str:
        return ":".join(
            (
                "|".join(self.entities) or "none",
                self.topic_family,
                self.relationship_mode,
                self.scope_family,
            )
        )


@dataclass
class EpisodeRecord:
    episode_id: str
    session_id: str
    context_key: MemoryContextKey
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    compact_summary: str = ""
    supported_findings: tuple[str, ...] = ()
    working_facts: tuple[WorkingFact, ...] = ()
    inferred_role: tuple[str, ...] = ()
    key_findings: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    next_checks: tuple[str, ...] = ()
    evidence_scope: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    last_providers: tuple[str, ...] = ()
    last_scope: str = "none"
    turn_count: int = 0
    baseline: InvestigationBaseline | None = None

    @classmethod
    def create(cls, session_id: str, context_key: MemoryContextKey) -> "EpisodeRecord":
        return cls(episode_id=uuid4().hex[:12], session_id=session_id, context_key=context_key)


@dataclass
class WorkingMemory:
    session_id: str
    context_key: MemoryContextKey
    episode_id: str
    latest_user_turn: str = ""
    latest_assistant_turn: str = ""
    compact_summary: str = ""
    last_providers: tuple[str, ...] = ()
    last_scope: str = "none"
    limitations: tuple[str, ...] = ()
    working_facts: tuple[WorkingFact, ...] = ()
    baseline: InvestigationBaseline | None = None


@dataclass(frozen=True)
class TurnReference:
    """Bounded durable digest; the transcript remains authoritative when available."""

    request_id: str
    context_key: MemoryContextKey
    created_at: str = field(default_factory=_now)
    user_digest: str = ""
    assistant_digest: str = ""
    workflow_status: str = "completed"


@dataclass(frozen=True)
class RelevantTurn:
    """One selected same-conversation turn prepared for model context."""

    request_id: str
    context_key: MemoryContextKey
    user_content: str
    assistant_content: str
    created_at: str
    retrieval_reason: str
    estimated_tokens: int
    source_representation: str = "raw"


@dataclass(frozen=True)
class MemoryContextPackage:
    """Storage-neutral, bounded memory sections consumed by context composition."""

    working_summary: str = ""
    relevant_turns: tuple[RelevantTurn, ...] = ()
    episode_summaries: tuple[EpisodeRecord, ...] = ()
    long_term_memories: tuple[RetrievedLongTermMemory, ...] = ()
    working_facts: tuple[WorkingFact, ...] = ()
    entity_timeline: tuple[EntityVisit, ...] = ()
    active_entities: tuple[str, ...] = ()
    estimated_tokens: int = 0
    omitted: tuple[str, ...] = ()

    def model_messages(self) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        normalized_long_term = {
            item.memory.statement.strip().casefold()
            for item in self.long_term_memories
        }
        if self.working_facts:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "[SOORIN USER-PROVIDED WORKING FACTS — CONVERSATION-SCOPED]\n"
                        "Authority: explicit user assertions only; not independently verified operational evidence.\n"
                    ) + "\n".join(
                        f"- scope={item.scope}; entities={','.join(item.entity_ids) or 'conversation'}; "
                        f"{item.key}: {item.value}"
                        for item in self.working_facts
                    ),
                }
            )
        if self.working_summary and self.working_summary.strip().casefold() not in normalized_long_term:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "[SOORIN CONVERSATION SUMMARY]\n"
                        "Source: compact working summary — conversation-derived.\n"
                        "Authority: bounded continuity summary; do not treat it as current verification.\n"
                        f"{self.working_summary}"
                    ),
                }
            )
        if self.episode_summaries:
            summaries = "\n".join(
                episode.compact_summary or "\n".join(
                    (
                        *(f"contradiction: {item}" for item in episode.contradictions),
                        *(f"finding: {item}" for item in episode.key_findings),
                        *(f"next_check: {item}" for item in episode.next_checks),
                    )
                )
                for episode in self.episode_summaries
                if (episode.compact_summary or episode.contradictions or episode.key_findings)
                and (episode.compact_summary or "").strip().casefold() not in normalized_long_term
            )
            if summaries:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "[SOORIN EPISODIC INVESTIGATION HISTORY — CONVERSATION-DERIVED]\n"
                            "Authority: historical continuity only, not fresh operational verification.\n"
                            f"{summaries}"
                        ),
                    }
                )
        if self.entity_timeline:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "[SOORIN THREAD CHRONOLOGY — STRUCTURAL HISTORY]\n"
                        "Source: ordered investigation visits; historical continuity only.\n"
                        + "\n".join(
                            f"- visit {item.sequence}: {', '.join(item.ordered_entity_ids)} "
                            f"({item.task_family})"
                            for item in self.entity_timeline
                        )
                    ),
                }
            )
        if self.long_term_memories:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "[SOORIN VALIDATED LONG-TERM MEMORY]\n"
                        "Source: Product-canonical durable memory. Authority: validated historical findings; "
                        "current operational evidence overrides memory.\n"
                        + "\n".join(item.model_text() for item in self.long_term_memories)
                    ),
                }
            )
        if self.relevant_turns:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "[SOORIN SHORT-TERM RECENT TURN CONTEXT]\n"
                        "Authority: bounded conversation history. It may contain user assertions or prior assistant text, "
                        "not independent current operational evidence."
                    ),
                }
            )
        for turn in self.relevant_turns:
            messages.extend(
                (
                    {"role": "user", "content": turn.user_content},
                    {"role": "assistant", "content": turn.assistant_content},
                )
            )
        return messages


class WorkingMemoryStore(Protocol):
    def get_working(self, session_id: str) -> WorkingMemory | None: ...

    def set_working(self, state: WorkingMemory) -> None: ...


class EpisodicMemoryStore(Protocol):
    def list_episodes(self, session_id: str) -> tuple[EpisodeRecord, ...]: ...

    def add_episode(self, episode: EpisodeRecord) -> None: ...

    def take_episode(
        self,
        session_id: str,
        context_key: MemoryContextKey,
    ) -> EpisodeRecord | None: ...


class MemoryRepository(WorkingMemoryStore, EpisodicMemoryStore, Protocol):
    """Future-compatible boundary for durable memory implementations."""

    def clear_session(self, session_id: str) -> None: ...


class InMemoryMemoryRepository:
    def __init__(self, max_episodes_per_session: int = 20) -> None:
        self.max_episodes_per_session = max(1, max_episodes_per_session)
        self._working: dict[str, WorkingMemory] = {}
        self._episodes: dict[str, list[EpisodeRecord]] = {}

    def get_working(self, session_id: str) -> WorkingMemory | None:
        return self._working.get(session_id)

    def set_working(self, state: WorkingMemory) -> None:
        self._working[state.session_id] = state

    def list_episodes(self, session_id: str) -> tuple[EpisodeRecord, ...]:
        return tuple(self._episodes.get(session_id, ()))

    def add_episode(self, episode: EpisodeRecord) -> None:
        records = self._episodes.setdefault(episode.session_id, [])
        records.append(episode)
        self._episodes[episode.session_id] = records[-self.max_episodes_per_session :]

    def take_episode(
        self,
        session_id: str,
        context_key: MemoryContextKey,
    ) -> EpisodeRecord | None:
        records = self._episodes.get(session_id, [])
        for index in range(len(records) - 1, -1, -1):
            if records[index].context_key == context_key:
                episode = records.pop(index)
                if records:
                    self._episodes[session_id] = records
                else:
                    self._episodes.pop(session_id, None)
                return episode
        return None

    def clear_session(self, session_id: str) -> None:
        self._working.pop(session_id, None)
        self._episodes.pop(session_id, None)
