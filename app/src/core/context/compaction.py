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
