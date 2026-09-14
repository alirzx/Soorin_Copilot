"""Deterministic presentation for non-analytical structured Asset-set results."""

from __future__ import annotations

import json
import re
from typing import Any

from src.core.agent.contracts import (
    StructuredAssetAggregateEvidence,
    StructuredAssetSearchEvidence,
    ToolResult,
)


# Be deliberately conservative here.
#
# This renderer is an optimization only. It must never become a second semantic
# router. If a request appears analytical, investigative, security-oriented,
# explanatory, comparative, or otherwise ambiguous, keep the normal Synth path.
_ANALYTICAL_REQUEST = re.compile(
    r"\b(?:"
    r"explain|compare|comparison|investigate|investigation|"
    r"analy[sz]e|analysis|assess|assessment|"
    r"anomal(?:y|ies|ous)|"
    r"risk|risky|security|secure|"
    r"suspicious|suspicion|compromised|compromise|"
    r"malicious|malware|threat|attack|attacker|"
    r"vulnerable|vulnerability|exposure|exposed|"
    r"unusual|abnormal|concerning|concern|"
    r"why|reason|cause|root\s+cause|"
    r"recommend|recommendation|remediat(?:e|ion)|"
    r"comprehensive|deep(?:ly)?|"
    r"hypothesis|interpret|interpretation|"
    r"behavior|behaviour|"
    r"relationship|communication|topology|"
    r"current\s+state|posture"
    r")\b",
    re.IGNORECASE,
)


# Only positively identify requests that look like deterministic presentation
# operations. If neither pattern is conclusive, fall back to Synth.
_PRESENTATION_REQUEST = re.compile(
    r"\b(?:"
    r"list|show|display|return|give|find|"
    r"count|how\s+many|"
    r"group|grouped|break\s+down|breakdown|"
    r"sort|sorted|order|ordered|"
    r"which|"
    r"top\s+\d+|bottom\s+\d+"
    r")\b",
    re.IGNORECASE,
)


# Canonical presentation order for fields that may be exposed by the
# structured Asset-row contract.
#
# Keep this list synchronized with the structured graph result contract.
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
    """Return True only for clearly presentation-only structured results.

    This function is intentionally fail-closed toward the normal Synth path.
    A false negative merely costs a Synth call; a false positive can hide
    analytical evidence or produce a semantically incomplete user answer.
    """

    text = str(request or "").strip()

    if not text:
        return False

    if _ANALYTICAL_REQUEST.search(text):
        return False

    if not _PRESENTATION_REQUEST.search(text):
        return False

    structured = tuple(
        item
        for item in results
        if item.structured_asset_set is not None
    )

    # Deterministic rendering currently represents exactly one structured
    # Asset-set result. Multiple structured results require synthesis so their
    # relationship can be explained safely.
    if len(structured) != 1:
        return False

    # Do not hide any additional operational/tool evidence. If another tool
    # result exists, let Synth decide how the evidence should be combined.
    if len(results) != 1:
        return False

    # Deepening implies investigation/focal analysis even if the wording itself
    # happened not to match one of the analytical keywords above.
    if any(
        str(item.step_id or "").startswith("deepening-")
        for item in results
    ):
        return False

    return True


def render_structured_presentation(
    results: tuple[ToolResult, ...],
) -> str | None:
    """Render one validated structured result without model interpretation."""

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
        return (
            "No assets matched the requested criteria in the current "
            "organizational asset data."
        )

    columns = [
        key
        for key in _SEARCH_COLUMN_ORDER
        if key == "ip"
        or any(row.get(key) is not None for row in evidence.rows)
    ]

    # StructuredAssetRow should always expose IP, but keep the renderer safe
    # against an empty bounded row set when matched_total is still non-zero.
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
        # This should not normally happen because IP is part of the structured
        # result contract, but deterministic rendering must not crash if the
        # evidence is malformed upstream.
        return (
            f"I found {evidence.matched_total} matching "
            f"asset{'s' if evidence.matched_total != 1 else ''}."
        )

    header = "| " + " | ".join(_label(item) for item in columns) + " |"
    separator = "| " + " | ".join("---" for _item in columns) + " |"

    body = [
        "| "
        + " | ".join(
            _display(row.get(column))
            for column in columns
        )
        + " |"
        for row in evidence.rows
    ]

    lead = (
        f"I found {evidence.matched_total} matching "
        f"asset{'s' if evidence.matched_total != 1 else ''}."
    )

    text = "\n\n".join(
        (
            lead,
            "\n".join((header, separator, *body)),
        )
    )

    if evidence.truncated:
        omitted = max(
            0,
            int(evidence.matched_total) - int(evidence.returned_count),
        )
        text += (
            f"\n\nShowing {evidence.returned_count} returned assets; "
            f"{omitted} matching identities were not returned."
        )

    return text


def _render_aggregate(
    evidence: StructuredAssetAggregateEvidence,
) -> str:
    if evidence.operation == "count":
        return (
            "The current organizational asset data contains "
            f"{evidence.count} matching assets."
        )

    group_fields = tuple(evidence.group_by_fields or ())

    if not group_fields and evidence.group_by is not None:
        group_fields = (evidence.group_by,)

    # Backward/failure-safe fallback. A valid group_count result should expose
    # at least one grouping field, but use the legacy value field if necessary
    # instead of producing an invalid table.
    legacy_group_column = not group_fields

    headers = (
        ("group",)
        if legacy_group_column
        else group_fields
    ) + (
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
        member_truncated = bool(
            group.get("member_ips_truncated", False)
        )
        any_member_truncation = (
            any_member_truncation or member_truncated
        )

        if legacy_group_column:
            values: list[Any] = [group.get("value")]
        else:
            values = [
                group_values.get(field)
                for field in group_fields
            ]

        percentage = group.get("percentage_of_total")

        values.extend(
            (
                group.get("count", 0),
                (
                    f"{float(percentage):.2f}%"
                    if percentage is not None
                    else "—"
                ),
                ", ".join(str(item) for item in member_ips) or "—",
            )
        )

        rows.append(
            "| "
            + " | ".join(_display(item) for item in values)
            + " |"
        )

    lead = f"The exact filtered total is {evidence.count} assets."

    if rows:
        text = "\n\n".join(
            (
                lead,
                "\n".join((header, separator, *rows)),
            )
        )
    else:
        text = lead

    notes: list[str] = []

    if evidence.truncated:
        notes.append(
            "Only a bounded subset of groups is shown; "
            "the total count remains exact."
        )

    if any_member_truncation:
        notes.append(
            "Member IPs are bounded returned identities; "
            "each group count remains exact even where more identities exist."
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

    elif isinstance(value, (list, tuple, set, frozenset)):
        text = ", ".join(
            _stringify(item)
            for item in value
        ) or "—"

    elif isinstance(value, dict):
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    else:
        text = str(value)

    # Keep Markdown tables structurally valid.
    return text.replace("\n", "<br>").replace("|", "\\|")


def _stringify(value: Any) -> str:
    if value is None:
        return "—"

    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")

    if isinstance(value, dict):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    return str(value)