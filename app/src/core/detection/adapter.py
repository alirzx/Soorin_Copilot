"""Deterministic adapter from raw product response to Copilot evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.core.detection.conflicts import detect_conflicts
from src.core.detection.models import (
    AssetDetectionEvidence,
    DetectionClassification,
    DetectionRule,
    DetectionSignals,
    DetectionTagging,
    RawAssetDetectionResponse,
)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _dedupe_strings(values: list[Any] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        text = str(value)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _top_roles(values: list[Any] | None) -> list[str]:
    roles: list[str] = []
    for item in values or []:
        if isinstance(item, dict):
            role = item.get("role") or item.get("name") or item.get("label")
        else:
            role = item
        if role is not None:
            roles.append(str(role))
    return _dedupe_strings(roles)


def _metric_view(extended: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(normalized)
    for key, value in extended.items():
        if isinstance(value, (bool, int, float, str)) or value is None:
            metrics[key] = value
    return metrics


def adapt_asset_detection(
    raw: RawAssetDetectionResponse,
    *,
    fetched_at: datetime | None = None,
    source: str = "product_asset_detection",
) -> AssetDetectionEvidence:
    detection = raw.detection
    tagging = raw.tagging
    signals = raw.signals
    extended = _as_dict(signals.extended if signals else None)
    normalized = _as_dict(signals.normalized if signals else None)

    primary_role = _first_present(
        detection.primary_role if detection else None,
        normalized.get("primary_role"),
        normalized.get("primaryRole"),
    )
    inferred_device_type = _first_present(
        detection.inferred_device_type if detection else None,
        tagging.inferred_device_type if tagging else None,
        normalized.get("inferred_device_type"),
        normalized.get("inferredDeviceType"),
    )
    vendor = _first_present(
        detection.vendor if detection else None,
        tagging.vendor if tagging else None,
        normalized.get("vendor"),
    )
    product = _first_present(
        detection.product if detection else None,
        tagging.product if tagging else None,
        normalized.get("product"),
    )

    classification = DetectionClassification(
        asset_found=raw.asset_found,
        primary_role=primary_role,
        inferred_device_type=inferred_device_type,
        confidence=detection.confidence if detection else None,
        top_roles=_top_roles(detection.top_roles if detection else None),
        vendor=vendor,
        product=product,
    )
    internal_tagging = DetectionTagging(
        stored_tag=raw.stored_tag,
        stored_sub_tag=raw.stored_sub_tag,
        tag=tagging.tag if tagging else None,
        sub_tag=tagging.sub_tag if tagging else None,
        confidence=tagging.confidence if tagging else None,
    )
    rules = [
        DetectionRule(
            id=rule.id,
            code=rule.code,
            name=rule.name,
            confidence=rule.confidence,
            evidence=_dedupe_strings(rule.evidence),
        )
        for rule in raw.matched_rules
    ]
    missing_sections = []
    null_sections = []
    if "detection" not in raw.model_fields_set:
        missing_sections.append("detection")
    elif raw.detection is None:
        null_sections.append("detection")
    if "tagging" not in raw.model_fields_set:
        missing_sections.append("tagging")
    elif raw.tagging is None:
        null_sections.append("tagging")
    if raw.signals is None:
        if "signals" not in raw.model_fields_set:
            missing_sections.append("signals")
        else:
            null_sections.append("signals")
    else:
        if "extended" not in raw.signals.model_fields_set:
            missing_sections.append("signals.extended")
        elif raw.signals.extended is None:
            null_sections.append("signals.extended")
        if "normalized" not in raw.signals.model_fields_set:
            missing_sections.append("signals.normalized")
        elif raw.signals.normalized is None:
            null_sections.append("signals.normalized")
    internal_signals = DetectionSignals(
        extended=extended,
        normalized=normalized,
        metrics=_metric_view(extended, normalized),
        missing_sections=missing_sections,
        null_sections=null_sections,
    )
    conflicts = detect_conflicts(
        primary_role=primary_role,
        inferred_device_type=inferred_device_type,
        extended=extended,
        normalized=normalized,
    )
    limitations = [
        "Asset detection is a product-derived point-in-time inference.",
        "Missing fields mean the backend did not return that evidence section.",
        "Null values mean the backend returned an unknown or unavailable value.",
    ]
    return AssetDetectionEvidence(
        ip=raw.ip,
        found=raw.asset_found,
        classification=classification,
        tagging=internal_tagging,
        matched_rules=rules,
        signals=internal_signals,
        conflicts=conflicts,
        limitations=limitations,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        source=source,
    )
