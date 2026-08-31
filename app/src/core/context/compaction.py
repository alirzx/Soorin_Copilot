"""Deterministic fact deduplication and conservative evidence deltas."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.core.memory.baselines import (
    MAX_BASELINE_PROJECTIONS,
    MAX_BASELINE_PROJECTION_BYTES,
    MAX_BASELINE_TOTAL_BYTES,
    MAX_GRAPH_BASELINE_PEERS,
    BaselineProjection,
    InvestigationBaseline,
)


logger = logging.getLogger(__name__)


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a normalized provider field from either an object or a mapping."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)

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
    entity_ids: tuple[str, ...] = ()
    scope: str = "none"
    direction: str = "none"
    depth: int = 0

    @property
    def identity(self) -> str:
        entities = self.entity_ids or (self.entity,)
        return ":".join((
            "|".join(sorted(entities)), self.capability, self.view, self.scope, self.direction, str(self.depth)
        ))


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

    entity_ids: tuple[str, ...] = ()
    scope: str = "none"
    direction: str = "none"
    depth: int = 0
    @property
    def identity(self) -> str:
        entities = self.entity_ids or (self.entity,)
        return ":".join((
            "|".join(sorted(entities)), self.capability, self.view, self.scope, self.direction, str(self.depth)
        ))


def _graph_projection_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable bounded graph counts, peers, and path/relationship facts."""
    count_keys = (
        "inbound_total", "outbound_total", "bidirectional_total", "total_peer_count",
        "candidate_node_count", "retrieved_node_count", "candidate_edge_count",
        "retrieved_edge_count", "shared_peer_total", "entity_a_unique_peer_total",
        "entity_b_unique_peer_total",
    )
    payload: dict[str, Any] = {
        "counts": {
            key: context[key]
            for key in count_keys
            if isinstance(context.get(key), (int, float))
        }
    }
    target = str(context.get("target_ip") or "")
    peers: list[dict[str, Any]] = []
    for item in context.get("nodes", ()) if isinstance(context.get("nodes"), list) else ():
        if not isinstance(item, dict):
            continue
        peer_id = str(item.get("id") or item.get("ip") or "")
        if not peer_id or peer_id == target:
            continue
        direction = (
            "bidirectional" if item.get("inbound") and item.get("outbound")
            else "inbound" if item.get("inbound")
            else "outbound" if item.get("outbound")
            else str(item.get("direction") or "unknown")
        )
        peers.append({
            "id": peer_id,
            "direction": direction,
            "hop": int(item.get("hop") or 1),
        })
    if peers:
        payload["peers"] = sorted(
            peers, key=lambda item: (item["id"], item["direction"], item["hop"])
        )[:MAX_GRAPH_BASELINE_PEERS]
    for key in ("path_exists", "hop_count", "direct_relationship", "relationship_exists"):
        if key in context:
            payload[key] = context[key]
    if isinstance(context.get("path_nodes"), list):
        payload["path_nodes"] = [str(item) for item in context["path_nodes"][:12]]
    return payload


