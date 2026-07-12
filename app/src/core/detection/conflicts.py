"""Deterministic conflict checks for asset-detection evidence."""

from __future__ import annotations

from typing import Any

from src.core.detection.models import DetectionConflict


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def detect_conflicts(
    *,
    primary_role: Any,
    inferred_device_type: Any,
    extended: dict[str, Any],
    normalized: dict[str, Any],
) -> list[DetectionConflict]:
    conflicts: list[DetectionConflict] = []

    if "domain joined workstation" in _text(primary_role) and "network device" in _text(inferred_device_type):
        conflicts.append(
            DetectionConflict(
                code="role_device_type_conflict",
                severity="medium",
                primary_value=primary_role,
                conflicting_value=inferred_device_type,
                explanation="Primary role indicates a workstation, but inferred device type indicates a network device.",
            )
        )

    if extended.get("dhcp_is_printer") is False and _text(extended.get("printer_product")) == "printer":
        conflicts.append(
            DetectionConflict(
                code="printer_hint_conflict",
                severity="medium",
                primary_value=False,
                conflicting_value=extended.get("printer_product"),
                explanation="DHCP printer flag is false, but printer product evidence is present.",
            )
        )

    precise_ratio = extended.get("outbound_ratio_pct")
    normalized_ratio = normalized.get("outbound_ratio_pct")
    if isinstance(precise_ratio, (int, float)) and isinstance(normalized_ratio, (int, float)):
        if abs(float(precise_ratio) - float(normalized_ratio)) >= 0.4:
            conflicts.append(
                DetectionConflict(
                    code="rounded_metric_mismatch",
                    severity="low",
                    primary_value=precise_ratio,
                    conflicting_value=normalized_ratio,
                    explanation="Precise outbound ratio differs from the normalized rounded value.",
                )
            )

    return conflicts
