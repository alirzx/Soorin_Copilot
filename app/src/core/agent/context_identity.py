"""Stable identities for independently reviewable model-context results."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable


COMMUTATIVE_GRAPH_CAPABILITIES = {"graph.compare_assets"}
ACTION_FINGERPRINT_SCHEMA = "agent-action-fingerprint-v1"
_OPERATIONAL_ACTION_ARGUMENTS = frozenset({
    "request_id", "session_id", "trace_id", "step_id", "plan_id", "timeout",
    "timeout_seconds",
})


def context_result_identity(
    capability: str,
    entities: Iterable[str],
    *,
    scope: str = "",
    direction: str = "",
    depth: int | None = None,
    relationship_mode: str = "",
    purpose: str = "",
    query_hash: str = "",
) -> str:
    values = tuple(str(value) for value in entities if value)
    if capability in COMMUTATIVE_GRAPH_CAPABILITIES:
        values = tuple(sorted(values))
    entity_part = "|".join(values)
    parts = [capability]
    if entity_part:
        parts.append(entity_part)
    if capability == "graph.get_neighbors" and scope:
        parts.append(scope)
    if capability.startswith("graph.") and capability not in {
        "graph.search_assets", "graph.aggregate_assets"
    }:
        if direction:
            parts.append(f"direction={direction}")
        if depth is not None:
            parts.append(f"depth={int(depth)}")
        if relationship_mode:
            parts.append(f"relationship={relationship_mode}")
    if capability in {"graph.search_assets", "graph.aggregate_assets"} and query_hash:
        parts.append(query_hash)
    if capability == "knowledge.search":
        if query_hash:
            parts.append(query_hash)
        if purpose:
            parts.append(purpose)
    elif capability.startswith("asset.") and purpose:
        parts.append(purpose)
    return ":".join(parts)


def identity_for_tool_result(result: Any) -> str:
    payload = result.raw_payload if isinstance(result.raw_payload, dict) else {}
    return context_result_identity(
        result.source_capability,
        result.entities,
        scope=str(payload.get("requested_scope") or payload.get("scope") or ""),
        purpose=result.purpose,
        query_hash=result.normalized_query_hash,
    )


def canonical_action_fingerprint(step: Any) -> str:
    """Hash validated execution semantics, excluding operational identifiers."""
    capability = str(step.capability)
    arguments = {
        str(key): value
        for key, value in dict(step.arguments).items()
        if str(key) not in _OPERATIONAL_ACTION_ARGUMENTS
    }
    entities = tuple(str(item) for item in tuple(arguments.get("entities") or ()))
    if capability in COMMUTATIVE_GRAPH_CAPABILITIES:
        arguments["entities"] = sorted(entities)
    elif entities:
        arguments["entities"] = list(entities)
    if "views" in arguments:
        arguments["views"] = sorted(dict.fromkeys(str(item) for item in arguments["views"]))
    canonical = json.dumps(
        {
            "schema": ACTION_FINGERPRINT_SCHEMA,
            "capability": capability,
            "arguments": arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{ACTION_FINGERPRINT_SCHEMA}:" + hashlib.sha256(canonical).hexdigest()


def normalize_knowledge_query(query: str) -> str:
    """Return the shared deterministic Planner/Investigator query form."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", query.casefold())).strip()


def knowledge_query_term_hashes(query: str) -> tuple[str, ...]:
    """Retain similarity semantics without storing raw query terms in the ledger."""
    normalized = normalize_knowledge_query(query)
    return tuple(sorted({
        hashlib.sha256(term.encode("utf-8")).hexdigest()[:16]
        for term in normalized.split()
        if term
    }))


def knowledge_query_hash(query: str) -> str:
    normalized = normalize_knowledge_query(query)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def knowledge_actions_equivalent(
    first_purpose: str,
    first_term_hashes: Iterable[str],
    second_purpose: str,
    second_term_hashes: Iterable[str],
) -> bool:
    """Reuse the conservative Planner duplicate rule across adaptive turns."""
    first = set(first_term_hashes)
    second = set(second_term_hashes)
    similarity = len(first & second) / len(first | second) if first and second else 0.0
    return first_purpose.strip().casefold() == second_purpose.strip().casefold() or similarity >= 0.85