def current_evidence_projections(
    results: tuple[Any, ...],
    *,
    owner_id: str,
) -> tuple[CurrentEvidenceProjection, ...]:
    """Project bounded current Product views and graph context for deterministic comparison."""
    projections: list[CurrentEvidenceProjection] = []
    for result in results:
        capability = str(getattr(result, "source_capability", "") or "")
        status = str(getattr(result, "status", "") or "")
        entities = tuple(getattr(result, "entities", ()) or ())
        complete = (
            status == "ok"
            and getattr(result, "completeness", "") == "complete"
            and not bool(getattr(result, "truncated", False))
            and not bool(getattr(result, "projection_truncated", False))
            and bool(getattr(result, "source_payload_complete", True))
            and bool(getattr(result, "projection_usable", True))
            and not bool(getattr(result, "contradictions", ()))
            and bool(getattr(result, "context_included", True))
        )
        if capability in {"asset.get_profile", "asset.get_detection"}:
            if status not in {"ok", "partial"} or len(entities) != 1:
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
                        entity=entities[0],
                        entity_ids=entities,
                        capability=capability,
                        view=str(view),
                        schema_version=schema_version,
                        payload=payload,
                        retrieved_at=str(getattr(result, "valid_at", None) or result.retrieved_at),
                        complete=complete,
                    ))
            continue
        if not capability.startswith("graph.") or status not in {"ok", "partial"} or not entities:
            continue
        provider_result = _field(result, "provider_result")
        context = _field(provider_result, "context")
        if not isinstance(context, Mapping):
            continue
        scope = str(context.get("requested_scope") or context.get("scope") or "none")
        direction = str(context.get("direction") or "none")
        depth = int(context.get("depth") or 0)
        graph_complete = bool(
            complete
            and context.get("complete_for_user_request", context.get("requested_scope_complete", True))
            and not context.get("retrieval_truncated", False)
        )
        projections.append(CurrentEvidenceProjection(
            owner_id=owner_id,
            entity=entities[0],
            entity_ids=entities,
            capability=capability,
            view="graph",
            schema_version="graph-baseline-v1",
            payload=_graph_projection_payload(context),
            retrieved_at=str(getattr(result, "valid_at", None) or result.retrieved_at),
            complete=graph_complete,
            scope=scope,
            direction=direction,
            depth=depth,
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



_EVIDENCE_CLASSES_BY_CAPABILITY = {
    "asset.get_profile": ("asset_identity", "asset_role"),
    "asset.get_detection": ("detection_classification", "detection_evidence"),
    "graph.get_summary": ("graph_topology",),
    "graph.get_neighbors": ("graph_topology",),
    "graph.get_relationship": ("graph_topology",),
    "graph.compare_assets": ("graph_topology",),
    "graph.find_path": ("graph_topology",),
}


def investigation_baseline_from_results(
    results: tuple[Any, ...],
    *,
    owner_id: str,
    source_request_id: str,
    scope: str,
    required_capabilities: tuple[str, ...],
) -> InvestigationBaseline | None:
    """Capture only a complete bounded normalized operational evidence set."""
    current = current_evidence_projections(results, owner_id=owner_id)
    if not current:
        logger.info(
            "event=baseline_rejected request_id=%s reason=no_current_projections",
            source_request_id,
        )
        return None
    incomplete = [item for item in current if not item.complete]
    if incomplete:
        logger.info(
            "event=baseline_rejected request_id=%s reason=incomplete_projections capabilities=%s",
            source_request_id,
            ",".join(item.capability for item in incomplete),
        )
        return None
    complete = current
    required_operational = {
        item for item in required_capabilities
        if item in _EVIDENCE_CLASSES_BY_CAPABILITY
    }
    missing_required = required_operational.difference(item.capability for item in complete)
    if missing_required:
        logger.info(
            "event=baseline_rejected request_id=%s reason=missing_required_capabilities capabilities=%s",
            source_request_id,
            ",".join(missing_required),
        )
        return None
    retained: list[BaselineProjection] = []
    used_bytes = 0
    dropped_oversized: list[str] = []
    dropped_budget: list[str] = []
    dropped_limit: list[str] = []
    for item in complete:
        encoded = json.dumps(
            item.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_BASELINE_PROJECTION_BYTES:
            dropped_oversized.append(item.capability)
            continue
        projected = BaselineProjection(
            capability=item.capability,
            entity_ids=item.entity_ids or (item.entity,),
            view=item.view,
            schema_version=item.schema_version,
            evidence_classes=_EVIDENCE_CLASSES_BY_CAPABILITY.get(item.capability, ()),
            payload=item.payload,
            valid_at=item.retrieved_at,
            completeness="complete",
            fingerprint=fingerprint(item.payload),
            scope=item.scope,
            direction=item.direction,
            depth=item.depth,
            truncated=False,
        )
        projection_bytes = len(json.dumps(
            {
                "capability": projected.capability,
                "entity_ids": projected.entity_ids,
                "view": projected.view,
                "schema_version": projected.schema_version,
                "payload": projected.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"))
        if used_bytes + projection_bytes > MAX_BASELINE_TOTAL_BYTES:
            dropped_budget.append(item.capability)
            continue
        retained.append(projected)
        used_bytes += projection_bytes
        if len(retained) >= MAX_BASELINE_PROJECTIONS:
            dropped_limit.extend(p.capability for p in complete[len(retained):])
            break
    if dropped_oversized:
        logger.info(
            "event=baseline_projection_dropped request_id=%s reason=oversized capabilities=%s",
            source_request_id,
            ",".join(dropped_oversized),
        )
    if dropped_budget:
        logger.info(
            "event=baseline_projection_dropped request_id=%s reason=total_budget_exceeded capabilities=%s",
            source_request_id,
            ",".join(dropped_budget),
        )
    if dropped_limit:
        logger.info(
            "event=baseline_projection_dropped request_id=%s reason=projection_limit_exceeded capabilities=%s",
            source_request_id,
            ",".join(dropped_limit),
        )
    if len(retained) != len(complete) or required_operational.difference(
        item.capability for item in retained
    ):
        missing_final = required_operational.difference(item.capability for item in retained)
        logger.info(
            "event=baseline_rejected request_id=%s reason=required_capabilities_dropped missing=%s retained=%s",
            source_request_id,
            ",".join(missing_final) if missing_final else "none",
            ",".join(item.capability for item in retained),
        )
        return None
    entities = tuple(dict.fromkeys(
        entity for projection in retained for entity in projection.entity_ids
    ))
    captured_at = max((item.valid_at for item in retained), default="")
    logger.info(
        "event=baseline_captured request_id=%s entity_count=%s projection_count=%s",
        source_request_id,
        len(entities),
        len(retained),
    )
    return InvestigationBaseline(
        entity_ids=entities,
        captured_at=captured_at,
        source_request_id=source_request_id,
        scope=scope,
        projections=tuple(retained),
        owner_id=owner_id,
    )


def episodic_baseline_projections(
    baselines: tuple[InvestigationBaseline, ...],
) -> tuple[HistoricalBaselineProjection, ...]:
    """Convert directly selected episode baselines into the existing delta input."""
    output: list[HistoricalBaselineProjection] = []
    for baseline in baselines:
        for projection in baseline.projections:
            output.append(HistoricalBaselineProjection(
                owner_id=baseline.owner_id,
                entity=projection.entity_ids[0] if projection.entity_ids else "",
                entity_ids=projection.entity_ids,
                capability=projection.capability,
                view=projection.view,
                schema_version=projection.schema_version,
                payload=projection.payload,
                memory_id=f"episode:{baseline.source_request_id}",
                provenance="validated_tool_result",
                observed_at=projection.valid_at or baseline.captured_at,
                status="active",
                freshness="historical",
                authoritative=True,
                memory_type="investigation_baseline",
                evidence_classes=projection.evidence_classes,
                unresolved_conflict=False,
                accessible=True,
                complete=projection.completeness == "complete" and not projection.truncated,
                scope=projection.scope,
                direction=projection.direction,
                depth=projection.depth,
            ))
    return tuple(output)

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


def _stable_collection_key(current: list[Any], baseline: list[Any]) -> str | None:
    combined = [*current, *baseline]
    if not combined or not all(isinstance(item, dict) for item in combined):
        return None
    for key in ("id", "ip", "entity", "name", "key"):
        current_values = [str(item.get(key) or "") for item in current]
        baseline_values = [str(item.get(key) or "") for item in baseline]
        if (
            all(current_values)
            and all(baseline_values)
            and len(set(current_values)) == len(current_values)
            and len(set(baseline_values)) == len(baseline_values)
        ):
            return key
    return None


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
    """Recursively compare compatible normalized projections within fixed bounds."""
    current_fingerprint = fingerprint(current)
    if baseline is None:
        return DeltaContext(False, "baseline_absent", current, current_fingerprint)
    if not baseline_accessible:
        return DeltaContext(False, "baseline_not_in_context", current, current_fingerprint)
    if current_identity != baseline_identity:
        return DeltaContext(False, "baseline_identity_mismatch", current, current_fingerprint)
    if schema_version != baseline_schema_version:
        return DeltaContext(False, "baseline_schema_mismatch", current, current_fingerprint)
    if not isinstance(current, dict) or not isinstance(baseline, dict):
        return DeltaContext(False, "non_mapping_payload", current, current_fingerprint)

    states: dict[str, list[dict[str, Any]]] = {
        "changed": [], "unchanged": [], "new": [], "missing": [], "incomparable": [],
    }
    maximum_records = 128

    def add(state: str, path: str, **values: Any) -> None:
        if sum(len(items) for items in states.values()) >= maximum_records:
            return
        states[state].append({"path": path or "$", **values})

    def compare(current_value: Any, baseline_value: Any, path: str, depth: int) -> None:
        if depth > 6:
            add("incomparable", path, reason="maximum_depth")
            return
        if isinstance(current_value, dict) and isinstance(baseline_value, dict):
            for key in sorted(set(current_value) | set(baseline_value), key=str):
                child = f"{path}.{key}" if path else str(key)
                if key not in baseline_value:
                    add("new", child, value=current_value[key])
                elif key not in current_value:
                    if current_complete:
                        add("missing", child, previous=baseline_value[key])
                    else:
                        add("incomparable", child, reason="partial_current_unavailable")
                else:
                    compare(current_value[key], baseline_value[key], child, depth + 1)
            return
        if isinstance(current_value, list) and isinstance(baseline_value, list):
            if all(isinstance(item, (str, int, float, bool)) for item in [*current_value, *baseline_value]):
                if current_value == baseline_value:
                    add("unchanged", path, value=current_value)
                else:
                    add("incomparable", path, reason="unkeyed_order_sensitive_collection")
                return
            key = _stable_collection_key(current_value, baseline_value)
            if key:
                current_map = {str(item[key]): item for item in current_value}
                baseline_map = {str(item[key]): item for item in baseline_value}
                for item_key in sorted(set(current_map) | set(baseline_map)):
                    child = f"{path}[{key}={item_key}]"
                    if item_key not in baseline_map:
                        add("new", child, value=current_map[item_key])
                    elif item_key not in current_map:
                        if current_complete:
                            add("missing", child, previous=baseline_map[item_key])
                        else:
                            add("incomparable", child, reason="partial_current_collection")
                    else:
                        compare(current_map[item_key], baseline_map[item_key], child, depth + 1)
                return
            if current_value == baseline_value:
                add("unchanged", path, value=current_value)
            else:
                add("incomparable", path, reason="unkeyed_order_sensitive_collection")
            return
        if type(current_value) is not type(baseline_value):
            add("incomparable", path, previous=baseline_value, current=current_value, reason="type_changed")
        elif current_value == baseline_value:
            add("unchanged", path, value=current_value)
        else:
            add("changed", path, previous=baseline_value, current=current_value)

    compare(current, baseline, "", 0)
    changed = {key: value for key, value in current.items() if key in baseline and baseline[key] != value}
    added = {key: value for key, value in current.items() if key not in baseline}
    removed = (
        {key: baseline[key] for key in baseline if key not in current}
        if current_complete else {}
    )
    unchanged = {key: value for key, value in current.items() if key in baseline and baseline[key] == value}
    payload = {
        "baseline_fingerprint": fingerprint(baseline),
        "current_fingerprint": current_fingerprint,
        "current_complete": current_complete,
        "changed": changed,
        "new": added,
        "removed": removed,
        "unchanged_important": dict(list(unchanged.items())[:8]),
        "states": states,
    }
    return DeltaContext(
        True,
        "compatible_accessible_baseline" if current_complete else "compatible_partial_current_baseline",
        payload,
        current_fingerprint,
    )


class _Omitted:
    pass


_OMITTED = _Omitted()
