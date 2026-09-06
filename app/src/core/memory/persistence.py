"""Typed contracts for opt-in local chat and compact thread persistence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.core.identity import RequestIdentity, normalize_identifier
from src.core.memory.baselines import (
    MAX_BASELINE_PROJECTIONS,
    MAX_BASELINE_PROJECTION_BYTES,
    MAX_BASELINE_TOTAL_BYTES,
    BaselineProjection,
    InvestigationBaseline,
)
from src.core.memory.episodes import (
    EntityVisit,
    EpisodeRecord,
    MemoryContextKey,
    TurnReference,
    WorkingFact,
    WorkingMemory,
)
from src.core.memory.routing_state import SessionRoutingState


LOCAL_SCHEMA_VERSION = 7
THREAD_STATE_SCHEMA_VERSION = 4
_COMPATIBLE_THREAD_STATE_SCHEMA_VERSIONS = frozenset({3, 4})
MAX_THREAD_STATE_BYTES = 16_384
MAX_CHAT_CONTENT_CHARS = 100_000
MAX_CONVERSATION_TITLE_CHARS = 256
MAX_CHAT_READ_LIMIT = 200
MAX_ENTITY_TIMELINE_VISITS = 24


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalPersistenceError(RuntimeError):
    """Base class for safely classified local persistence failures."""


class LocalPersistenceOwnershipError(LocalPersistenceError):
    """The requested local record belongs to a different user or conversation."""


class LocalPersistenceConflictError(LocalPersistenceError):
    """An idempotency or optimistic-revision conflict was detected."""


class LocalPersistenceSchemaError(LocalPersistenceError):
    """Stored data uses an incompatible or malformed schema."""


class LocalPersistenceQuotaError(LocalPersistenceError):
    """A bounded local store cannot safely admit another protected record."""


@dataclass(frozen=True)
class MemoryStoragePolicy:
    """Portable owner-scoped storage bounds enforced by persistence adapters."""

    max_conversations_per_user: int = 100
    max_messages_per_conversation: int = 200
    max_active_long_term_per_user: int = 500
    max_candidate_long_term_per_user: int = 250

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class LocalConversation:
    conversation_id: str
    user_id: str
    session_id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LocalChatMessage:
    message_id: str
    conversation_id: str
    request_id: str
    role: str
    content: str
    status: str
    position: int
    created_at: str


@dataclass(frozen=True)
class LocalUser:
    """Local-development user identity; credential hashes never leave storage."""

    user_id: str
    username: str
    created_at: str


@dataclass(frozen=True)
class LocalRequestCommit:
    user_id: str
    conversation_id: str
    request_id: str
    status: str
    created_at: str
    updated_at: str


def _bounded_optional(value: Any, *, field_name: str, maximum: int = 128) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise LocalPersistenceSchemaError(f"Invalid persisted {field_name}.")
    return text


def _ipv4_values(values: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise LocalPersistenceSchemaError(f"Invalid persisted {field_name}.")
    normalized: list[str] = []
    for raw in values:
        try:
            value = str(ipaddress.ip_address(str(raw).strip()))
        except ValueError as exc:
            raise LocalPersistenceSchemaError(
                f"Invalid persisted {field_name}."
            ) from exc
        if ":" in value:
            raise LocalPersistenceSchemaError(f"Invalid persisted {field_name}.")
        if value not in normalized:
            normalized.append(value)
    if len(normalized) > 2:
        raise LocalPersistenceSchemaError(f"Invalid persisted {field_name}.")
    return tuple(normalized)


def _boolean(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise LocalPersistenceSchemaError(f"Invalid persisted {field_name}.")
    return value


def _context_key_payload(value: MemoryContextKey) -> dict[str, Any]:
    return {
        "entities": list(value.entities),
        "topic_family": value.topic_family,
        "relationship_mode": value.relationship_mode,
        "scope_family": value.scope_family,
    }


def _context_key_from_payload(value: Any) -> MemoryContextKey:
    if not isinstance(value, dict):
        raise LocalPersistenceSchemaError("Invalid persisted memory context key.")
    if set(value) - {"entities", "topic_family", "relationship_mode", "scope_family"}:
        raise LocalPersistenceSchemaError("Invalid persisted memory context key.")
    return MemoryContextKey(
        entities=_ipv4_values(value.get("entities", []), field_name="context entities"),
        topic_family=_bounded_optional(value.get("topic_family"), field_name="topic_family") or "general",
        relationship_mode=_bounded_optional(value.get("relationship_mode"), field_name="relationship_mode") or "none",
        scope_family=_bounded_optional(value.get("scope_family"), field_name="scope_family") or "none",
    )


def _bounded_text(value: Any, *, maximum: int) -> str:
    text = str(value or "")
    if len(text) > maximum:
        raise LocalPersistenceSchemaError("Persisted memory text exceeds its bound.")
    return text


def _bounded_strings(value: Any, *, field_name: str, maximum_items: int, maximum_chars: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum_items:
        raise LocalPersistenceSchemaError(f"Invalid persisted {field_name}.")
    return tuple(_bounded_text(item, maximum=maximum_chars) for item in value)


def _working_memory_from_payload(
    value: Any,
    session_id: str,
    owner_id: str,
) -> WorkingMemory | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LocalPersistenceSchemaError("Invalid persisted working memory.")
    context_key = _context_key_from_payload(value.get("context_key"))
    return WorkingMemory(
        session_id=session_id,
        context_key=context_key,
        episode_id=_bounded_optional(value.get("episode_id"), field_name="episode_id") or "",
        compact_summary=_bounded_text(value.get("compact_summary"), maximum=4_000),
        last_providers=_bounded_strings(value.get("last_providers", []), field_name="last_providers", maximum_items=8, maximum_chars=64),
        last_scope=_bounded_optional(value.get("last_scope"), field_name="last_scope") or "none",
        limitations=_bounded_strings(value.get("limitations", []), field_name="limitations", maximum_items=8, maximum_chars=200),
        working_facts=_working_facts_from_payload(value.get("working_facts", [])),
        baseline=_baseline_from_payload(
            value.get("baseline"),
            owner_id=owner_id,
            context_entities=context_key.entities,
        ),
    )


def _working_facts_from_payload(value: Any) -> tuple[WorkingFact, ...]:
    if not isinstance(value, list) or len(value) > 20:
        raise LocalPersistenceSchemaError("Invalid persisted working facts.")
    facts: list[WorkingFact] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"key", "value", "fact_type", "scope", "entity_ids", "created_at", "source_request_id"}:
            raise LocalPersistenceSchemaError("Invalid persisted working fact.")
        facts.append(
            WorkingFact(
                key=_bounded_optional(item.get("key"), field_name="working fact key", maximum=64) or "",
                value=_bounded_text(item.get("value"), maximum=600),
                fact_type=_bounded_optional(item.get("fact_type"), field_name="working fact type", maximum=64) or "user_provided",
                scope=_bounded_optional(item.get("scope"), field_name="working fact scope", maximum=32) or "conversation",
                entity_ids=_bounded_strings(
                    item.get("entity_ids", []),
                    field_name="working fact entity ids",
                    maximum_items=2,
                    maximum_chars=64,
                ),
                created_at=_bounded_text(item.get("created_at"), maximum=64),
                source_request_id=_bounded_optional(
                    item.get("source_request_id") or None,
                    field_name="working fact source request id",
                    maximum=128,
                ) or "",
            )
        )
    return tuple(facts)


def _working_facts_payload(value: tuple[WorkingFact, ...]) -> list[dict[str, Any]]:
    return [
        {
            "key": item.key,
            "value": item.value,
            "fact_type": item.fact_type,
            "scope": item.scope,
            "entity_ids": list(item.entity_ids),
            "created_at": item.created_at,
            "source_request_id": item.source_request_id,
        }
        for item in value
    ]


def _entity_visits_payload(value: tuple[EntityVisit, ...]) -> list[dict[str, Any]]:
    return [
        {
            "sequence": item.sequence,
            "ordered_entity_ids": list(item.ordered_entity_ids),
            "task_family": item.task_family,
            "episode_id": item.episode_id,
            "created_at": item.created_at,
        }
        for item in value[-MAX_ENTITY_TIMELINE_VISITS:]
    ]


def _entity_visits_from_payload(value: Any) -> tuple[EntityVisit, ...]:
    if not isinstance(value, list) or len(value) > MAX_ENTITY_TIMELINE_VISITS:
        raise LocalPersistenceSchemaError("Invalid persisted entity timeline.")
    visits: list[EntityVisit] = []
    previous_sequence = 0
    for item in value:
        if not isinstance(item, dict) or set(item) - {
            "sequence", "ordered_entity_ids", "task_family", "episode_id", "created_at"
        }:
            raise LocalPersistenceSchemaError("Invalid persisted entity visit.")
        sequence = item.get("sequence")
        if not isinstance(sequence, int) or sequence <= previous_sequence:
            raise LocalPersistenceSchemaError("Invalid persisted entity visit sequence.")
        entities = _ipv4_values(
            item.get("ordered_entity_ids", []), field_name="entity visit entities"
        )
        if not entities:
            raise LocalPersistenceSchemaError("Persisted entity visit requires an entity.")
        visits.append(
            EntityVisit(
                sequence=sequence,
                ordered_entity_ids=entities,
                task_family=_bounded_optional(
                    item.get("task_family"), field_name="entity visit task family", maximum=96
                ) or "unknown",
                episode_id=_bounded_optional(
                    item.get("episode_id") or None,
                    field_name="entity visit episode",
                    maximum=64,
                ) or "",
                created_at=_bounded_text(item.get("created_at"), maximum=64),
            )
        )
        previous_sequence = sequence
    return tuple(visits)


def _baseline_payload(value: InvestigationBaseline) -> dict[str, Any] | None:
    if not value.projections or len(value.projections) > MAX_BASELINE_PROJECTIONS:
        return None
    projections: list[dict[str, Any]] = []
    used_bytes = 0
    for item in value.projections:
        encoded = json.dumps(
            item.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        used_bytes += len(encoded)
        if (
            len(encoded) > MAX_BASELINE_PROJECTION_BYTES
            or used_bytes > MAX_BASELINE_TOTAL_BYTES
            or hashlib.sha256(encoded).hexdigest() != item.fingerprint
        ):
            return None
        projections.append({
            "capability": item.capability,
            "entity_ids": list(item.entity_ids),
            "view": item.view,
            "schema_version": item.schema_version,
            "evidence_classes": list(item.evidence_classes),
            "payload": item.payload,
            "valid_at": item.valid_at,
            "completeness": item.completeness,
            "fingerprint": item.fingerprint,
            "scope": item.scope,
            "direction": item.direction,
            "depth": item.depth,
            "truncated": item.truncated,
        })
    return {
        "entity_ids": list(value.entity_ids),
        "captured_at": value.captured_at,
        "source_request_id": value.source_request_id,
        "scope": value.scope,
        "owner_id": value.owner_id,
        "projections": projections,
    }


def _baseline_from_payload(
    value: Any,
    *,
    owner_id: str,
    context_entities: tuple[str, ...],
) -> InvestigationBaseline | None:
    if value is None:
        return None
    try:
        if not isinstance(value, dict):
            raise LocalPersistenceSchemaError("Invalid persisted investigation baseline.")
        entities = _ipv4_values(value.get("entity_ids", []), field_name="baseline entities")
        persisted_owner = _bounded_optional(
            value.get("owner_id"), field_name="baseline owner", maximum=128
        ) or ""
        if (owner_id and persisted_owner != owner_id) or (
            context_entities and set(entities) != set(context_entities)
        ):
            return None
        raw_projections = value.get("projections", [])
        if not isinstance(raw_projections, list) or len(raw_projections) > MAX_BASELINE_PROJECTIONS:
            raise LocalPersistenceSchemaError("Invalid persisted baseline projections.")
        projections: list[BaselineProjection] = []
        used_bytes = 0
        for item in raw_projections:
            if not isinstance(item, dict):
                raise LocalPersistenceSchemaError("Invalid persisted baseline projection.")
            payload = item.get("payload")
            if not isinstance(payload, dict):
                raise LocalPersistenceSchemaError("Invalid persisted baseline payload.")
            encoded = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded) > MAX_BASELINE_PROJECTION_BYTES:
                return None
            used_bytes += len(encoded)
            if used_bytes > MAX_BASELINE_TOTAL_BYTES:
                return None
            projection_entities = _ipv4_values(
                item.get("entity_ids", []), field_name="baseline projection entities"
            )
            if not projection_entities or not set(projection_entities).issubset(entities):
                return None
            completeness = str(item.get("completeness") or "")
            if completeness not in {"complete", "partial"}:
                return None
            expected_fingerprint = hashlib.sha256(encoded).hexdigest()
            supplied_fingerprint = _bounded_text(item.get("fingerprint"), maximum=64)
            if supplied_fingerprint != expected_fingerprint:
                return None
            depth = item.get("depth", 0)
            if not isinstance(depth, int) or not 0 <= depth <= 10:
                return None
            projections.append(BaselineProjection(
                capability=_bounded_optional(item.get("capability"), field_name="baseline capability", maximum=128) or "",
                entity_ids=projection_entities,
                view=_bounded_optional(item.get("view"), field_name="baseline view", maximum=128) or "",
                schema_version=_bounded_optional(item.get("schema_version"), field_name="baseline schema", maximum=128) or "",
                evidence_classes=_bounded_strings(item.get("evidence_classes", []), field_name="baseline evidence classes", maximum_items=8, maximum_chars=128),
                payload=payload,
                valid_at=_bounded_text(item.get("valid_at"), maximum=64),
                completeness=completeness,
                fingerprint=supplied_fingerprint,
                scope=_bounded_optional(item.get("scope"), field_name="baseline scope", maximum=128) or "none",
                direction=_bounded_optional(item.get("direction"), field_name="baseline direction", maximum=32) or "none",
                depth=depth,
                truncated=_boolean(item.get("truncated", False), field_name="baseline truncated"),
            ))
        if not projections:
            return None
        return InvestigationBaseline(
            entity_ids=entities,
            captured_at=_bounded_text(value.get("captured_at"), maximum=64),
            source_request_id=_bounded_optional(value.get("source_request_id"), field_name="baseline request id", maximum=128) or "",
            scope=_bounded_optional(value.get("scope"), field_name="baseline scope", maximum=128) or "none",
            projections=tuple(projections),
            owner_id=persisted_owner,
        )
    except (LocalPersistenceSchemaError, TypeError, ValueError):
        return None
def _turn_references_from_payload(value: Any) -> tuple[TurnReference, ...]:
    if not isinstance(value, list) or len(value) > 20:
        raise LocalPersistenceSchemaError("Invalid persisted recent turn references.")
    records: list[TurnReference] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {
            "request_id", "context_key", "created_at", "user_digest", "assistant_digest", "workflow_status"
        }:
            raise LocalPersistenceSchemaError("Invalid persisted recent turn reference.")
        records.append(
            TurnReference(
                request_id=_bounded_optional(item.get("request_id"), field_name="request_id") or "",
                context_key=_context_key_from_payload(item.get("context_key")),
                created_at=_bounded_text(item.get("created_at"), maximum=64),
                user_digest=_bounded_text(item.get("user_digest"), maximum=360),
                assistant_digest=_bounded_text(item.get("assistant_digest"), maximum=560),
                workflow_status=_bounded_optional(
                    item.get("workflow_status") or None,
                    field_name="turn workflow status",
                    maximum=64,
                ) or "completed",
            )
        )
    return tuple(records)


def _episode_payload(value: EpisodeRecord) -> dict[str, Any]:
    payload = {
        "episode_id": value.episode_id,
        "context_key": _context_key_payload(value.context_key),
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "compact_summary": value.compact_summary,
        "working_facts": _working_facts_payload(value.working_facts),
        "inferred_role": list(value.inferred_role),
        "key_findings": list(value.key_findings),
        "contradictions": list(value.contradictions),
        "unresolved_questions": list(value.unresolved_questions),
        "next_checks": list(value.next_checks),
        "evidence_scope": list(value.evidence_scope),
        "limitations": list(value.limitations),
        "last_providers": list(value.last_providers),
        "last_scope": value.last_scope,
        "turn_count": value.turn_count,
    }
    baseline = _baseline_payload(value.baseline) if value.baseline is not None else None
    if baseline is not None:
        payload["baseline"] = baseline
    return payload


def _episodes_from_payload(
    value: Any,
    session_id: str,
    owner_id: str,
) -> tuple[EpisodeRecord, ...]:
    if not isinstance(value, list) or len(value) > 20:
        raise LocalPersistenceSchemaError("Invalid persisted recent episodes.")
    records: list[EpisodeRecord] = []
    for item in value:
        if not isinstance(item, dict):
            raise LocalPersistenceSchemaError("Invalid persisted episode.")
        turn_count = item.get("turn_count", 0)
        if not isinstance(turn_count, int) or turn_count < 0:
            raise LocalPersistenceSchemaError("Invalid persisted episode turn count.")
        context_key = _context_key_from_payload(item.get("context_key"))
        records.append(
            EpisodeRecord(
                episode_id=_bounded_optional(item.get("episode_id"), field_name="episode_id") or "",
                session_id=session_id,
                context_key=context_key,
                created_at=_bounded_text(item.get("created_at"), maximum=64),
                updated_at=_bounded_text(item.get("updated_at"), maximum=64),
                compact_summary=_bounded_text(item.get("compact_summary"), maximum=4_000),
                working_facts=_working_facts_from_payload(item.get("working_facts", [])),
                inferred_role=_bounded_strings(item.get("inferred_role", []), field_name="inferred_role", maximum_items=8, maximum_chars=300),
                key_findings=_bounded_strings(item.get("key_findings", []), field_name="key_findings", maximum_items=12, maximum_chars=300),
                contradictions=_bounded_strings(item.get("contradictions", []), field_name="contradictions", maximum_items=8, maximum_chars=400),
                unresolved_questions=_bounded_strings(item.get("unresolved_questions", []), field_name="unresolved_questions", maximum_items=8, maximum_chars=300),
                next_checks=_bounded_strings(item.get("next_checks", []), field_name="next_checks", maximum_items=8, maximum_chars=300),
                evidence_scope=_bounded_strings(item.get("evidence_scope", []), field_name="evidence_scope", maximum_items=8, maximum_chars=160),
                limitations=_bounded_strings(item.get("limitations", []), field_name="limitations", maximum_items=8, maximum_chars=200),
                last_providers=_bounded_strings(item.get("last_providers", []), field_name="last_providers", maximum_items=8, maximum_chars=64),
                last_scope=_bounded_optional(item.get("last_scope"), field_name="last_scope") or "none",
                turn_count=turn_count,
                baseline=_baseline_from_payload(
                    item.get("baseline"),
                    owner_id=owner_id,
                    context_entities=context_key.entities,
                ),
            )
        )
    return tuple(records)


@dataclass(frozen=True)
class ThreadMemoryState:
    """Versioned bounded thread memory; never arbitrary workflow or evidence state."""

    thread_key: str
    user_id: str | None
    conversation_id: str | None
    session_id: str
    active_entities: tuple[str, ...] = ()
    last_resolved_entities: tuple[str, ...] = ()
    previous_entity_count: int = 0
    previous_entity_mode: str | None = None
    last_provider: str | None = None
    last_providers: tuple[str, ...] = ()
    previous_intent: str | None = None
    previous_scope: str | None = None
    previous_direction: str | None = None
    previous_depth: int | None = None
    previous_requires_detection: bool = False
    previous_requires_asset_profile: bool = False
    working_memory: WorkingMemory | None = None
    recent_turn_references: tuple[TurnReference, ...] = ()
    recent_episodes: tuple[EpisodeRecord, ...] = ()
    entity_timeline: tuple[EntityVisit, ...] = ()
    summary_updated_at: str = ""
    summary_source_request_id: str = ""
    summary_size_tokens: int = 0
    updated_at: str = ""
    revision: int = 0
    schema_version: int = THREAD_STATE_SCHEMA_VERSION

    @classmethod
    def from_routing_state(
        cls,
        identity: RequestIdentity,
        state: SessionRoutingState,
        *,
        revision: int = 0,
        working_memory: WorkingMemory | None = None,
        recent_turn_references: tuple[TurnReference, ...] = (),
        recent_episodes: tuple[EpisodeRecord, ...] = (),
        entity_timeline: tuple[EntityVisit, ...] | None = None,
        summary_updated_at: str = "",
        summary_source_request_id: str = "",
        summary_size_tokens: int = 0,
    ) -> "ThreadMemoryState":
        return cls(
            thread_key=identity.thread_key,
            user_id=identity.user_id,
            conversation_id=identity.conversation_id,
            session_id=identity.session_id,
            active_entities=state.active_entities,
            last_resolved_entities=state.last_resolved_entities,
            previous_entity_count=state.previous_entity_count,
            previous_entity_mode=state.previous_entity_mode,
            last_provider=state.last_provider,
            last_providers=state.last_providers,
            previous_intent=state.previous_intent,
            previous_scope=state.previous_scope,
            previous_direction=state.previous_direction,
            previous_depth=state.previous_depth,
            previous_requires_detection=state.previous_requires_detection,
            previous_requires_asset_profile=state.previous_requires_asset_profile,
            working_memory=working_memory,
            recent_turn_references=recent_turn_references,
            recent_episodes=recent_episodes,
            entity_timeline=(
                state.entity_timeline if entity_timeline is None else entity_timeline
            ),
            summary_updated_at=summary_updated_at,
            summary_source_request_id=summary_source_request_id,
            summary_size_tokens=summary_size_tokens,
            updated_at=utc_now(),
            revision=revision,
        )

    def to_routing_state(self) -> SessionRoutingState:
        return SessionRoutingState(
            active_entities=self.active_entities,
            last_resolved_entities=self.last_resolved_entities,
            previous_entity_count=self.previous_entity_count,
            previous_entity_mode=self.previous_entity_mode,
            last_provider=self.last_provider,
            last_providers=self.last_providers,
            previous_intent=self.previous_intent,
            previous_scope=self.previous_scope,
            previous_direction=self.previous_direction,
            previous_depth=self.previous_depth,
            previous_requires_detection=self.previous_requires_detection,
            previous_requires_asset_profile=self.previous_requires_asset_profile,
            entity_timeline=self.entity_timeline,
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "active_entities": list(self.active_entities),
            "last_resolved_entities": list(self.last_resolved_entities),
            "previous_entity_count": self.previous_entity_count,
            "previous_entity_mode": self.previous_entity_mode,
            "last_provider": self.last_provider,
            "last_providers": list(self.last_providers),
            "previous_intent": self.previous_intent,
            "previous_scope": self.previous_scope,
            "previous_direction": self.previous_direction,
            "previous_depth": self.previous_depth,
            "previous_requires_detection": self.previous_requires_detection,
            "previous_requires_asset_profile": self.previous_requires_asset_profile,
            "recent_turn_references": [
                {
                    "request_id": item.request_id,
                    "context_key": _context_key_payload(item.context_key),
                    "created_at": item.created_at,
                    "user_digest": item.user_digest,
                    "assistant_digest": item.assistant_digest,
                    "workflow_status": item.workflow_status,
                }
                for item in self.recent_turn_references
            ],
            "recent_episodes": [_episode_payload(item) for item in self.recent_episodes],
            "entity_timeline": _entity_visits_payload(self.entity_timeline),
            "summary_updated_at": self.summary_updated_at,
            "summary_source_request_id": self.summary_source_request_id,
            "summary_size_tokens": self.summary_size_tokens,
        }
        if self.working_memory is not None:
            payload["working_memory"] = {
                "context_key": _context_key_payload(self.working_memory.context_key),
                "episode_id": self.working_memory.episode_id,
                "compact_summary": self.working_memory.compact_summary,
                "working_facts": _working_facts_payload(self.working_memory.working_facts),
                "last_providers": list(self.working_memory.last_providers),
                "last_scope": self.working_memory.last_scope,
                "limitations": list(self.working_memory.limitations),
            }
            baseline = (
                _baseline_payload(self.working_memory.baseline)
                if self.working_memory.baseline is not None
                else None
            )
            if baseline is not None:
                payload["working_memory"]["baseline"] = baseline

        def payload_size() -> int:
            return len(json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"))

        for episode in payload["recent_episodes"]:
            if payload_size() <= MAX_THREAD_STATE_BYTES:
                break
            episode.pop("baseline", None)
        if payload_size() > MAX_THREAD_STATE_BYTES and "working_memory" in payload:
            payload["working_memory"].pop("baseline", None)
        return payload

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        thread_key: str,
        user_id: str | None,
        conversation_id: str | None,
        session_id: str,
        updated_at: str,
        revision: int,
        schema_version: int,
    ) -> "ThreadMemoryState":
        if schema_version not in _COMPATIBLE_THREAD_STATE_SCHEMA_VERSIONS:
            raise LocalPersistenceSchemaError(
                "Persisted thread-state schema version is incompatible."
            )
        if not isinstance(payload, dict):
            raise LocalPersistenceSchemaError("Persisted thread state must be an object.")
        allowed = {
            "active_entities",
            "last_resolved_entities",
            "previous_entity_count",
            "previous_entity_mode",
            "last_provider",
            "last_providers",
            "previous_intent",
            "previous_scope",
            "previous_direction",
            "previous_depth",
            "previous_requires_detection",
            "previous_requires_asset_profile",
            "working_memory",
            "recent_turn_references",
            "recent_episodes",
            "entity_timeline",
            "summary_updated_at",
            "summary_source_request_id",
            "summary_size_tokens",
        }
        if set(payload) - allowed:
            raise LocalPersistenceSchemaError("Persisted thread state has unknown fields.")
        active_entities = _ipv4_values(
            payload.get("active_entities", []),
            field_name="active_entities",
        )
        last_resolved = _ipv4_values(
            payload.get("last_resolved_entities", []),
            field_name="last_resolved_entities",
        )
        providers = payload.get("last_providers", [])
        if not isinstance(providers, (list, tuple)) or len(providers) > 8:
            raise LocalPersistenceSchemaError("Invalid persisted last_providers.")
        previous_count = payload.get("previous_entity_count", 0)
        previous_depth = payload.get("previous_depth")
        if not isinstance(previous_count, int) or not 0 <= previous_count <= 2:
            raise LocalPersistenceSchemaError("Invalid persisted previous_entity_count.")
        if previous_depth is not None and (
            not isinstance(previous_depth, int) or not 0 <= previous_depth <= 10
        ):
            raise LocalPersistenceSchemaError("Invalid persisted previous_depth.")
        if revision < 0:
            raise LocalPersistenceSchemaError("Invalid persisted revision.")
        normalized_owner = normalize_identifier(user_id, field_name="user_id") if user_id else ""
        working_memory = _working_memory_from_payload(
            payload.get("working_memory"), session_id, normalized_owner
        )
        turn_references = _turn_references_from_payload(payload.get("recent_turn_references", []))
        episodes = _episodes_from_payload(
            payload.get("recent_episodes", []), session_id, normalized_owner
        )
        timeline = _entity_visits_from_payload(payload.get("entity_timeline", []))
        summary_size = payload.get("summary_size_tokens", 0)
        if not isinstance(summary_size, int) or not 0 <= summary_size <= 16_384:
            raise LocalPersistenceSchemaError("Invalid persisted summary_size_tokens.")
        return cls(
            thread_key=normalize_identifier(thread_key, field_name="thread_key") or "",
            user_id=(
                normalize_identifier(user_id, field_name="user_id")
                if user_id is not None
                else None
            ),
            conversation_id=(
                normalize_identifier(conversation_id, field_name="conversation_id")
                if conversation_id is not None
                else None
            ),
            session_id=normalize_identifier(session_id, field_name="session_id") or "",
            active_entities=active_entities,
            last_resolved_entities=last_resolved,
            previous_entity_count=previous_count,
            previous_entity_mode=_bounded_optional(
                payload.get("previous_entity_mode"),
                field_name="previous_entity_mode",
            ),
            last_provider=_bounded_optional(
                payload.get("last_provider"),
                field_name="last_provider",
            ),
            last_providers=tuple(
                _bounded_optional(item, field_name="last_provider") or ""
                for item in providers
            ),
            previous_intent=_bounded_optional(
                payload.get("previous_intent"),
                field_name="previous_intent",
            ),
            previous_scope=_bounded_optional(
                payload.get("previous_scope"),
                field_name="previous_scope",
            ),
            previous_direction=_bounded_optional(
                payload.get("previous_direction"),
                field_name="previous_direction",
            ),
            previous_depth=previous_depth,
            previous_requires_detection=_boolean(
                payload.get("previous_requires_detection", False),
                field_name="previous_requires_detection",
            ),
            previous_requires_asset_profile=_boolean(
                payload.get("previous_requires_asset_profile", False),
                field_name="previous_requires_asset_profile",
            ),
            working_memory=working_memory,
            recent_turn_references=turn_references,
            recent_episodes=episodes,
            entity_timeline=timeline,
            summary_updated_at=str(payload.get("summary_updated_at") or "")[:64],
            summary_source_request_id=str(payload.get("summary_source_request_id") or "")[:128],
            summary_size_tokens=summary_size,
            updated_at=str(updated_at),
            revision=revision,
            schema_version=schema_version,
        )


# Read-only compatibility name for callers that predate the richer Gate 5 contract.
CompactThreadState = ThreadMemoryState
