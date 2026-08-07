"""Typed in-process working and episodic memory contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from src.core.agent.contracts import TaskSpec


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            if task.intent in {"asset_investigation", "graph_neighbors", "graph_followup"}
            else "general"
            if task.intent in {"general_knowledge", "general_conversation", "unclear"}
            else task.intent
        )
        scope = (
            "topology"
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
    limitations: tuple[str, ...] = ()
    last_providers: tuple[str, ...] = ()
    last_scope: str = "none"
    turn_count: int = 0

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


@dataclass(frozen=True)
class TurnReference:
    """Bounded metadata for one completed turn; transcript text stays in ChatRepository."""

    request_id: str
    context_key: MemoryContextKey
    created_at: str = field(default_factory=_now)


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


@dataclass(frozen=True)
class MemoryContextPackage:
    """Storage-neutral, bounded memory sections consumed by context composition."""

    working_summary: str = ""
    relevant_turns: tuple[RelevantTurn, ...] = ()
    episode_summaries: tuple[EpisodeRecord, ...] = ()
    active_entities: tuple[str, ...] = ()
    estimated_tokens: int = 0
    omitted: tuple[str, ...] = ()

    def model_messages(self) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if self.working_summary:
            messages.append(
                {
                    "role": "system",
                    "content": f"[SOORIN CONVERSATION SUMMARY]\n{self.working_summary}",
                }
            )
        if self.episode_summaries:
            summaries = "\n".join(
                episode.compact_summary
                for episode in self.episode_summaries
                if episode.compact_summary
            )
            if summaries:
                messages.append(
                    {
                        "role": "system",
                        "content": f"[SOORIN RELEVANT EPISODES]\n{summaries}",
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

    def clear_session(self, session_id: str) -> None:
        self._working.pop(session_id, None)
        self._episodes.pop(session_id, None)
