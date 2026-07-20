"""Tiny in-memory routing state for deterministic follow-up resolution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionRoutingState:
    active_ip: str | None = None
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
    last_plan_id: str | None = None
    last_review_outcome: str | None = None
    last_evidence_ids: tuple[str, ...] = ()
    last_capability_statuses: tuple[str, ...] = ()


class SessionRoutingStateStore:
    """Session-scoped routing metadata, separate from chat transcript memory."""

    def __init__(self) -> None:
        self._state: dict[str, SessionRoutingState] = {}

    def get(self, session_id: str) -> SessionRoutingState:
        return self._state.get(session_id, SessionRoutingState())

    def set(self, session_id: str, state: SessionRoutingState) -> None:
        self._state[session_id] = state

    def clear(self, session_id: str) -> None:
        self._state.pop(session_id, None)
