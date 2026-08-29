"""Pure bounded structured-baseline contracts shared by memory and context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


MAX_BASELINE_PROJECTIONS = 8
MAX_BASELINE_PROJECTION_BYTES = 6_000
MAX_BASELINE_TOTAL_BYTES = 10_000
MAX_GRAPH_BASELINE_PEERS = 48


@dataclass(frozen=True)
class BaselineProjection:
    """One bounded normalized historical evidence projection."""

    capability: str
    entity_ids: tuple[str, ...]
    view: str
    schema_version: str
    evidence_classes: tuple[str, ...]
    payload: dict[str, Any]
    valid_at: str
    completeness: Literal["complete", "partial"]
    fingerprint: str
    scope: str = "none"
    direction: str = "none"
    depth: int = 0
    truncated: bool = False

    @property
    def identity(self) -> str:
        return ":".join((
            "|".join(sorted(self.entity_ids)),
            self.capability,
            self.view,
            self.scope,
            self.direction,
            str(self.depth),
            self.schema_version,
        ))


@dataclass(frozen=True)
class InvestigationBaseline:
    """Latest complete structured evidence snapshot for one investigation episode."""

    entity_ids: tuple[str, ...]
    captured_at: str
    source_request_id: str
    scope: str
    projections: tuple[BaselineProjection, ...]
    owner_id: str = ""
