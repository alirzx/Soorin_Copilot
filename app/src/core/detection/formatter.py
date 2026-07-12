"""Compact deterministic rendering for asset-detection evidence."""

from __future__ import annotations

from typing import Any

from src.core.detection.models import AssetDetectionEvidence


SENSITIVE_SIGNAL_KEYS = {"mac", "mac_address", "username", "user", "hostname_raw"}


def _preview(value: Any, limit: int = 80) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _value_line(key: str, value: Any) -> str:
    return f"{key}={_preview(value)}"


def _selected_signal_lines(evidence: AssetDetectionEvidence, *, include_negatives: bool) -> list[str]:
    keys = [
        "vendor",
        "product",
        "primary_role",
        "inferred_device_type",
        "outbound_ratio_pct",
        "external_peer_count",
        "dns_query_count",
        "tls_server_sessions",
        "kerberos_server",
        "is_domain_controller",
        "serves_smb_sessions",
        "dhcp_is_printer",
        "printer_product",
        "os_is_windows",
        "os_is_linux",
    ]
    lines: list[str] = []
    for key in keys:
        if key in SENSITIVE_SIGNAL_KEYS:
            continue
        value = evidence.signals.metrics.get(key)
        if value is None and key not in evidence.signals.metrics:
            continue
        if value is False or value == 0:
            if include_negatives:
                lines.append(_value_line(key, value))
            continue
        lines.append(_value_line(key, value))
    return lines


def _bounded(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n...[truncated]"


def summary(evidence: AssetDetectionEvidence) -> str:
    classification = evidence.classification
    tagging = evidence.tagging
    rules = evidence.matched_rules[:3]
    conflicts = evidence.conflicts[:3]
    lines = [
        "[SOORIN ASSET DETECTION SUMMARY]",
        f"IP: {evidence.ip}",
        f"Asset found: {evidence.found}",
        f"Tag/subtag: {tagging.stored_tag or tagging.tag or 'unknown'} / {tagging.stored_sub_tag or tagging.sub_tag or 'unknown'}",
        f"Vendor/product: {classification.vendor or 'unknown'} / {classification.product or 'unknown'}",
        f"Primary role: {classification.primary_role or 'unknown'}",
        f"Inferred device type: {classification.inferred_device_type or 'unknown'}",
        f"Confidence: {classification.confidence if classification.confidence is not None else 'unknown'}",
        f"Top roles: {', '.join(classification.top_roles[:5]) or 'none'}",
        "Top supporting evidence:",
    ]
    for rule in rules:
        evidence_text = "; ".join(rule.evidence[:3]) or rule.name or rule.code or rule.id or "unnamed rule"
        lines.append(f"- {evidence_text}")
    signal_lines = _selected_signal_lines(evidence, include_negatives=False)[:8]
    if signal_lines:
        lines.append("Key metrics: " + "; ".join(signal_lines))
    if conflicts:
        lines.append("Important conflicts:")
        lines.extend(f"- {item.code}: {item.explanation}" for item in conflicts)
    if evidence.limitations:
        lines.append("Limitations: " + " ".join(evidence.limitations[:2]))
    return _bounded("\n".join(lines), 2200)


def compact_full(evidence: AssetDetectionEvidence) -> str:
    classification = evidence.classification
    tagging = evidence.tagging
    lines = [
        "[SOORIN ASSET DETECTION EVIDENCE]",
        f"Source: {evidence.source}; fetched_at={evidence.fetched_at.isoformat()}",
        f"IP: {evidence.ip}; found={evidence.found}",
        f"Classification: primary_role={classification.primary_role or 'unknown'}, inferred_device_type={classification.inferred_device_type or 'unknown'}, confidence={classification.confidence if classification.confidence is not None else 'unknown'}",
        f"Tagging: stored_tag={tagging.stored_tag or 'unknown'}, stored_sub_tag={tagging.stored_sub_tag or 'unknown'}, tag={tagging.tag or 'unknown'}, sub_tag={tagging.sub_tag or 'unknown'}, confidence={tagging.confidence if tagging.confidence is not None else 'unknown'}",
        f"Vendor/product: {classification.vendor or 'unknown'} / {classification.product or 'unknown'}",
        f"Top roles: {', '.join(classification.top_roles[:8]) or 'none'}",
        "Matched rules:",
    ]
    for rule in evidence.matched_rules[:8]:
        lines.append(
            f"- id={rule.id or ''} code={rule.code or ''} name={rule.name or ''} confidence={rule.confidence if rule.confidence is not None else 'unknown'} evidence={'; '.join(rule.evidence[:5])}"
        )
    signal_lines = _selected_signal_lines(evidence, include_negatives=True)
    if signal_lines:
        lines.append("Selected signals:")
        lines.extend(f"- {line}" for line in signal_lines[:30])
    if evidence.signals.missing_sections:
        lines.append("Missing sections: " + ", ".join(evidence.signals.missing_sections))
    if evidence.signals.null_sections:
        lines.append("Null sections: " + ", ".join(evidence.signals.null_sections))
    if evidence.conflicts:
        lines.append("Conflicts:")
        for conflict in evidence.conflicts[:8]:
            lines.append(
                f"- {conflict.code} severity={conflict.severity} primary={_preview(conflict.primary_value)} conflicting={_preview(conflict.conflicting_value)} explanation={conflict.explanation}"
            )
    if evidence.limitations:
        lines.append("Limitations:")
        lines.extend(f"- {item}" for item in evidence.limitations[:5])
    return _bounded("\n".join(lines), 5200)
