"""Deterministic fact deduplication and conservative evidence deltas."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


_TIME_KEY = re.compile(r"(?:time|date|at|timestamp)$", re.IGNORECASE)
_DEDUP_KEYS = frozenset({
    "ip", "ipaddress", "hostname", "name", "mac", "macaddress", "vendor",
    "product", "role", "assetrole", "detectionrole", "tag", "subtag", "status",
})


@dataclass(frozen=True)
class DeduplicationResult:
    payloads: tuple[Any, ...]
    canonical_facts: tuple[dict[str, Any], ...]
    collapsed_count: int


@dataclass(frozen=True)
class DeltaContext:
    created: bool
    reason: str
    payload: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True)
class CurrentEvidenceProjection:
    owner_id: str
    entity: str
    capability: str
    view: str
    schema_version: str
    payload: dict[str, Any]
    retrieved_at: str
    complete: bool = True

    @property
    def identity(self) -> str:
        return f"{self.entity}:{self.capability}:{self.view}"


@dataclass(frozen=True)
class HistoricalBaselineProjection:
    owner_id: str
    entity: str
    capability: str
    view: str
    schema_version: str
    payload: dict[str, Any]
    memory_id: str
    provenance: str
    observed_at: str
    status: str
    freshness: str
    authoritative: bool
    memory_type: str = "validated_finding"
    evidence_classes: tuple[str, ...] = ()
    unresolved_conflict: bool = False
    accessible: bool = True
    complete: bool = True

    @property
    def identity(self) -> str:
        return f"{self.entity}:{self.capability}:{self.view}"


def current_evidence_projections(
    results: tuple[Any, ...],
    *,
    owner_id: str,
) -> tuple[CurrentEvidenceProjection, ...]:
    """Project complete current Product views without importing agent contracts."""
    projections: list[CurrentEvidenceProjection] = []
    for result in results:
        if (
            getattr(result, "source_capability", "") not in {"asset.get_profile", "asset.get_detection"}
            or getattr(result, "status", "") != "ok"
            or getattr(result, "completeness", "") != "complete"
            or bool(getattr(result, "truncated", False))
            or bool(getattr(result, "projection_truncated", False))
            or len(getattr(result, "entities", ())) != 1
        ):
            continue
        evidence = getattr(result, "view_payload", None)
        views = evidence.get("views") if isinstance(evidence, dict) else None
        schema_version = str(getattr(result, "projection_schema_version", "") or "")
        if not isinstance(views, dict) or not schema_version:
            continue
        for view in tuple(getattr(result, "selected_views", ()) or ()):
            payload = views.get(view)
            if isinstance(payload, dict):
                projections.append(CurrentEvidenceProjection(
                    owner_id=owner_id,
                    entity=result.entities[0],
                    capability=result.source_capability,
                    view=str(view),
                    schema_version=schema_version,
                    payload=payload,
                    retrieved_at=str(getattr(result, "valid_at", None) or result.retrieved_at),
                ))
    return tuple(projections)


def historical_baseline_projections(
    memories: tuple[Any, ...],
) -> tuple[HistoricalBaselineProjection, ...]:
    """Parse only bounded structured Product-view memories; malformed prose fails closed."""
    projections: list[HistoricalBaselineProjection] = []
    for retrieved in memories:
        memory = getattr(retrieved, "memory", None)
        if memory is None:
            continue
        try:
            statement = json.loads(memory.statement)
        except (TypeError, ValueError):
            continue
        if not isinstance(statement, dict):
            continue
        capability = str(statement.get("source_capability") or "")
        entities = tuple(statement.get("entities") or ())
        evidence = statement.get("evidence")
        views = evidence.get("views") if isinstance(evidence, dict) else None
        schema_version = str(statement.get("schema_version") or "")
        selected_views = tuple(statement.get("selected_views") or ())
        if (
            capability not in {"asset.get_profile", "asset.get_detection"}
            or len(entities) != 1
            or not isinstance(views, dict)
            or not schema_version
        ):
            continue
        for view in selected_views:
            payload = views.get(view)
            if not isinstance(payload, dict):
                continue
            projections.append(HistoricalBaselineProjection(
                owner_id=memory.user_id,
                entity=str(entities[0]),
                capability=capability,
                view=str(view),
                schema_version=schema_version,
                payload=payload,
                memory_id=memory.memory_id,
                provenance=memory.provenance_category,
                observed_at=memory.valid_from,
                status=memory.status,
                freshness=str(getattr(retrieved, "freshness", "inactive")),
                authoritative=bool(memory.authoritative),
                memory_type=memory.memory_type,
                evidence_classes=tuple(statement.get("evidence_classes") or ()),
                unresolved_conflict=bool(memory.has_unresolved_conflict),
                complete=(
                    statement.get("completeness") == "complete"
                    and bool(statement.get("projection_complete"))
                ),
            ))
    return tuple(projections)


def fingerprint(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def deduplicate_payloads(items: list[tuple[str, Any]]) -> DeduplicationResult:
    """Collapse only exact same-key/value current facts and retain source support."""
    seen: dict[tuple[str, str], int] = {}
    facts: list[dict[str, Any]] = []
    collapsed = 0

    def visit(value: Any, source: str, path: tuple[str, ...]) -> Any:
        nonlocal collapsed
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key in sorted(value, key=str):
                child = visit(value[key], source, (*path, str(key)))
                if child is not _OMITTED:
                    result[str(key)] = child
            return result
        if isinstance(value, list):
            output = [visit(item, source, (*path, "[]")) for item in value]
            return [item for item in output if item is not _OMITTED]
        semantic_key = re.sub(r"[^a-z0-9]", "", path[-1].casefold()) if path else "value"
        if _TIME_KEY.search(semantic_key) or semantic_key not in _DEDUP_KEYS:
            return value
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        signature = (semantic_key, encoded)
        if signature in seen:
            facts[seen[signature]]["support"].append({"source": source, "path": ".".join(path)})
            collapsed += 1
            return _OMITTED
        seen[signature] = len(facts)
        facts.append({
            "fact": semantic_key,
            "value": value,
            "support": [{"source": source, "path": ".".join(path)}],
        })
        return value

    payloads = tuple(visit(payload, source, ()) for source, payload in items)
    return DeduplicationResult(payloads, tuple(facts), collapsed)


def build_delta_context(
    current: Any,
    *,
    baseline: Any | None,
    current_identity: str,
    baseline_identity: str = "",
    schema_version: str = "product-view-v1",
    baseline_schema_version: str = "",
    baseline_accessible: bool = False,
    current_complete: bool = True,
) -> DeltaContext:
    current_fingerprint = fingerprint(current)
    if baseline is None:
        return DeltaContext(False, "baseline_absent", current, current_fingerprint)
    if not baseline_accessible:
        return DeltaContext(False, "baseline_not_in_context", current, current_fingerprint)
    if current_identity != baseline_identity:
        return DeltaContext(False, "baseline_identity_mismatch", current, current_fingerprint)
    if schema_version != baseline_schema_version:
        return DeltaContext(False, "baseline_schema_mismatch", current, current_fingerprint)
    if not current_complete:
        return DeltaContext(False, "current_incomplete", current, current_fingerprint)
    if not isinstance(current, dict) or not isinstance(baseline, dict):
        return DeltaContext(False, "non_mapping_payload", current, current_fingerprint)
    changed = {key: value for key, value in current.items() if key in baseline and baseline[key] != value}
    added = {key: value for key, value in current.items() if key not in baseline}
    removed = {key: baseline[key] for key in baseline if key not in current}
    unchanged = {key: value for key, value in current.items() if key in baseline and baseline[key] == value}
    payload = {
        "baseline_fingerprint": fingerprint(baseline),
        "current_fingerprint": current_fingerprint,
        "changed": changed,
        "new": added,
        "removed": removed,
        "unchanged_important": dict(list(unchanged.items())[:8]),
    }
    return DeltaContext(True, "compatible_accessible_baseline", payload, current_fingerprint)


class _Omitted:
    pass


_OMITTED = _Omitted()
