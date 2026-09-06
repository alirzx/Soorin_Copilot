"""Tiny in-memory routing state for deterministic follow-up resolution."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress

from src.core.memory.episodes import EntityVisit


def _valid_ipv4(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if ip.version != 4:
        return None
    return str(ip)


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
    entity_timeline: tuple[EntityVisit, ...] = ()

    def __post_init__(self) -> None:
        active_entities = tuple(
            dict.fromkeys(
                ip
                for raw in self.active_entities
                if (ip := _valid_ipv4(raw))
            )
        )[:2]
        active_ip = _valid_ipv4(self.active_ip)
        if active_ip and not active_entities:
            active_entities = (active_ip,)
        elif not active_ip and len(active_entities) == 1:
            active_ip = active_entities[0]
        object.__setattr__(self, "active_ip", active_ip)
        object.__setattr__(self, "active_entities", active_entities)
        timeline = tuple(
            item for item in self.entity_timeline
            if isinstance(item, EntityVisit) and item.ordered_entity_ids
        )[-24:]
        object.__setattr__(self, "entity_timeline", timeline)

    @property
    def active_entity_count(self) -> int:
        return len(self.active_entities)


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
