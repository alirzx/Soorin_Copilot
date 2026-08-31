"""Deterministic Product evidence projections for model-facing context."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from src.core.context.models import approx_tokens


DETECTION_VIEWS = ("overview", "evidence", "similarity", "cluster", "full")
PROFILE_VIEWS = ("overview", "identity", "security", "network", "activity", "full")
DETAIL_LEVELS = ("brief", "standard", "deep")
MAX_PRODUCT_VIEW_TOKENS = 8000
MIN_PRODUCT_VIEW_TOKENS = 128
DEFAULT_LIST_LIMIT = 12

_LEGACY_VIEW_ALIASES = {
    "detection": {
        "identity_role": "overview",
        "anomaly_risk": "evidence",
        "behavior": "full",
        "evidence_deep": "full",
    },
    "asset_profile": {
        "identity_role": "identity",
        "services_software": "network",
        "security_posture": "security",
        "evidence_deep": "full",
    },
}


@dataclass(frozen=True)
class PayloadInventory:
    top_level_keys: tuple[str, ...]
    paths: tuple[dict[str, Any], ...]
    object_count: int
    array_count: int
    scalar_count: int
    raw_chars: int
    raw_bytes: int
    approx_tokens: int
    source_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "top_level_keys": list(self.top_level_keys),
            "paths": list(self.paths),
            "object_count": self.object_count,
            "array_count": self.array_count,
            "scalar_count": self.scalar_count,
            "raw_chars": self.raw_chars,
            "raw_bytes": self.raw_bytes,
            "approx_tokens": self.approx_tokens,
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True)
class ProductEvidenceView:
    provider: str
    selected_views: tuple[str, ...]
    detail: str
    purpose: str
    payload: Any
    included_paths: tuple[str, ...]
    omitted_path_count: int
    token_estimate: int
    truncated: bool
    inventory: PayloadInventory
    source_payload_complete: bool
    projection_usable: bool
    usable_fact_count: int
    projection_omitted_count: int
    projection_fingerprint: str = ""
    schema_version: str = "product-view-v1"


def payload_inventory(payload: Any, *, max_paths: int = 2000) -> PayloadInventory:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    paths: list[dict[str, Any]] = []
    counts = {"object": 0, "array": 0, "scalar": 0}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            counts["object"] += 1
            if len(paths) < max_paths:
                paths.append({"path": path or "$", "type": "object", "length": len(value)})
            for key in sorted(value, key=str):
                visit(value[key], f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            counts["array"] += 1
            if len(paths) < max_paths:
                paths.append({"path": path or "$", "type": "array", "length": len(value)})
            for item in value:
                visit(item, f"{path}[]" if path else "[]")
        else:
            counts["scalar"] += 1
            if len(paths) < max_paths:
                paths.append({"path": path or "$", "type": type(value).__name__})

    visit(payload, "")
    top_keys = tuple(sorted(str(key) for key in payload)) if isinstance(payload, dict) else ()
    return PayloadInventory(
        top_level_keys=top_keys,
        paths=tuple(paths),
        object_count=counts["object"],
        array_count=counts["array"],
        scalar_count=counts["scalar"],
        raw_chars=len(serialized),
        raw_bytes=len(serialized.encode("utf-8")),
        approx_tokens=approx_tokens(serialized),
        source_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )


def approved_views(provider: str) -> tuple[str, ...]:
    return DETECTION_VIEWS if provider == "detection" else PROFILE_VIEWS


def normalize_product_views(provider: str, views: Iterable[str]) -> tuple[str, ...]:
    aliases = _LEGACY_VIEW_ALIASES.get(provider, {})
    return tuple(dict.fromkeys(aliases.get(str(view), str(view)) for view in views))


def select_product_views(provider: str, request: str, detail: str) -> tuple[str, ...]:
    """Select the minimum deterministic view set for an unviewed plan step."""
    text = request.casefold()
    deep = detail == "deep" or any(word in text for word in ("deep", "exhaustive", "raw", "full payload"))
    if deep:
        return ("full",)
    if provider == "detection":
        similarity = any(word in text for word in ("similar", "nearest", "analogous", "resemble"))
        cluster = any(word in text for word in ("cluster", "cohort", "group", "purity"))
        explanation = any(word in text for word in ("why", "evidence", "rule", "contradiction", "conflict"))
        classification = any(word in text for word in ("classif", "confidence", "type", "role", "identify"))
        if similarity:
            return ("similarity",)
        if cluster:
            return ("cluster",)
        if explanation and classification:
            return ("overview", "evidence")
        if explanation:
            return ("evidence",)
        return ("overview",)
    identity = any(word in text for word in ("identity", "kerberos", "ldap", "ntlm", "smb", "domain controller", "authentication"))
    security = any(word in text for word in ("risk", "security", "alert", "failure", "suspicious"))
    network = any(word in text for word in ("network", "connection", "port", "service", "traffic", "subnet"))
    activity = any(word in text for word in ("activity", "recent", "trend", "session", "logs"))
    selected = tuple(
        view for view, enabled in (
            ("identity", identity),
            ("security", security),
            ("network", network),
            ("activity", activity),
        ) if enabled
    )
    return selected or ("overview",)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _find(payload: Any, aliases: tuple[str, ...]) -> Any:
    wanted = {_key(alias) for alias in aliases}
    queue = [payload]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            for key, value in current.items():
                if _key(str(key)) in wanted:
                    return value
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current[:DEFAULT_LIST_LIMIT])
    return None


def _bounded(value: Any, *, limit: int = DEFAULT_LIST_LIMIT) -> tuple[Any, int]:
    if isinstance(value, list):
        table = _compact_table(value, limit=limit)
        if table is not None:
            return table, max(0, len(value) - min(len(value), limit))
        bounded_items = []
        omitted_nested = 0
        for item in value[:limit]:
            compact, omitted = _bounded(item, limit=limit)
            bounded_items.append(compact)
            omitted_nested += omitted
        omitted = max(0, len(value) - len(bounded_items))
        if omitted or omitted_nested:
            return {
                "total_count": len(value),
                "included_count": len(bounded_items),
                "omitted_count": omitted,
                "selection_rule": "first_stable_items",
                "items": bounded_items,
            }, omitted + omitted_nested
        return bounded_items, 0
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        omitted = 0
        for key in sorted(value, key=str):
            result[str(key)], child_omitted = _bounded(value[key], limit=limit)
            omitted += child_omitted
        return result, omitted
    return value, 0


def _compact_table(value: list[Any], *, limit: int) -> dict[str, Any] | None:
    """Use TSV only for homogeneous flat rows where repeated JSON keys add noise."""
    if len(value) < 3 or not all(isinstance(item, dict) for item in value):
        return None
    columns = tuple(sorted({str(key) for item in value for key in item}))
    if not columns or len(columns) > 10:
        return None
    if any(
        isinstance(item.get(column), (dict, list))
        for item in value
        for column in columns
    ):
        return None

    def clean(item: Any) -> str:
        if item is None:
            return ""
        return str(item).replace("\t", " ").replace("\r", " ").replace("\n", " ")

    included = value[:limit]
    rows = ["\t".join(clean(item.get(column)) for column in columns) for item in included]
    return {
        "format": "tsv",
        "columns": list(columns),
        "rows": rows,
        "total_count": len(value),
        "included_count": len(included),
        "omitted_count": max(0, len(value) - len(included)),
        "selection_rule": "first_stable_items",
    }


def _fields(payload: Any, definitions: dict[str, tuple[str, ...]]) -> tuple[dict[str, Any], int]:
    output: dict[str, Any] = {}
    omitted = 0
    for name, aliases in definitions.items():
        value = _find(payload, aliases)
        if value is not None:
            output[name], bounded_omitted = _bounded(value)
            omitted += bounded_omitted
    return output, omitted


_PROFILE_COMMON = {
    "id": ("id", "assetId"),
    "name": ("name", "hostname", "assetName"),
    "status": ("status",),
    "asset_type": ("asset_type", "assetType", "suggestedType"),
    "os": ("os", "operatingSystem"),
    "owner": ("owner",),
    "ip": ("ip", "ip_address", "ipAddress"),
    "mac": ("mac", "mac_address", "macAddress"),
}


def _profile_projection(payload: Any, view: str) -> tuple[Any, int]:
    if view == "full":
        return payload, 0
    definitions = dict(_PROFILE_COMMON)
    if view == "overview":
        definitions.update({
            "subnet": ("subnet",), "zone": ("zone",), "risk_score": ("risk_score", "riskScore"),
            "risk_level": ("risk_level", "riskLevel"), "risk_trend": ("risk_trend", "riskTrend"),
            "active_connections": ("active_connections", "activeConnectionCount"),
            "active_services": ("active_services", "activeServiceCount"),
            "active_alerts": ("active_alerts", "activeAlertCount"), "tag": ("tag",),
            "sub_tag": ("subTag", "sub_tag"), "detection_role": ("role", "detectionRole"),
            "detection_confidence": ("confidence", "detectionConfidence"),
            "last_detection_at": ("lastDetectionAt", "last_detection_at"),
        })
    elif view == "identity":
        definitions.update({
            "kerberos": ("kerberos",), "ldap": ("ldap",), "ntlm": ("ntlm",), "smb": ("smb",),
            "domain": ("domain",), "identity_confidence": ("identityConfidence",),
        })
    elif view == "security":
        definitions.update({
            "risk": ("risk",), "risk_score": ("risk_score", "riskScore"), "risk_level": ("risk_level", "riskLevel"),
            "risk_trend": ("risk_trend", "riskTrend"), "alerts": ("alerts", "alertsSummary"),
            "authentication_failures": ("authenticationFailures", "authFailures"),
            "classification": ("classification", "detection"), "latest_observation": ("latestObservationAt", "lastSeenAt"),
        })
    elif view == "network":
        definitions.update({
            "subnet": ("subnet",), "gateway": ("gateway",), "dhcp": ("dhcp",),
            "open_ports": ("open_ports", "openPorts"), "services": ("services",),
            "connections": ("connections", "connectionSummary"), "external_connections": ("externalConnectionCount",),
            "traffic": ("traffic", "trafficSummary"),
        })
    elif view == "activity":
        definitions.update({
            "traffic_series": ("trafficSeries", "traffic_series"), "recent_logs": ("recentLogs", "recent_logs"),
            "authentication_activity": ("authenticationActivity", "authentication"),
            "service_activity": ("serviceActivity",), "session_activity": ("sessions", "sessionActivity"),
            "latest_observation": ("latestObservationAt", "lastSeenAt", "updatedAt"),
        })
    return _fields(payload, definitions)


def _detection_projection(payload: Any, view: str) -> tuple[Any, int]:
    if view == "full":
        return payload, 0
    definitions = {
        "overview": {
            "asset_name": ("assetName",), "ip": ("ip", "targetIp"), "asset_found": ("assetFound",), "status": ("status",),
            "suggested_type": ("suggestedType",), "model_confidence": ("modelConfidence", "confidence"),
            "mapping_confidence": ("mappingConfidence",), "unknown_score": ("unknownScore",),
            "classification_summary": ("classificationSummary", "classification"), "vendor": ("vendor",),
            "product": ("product",), "role": ("role",), "roles": ("roles",), "tag": ("tag",),
            "sub_tag": ("subTag",), "last_detection_at": ("lastDetectionAt",),
        },
        "evidence": {
            "top_positive_features": ("topPositiveFeatures",), "missing_features": ("missingFeatures",),
            "rare_features": ("rareFeatures",), "feature_family_summary": ("featureFamilySummary",),
            "rule_votes": ("ruleVotes", "matchedRules"), "contradictions": ("contradictions", "conflicts"),
            "overall_rule_confidence": ("overallRuleConfidence",),
        },
        "similarity": {
            "target_ip": ("targetIp", "ip"), "neighbors": ("neighbors", "similarAssets", "results"),
            "total_candidates": ("totalCandidates", "total"),
        },
        "cluster": {
            "target_ip": ("targetIp", "ip"), "cluster_id": ("clusterId",), "population": ("population",),
            "purity": ("purity",), "algorithm": ("algorithm",), "label_distribution": ("labelDistribution",),
            "centroid_distance": ("centroidDistance",), "members": ("members",),
        },
    }[view]
    projected, omitted = _fields(payload, definitions)
    if view == "similarity":
        projected["semantic_limitation"] = "Rule/tag/role-affinity similarity; not model embedding-space similarity."
    elif view == "cluster":
        projected["semantic_limitation"] = (
            "Rule/tag/role-affinity grouping; not unsupervised ML clustering. Population one is weak cohort evidence."
        )
    return projected, omitted


def build_product_view(
    payload: Any,
    *,
    provider: str,
    views: Iterable[str],
    detail: str,
    max_context_tokens: int,
    purpose: str,
) -> ProductEvidenceView:
    selected = normalize_product_views(provider, views)
    allowed = approved_views(provider)
    if not selected or len(selected) > 6 or any(view not in allowed for view in selected):
        raise ValueError("Product evidence views must use approved view names.")
    if detail not in DETAIL_LEVELS:
        raise ValueError("Product evidence detail must be brief, standard, or deep.")
    if int(max_context_tokens) < MIN_PRODUCT_VIEW_TOKENS:
        raise ValueError(f"Product evidence budget must be at least {MIN_PRODUCT_VIEW_TOKENS} tokens.")
    inventory = payload_inventory(payload)
    projected: dict[str, Any] = {}
    omitted_items = 0
    for view in selected:
        value, omitted = (
            _detection_projection(payload, view)
            if provider == "detection"
            else _profile_projection(payload, view)
        )
        projected[view] = value
        omitted_items += omitted
    model_payload: Any = payload if selected == ("full",) else {
        "provider": provider,
        "views": projected,
        "projection_metadata": {
            "source_payload_complete": True,
            "selected_views": list(selected),
            "normal_compaction": True,
        },
    }
    projected_inventory = payload_inventory(model_payload)
    projected_fact_count = (
        inventory.scalar_count
        if selected == ("full",)
        else sum(payload_inventory(value).scalar_count for value in projected.values())
    )
    serialized = json.dumps(model_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    included_paths = tuple(
        str(item["path"])
        for item in projected_inventory.paths
        if item.get("type") not in {"object", "array"}
    )
    omitted_paths = max(0, inventory.scalar_count - projected_fact_count)
    return ProductEvidenceView(
        provider=provider,
        selected_views=selected,
        detail=detail,
        purpose=purpose,
        payload=model_payload,
        included_paths=included_paths,
        omitted_path_count=omitted_paths,
        token_estimate=approx_tokens(serialized),
        truncated=False,
        inventory=inventory,
        source_payload_complete=True,
        projection_usable=bool(projected_fact_count),
        usable_fact_count=projected_fact_count,
        projection_omitted_count=omitted_paths + omitted_items,
        projection_fingerprint=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )


def normalize_purpose(value: str, default: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.casefold().strip()).strip("_")
    return (normalized or default)[:64]
