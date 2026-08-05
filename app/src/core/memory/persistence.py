"""Typed contracts for opt-in local chat and compact thread persistence."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.core.identity import RequestIdentity, normalize_identifier
from src.core.memory.routing_state import SessionRoutingState


LOCAL_SCHEMA_VERSION = 1
THREAD_STATE_SCHEMA_VERSION = 1
MAX_THREAD_STATE_BYTES = 16_384
MAX_CHAT_CONTENT_CHARS = 100_000
MAX_CONVERSATION_TITLE_CHARS = 256
MAX_CHAT_READ_LIMIT = 200


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


@dataclass(frozen=True)
class LocalConversation:
    conversation_id: str
    user_id: str
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


@dataclass(frozen=True)
class CompactThreadState:
    """Approved reusable continuity only; never arbitrary workflow state."""

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
    ) -> "CompactThreadState":
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
        )

    def to_payload(self) -> dict[str, Any]:
        return {
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
        }

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
    ) -> "CompactThreadState":
        if schema_version != THREAD_STATE_SCHEMA_VERSION:
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
            updated_at=str(updated_at),
            revision=revision,
            schema_version=schema_version,
        )
