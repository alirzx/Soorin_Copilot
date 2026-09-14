"""Deterministic rendering helpers for structured Asset-set evidence.

Rendering remains available for tests and diagnostics, but production final
answers are always owned by the Synth model. Deterministic retrieval continues
to own filtering, grouping, counts, percentages, identities, and tie handling.
"""

from __future__ import annotations

import json
from typing import Any

from src.core.agent.contracts import (
    StructuredAssetAggregateEvidence,
    StructuredAssetSearchEvidence,
    ToolResult,
)


_SEARCH_COLUMN_ORDER = (
    "ip",
    "graph_key",
    "asset_name",
    "status",
    "suggested_type",
    "classification_summary",
    "role",
    "roles",
    "vendor",
    "product",
    "tag",
    "sub_tag",
    "model_confidence",
    "mapping_confidence",
    "unknown_score",
    "last_detection_at",
    "enrichment_status",
    "enrichment_next_due_at",
)


def is_structured_presentation_only(
    request: str,
    results: tuple[ToolResult, ...],
) -> bool:
    """Never bypass Synth for a normal completed request."""

    _ = request, results
    return False


def render_structured_presentation(
    results: tuple[ToolResult, ...],
) -> str | None:
    """Render one validated structured result for diagnostics or fallback use."""

    evidence = next(
        (
            item.structured_asset_set
            for item in results
            if item.structured_asset_set is not None
        ),
        None,
    )
    if isinstance(evidence, StructuredAssetSearchEvidence):
        return _render_search(evidence)
    if isinstance(evidence, StructuredAssetAggregateEvidence):
        return _render_aggregate(evidence)
    return None


def _render_search(evidence: StructuredAssetSearchEvidence) -> str:
    if evidence.matched_total == 0:
        return "No assets matched the requested criteria in the current organizational asset data."

    columns = [
        key
        for key in _SEARCH_COLUMN_ORDER
        if key == "ip" or any(row.get(key) is not None for row in evidence.rows)
    ]
    if not evidence.rows:
        text = (
            f"I found {evidence.matched_total} matching "
            f"asset{'s' if evidence.matched_total != 1 else ''}, "
            "but no asset rows were returned in this bounded result."
        )
        if evidence.truncated:
            text += " The result set is truncated."
        return text
    if not columns:
        return (
            f"I found {evidence.matched_total} matching "
            f"asset{'s' if evidence.matched_total != 1 else ''}."
        )

    header = "| " + " | ".join(_label(item) for item in columns) + " |"
    separator = "| " + " | ".join("---" for _item in columns) + " |"
    body = [
        "| " + " | ".join(_display(row.get(column)) for column in columns) + " |"
        for row in evidence.rows
    ]
    lead = (
        f"I found {evidence.matched_total} matching "
        f"asset{'s' if evidence.matched_total != 1 else ''}."
    )
    text = "\n\n".join((lead, "\n".join((header, separator, *body))))
    if evidence.truncated:
        omitted = max(0, int(evidence.matched_total) - int(evidence.returned_count))
        text += (
            f"\n\nShowing {evidence.returned_count} returned assets; "
            f"{omitted} matching identities were not returned."
        )
    return text


def _render_aggregate(evidence: StructuredAssetAggregateEvidence) -> str:
    if evidence.operation == "count":
        return f"The current organizational asset data contains {evidence.count} matching assets."

    group_fields = tuple(evidence.group_by_fields or ())
    if not group_fields and evidence.group_by is not None:
        group_fields = (evidence.group_by,)
    legacy_group_column = not group_fields
    headers = (("group",) if legacy_group_column else group_fields) + (
        "count",
        "percentage",
        "member_ips",
    )
    header = "| " + " | ".join(_label(item) for item in headers) + " |"
    separator = "| " + " | ".join("---" for _item in headers) + " |"
    rows: list[str] = []
    any_member_truncation = False

    for group in evidence.groups:
        group_values = dict(group.get("group_values") or {})
        if len(group_fields) == 1 and not group_values:
            group_values[group_fields[0]] = group.get("value")
        member_ips = tuple(group.get("member_ips") or ())
        member_truncated = bool(group.get("member_ips_truncated", False))
        any_member_truncation = any_member_truncation or member_truncated
        if legacy_group_column:
            values: list[Any] = [group.get("value")]
        else:
            values = [group_values.get(field) for field in group_fields]
        percentage = group.get("percentage_of_total")
        values.extend(
            (
                group.get("count", 0),
                f"{float(percentage):.2f}%" if percentage is not None else "—",
                ", ".join(str(item) for item in member_ips) or "—",
            )
        )
        rows.append("| " + " | ".join(_display(item) for item in values) + " |")

    lead = f"The exact filtered total is {evidence.count} assets."
    text = "\n\n".join((lead, "\n".join((header, separator, *rows)))) if rows else lead
    notes: list[str] = []
    if evidence.truncated:
        notes.append("Only a bounded subset of groups is shown; the total count remains exact.")
    if any_member_truncation:
        notes.append(
            "Member IPs are bounded returned identities; each group count remains exact even where more identities exist."
        )
    if notes:
        text += "\n\n" + " ".join(notes)
    return text


def _label(value: str) -> str:
    return str(value).replace("_", " ").title()


def _display(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
    elif isinstance(value, (list, tuple)):
        text = ", ".join(_stringify(item) for item in value) or "—"
    elif isinstance(value, (set, frozenset)):
        text = ", ".join(_stringify(item) for item in sorted(value, key=str)) or "—"
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    return text.replace("\n", "<br>").replace("|", "\\|")


def _stringify(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)
