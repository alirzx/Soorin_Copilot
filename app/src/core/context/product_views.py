"""Deterministic, bounded Product payload inventories and evidence views."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from src.core.context.models import approx_tokens


DETECTION_VIEWS = (
    "overview",
    "identity_role",
    "anomaly_risk",
    "behavior",
    "evidence_deep",
)
PROFILE_VIEWS = (
    "overview",
    "identity_role",
    "services_software",
    "security_posture",
    "evidence_deep",
)
DETAIL_LEVELS = ("brief", "standard", "deep")
MAX_PRODUCT_VIEW_TOKENS = 8000

_SENSITIVE_PATH_PARTS = {
    "authorization", "password", "passwd", "secret", "token", "api_key",
    "apikey", "captcha", "cookie", "private_key",
}
_COMMON = {"ip", "id", "host", "hostname", "status", "found", "classification", "confidence"}
_KEYWORDS = {
    "overview": _COMMON | {"risk", "verdict", "count", "summary", "primary", "limitation"},
    "identity_role": _COMMON | {"identity", "role", "device", "type", "domain", "name", "service", "conflict"},
    "anomaly_risk": _COMMON | {"anomaly", "risk", "verdict", "rule", "signal", "conflict", "severity", "score"},
    "behavior": _COMMON | {"network", "auth", "protocol", "temporal", "peer", "activity", "connection", "traffic", "port"},
    "services_software": _COMMON | {"service", "software", "application", "package", "version", "port", "protocol"},
    "security_posture": _COMMON | {"security", "risk", "alert", "vulnerability", "hardening", "authentication", "exposure", "severity"},
    "evidence_deep": set(),
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
    payload: dict[str, Any]
    included_paths: tuple[str, ...]
    omitted_path_count: int
    token_estimate: int
    truncated: bool
    inventory: PayloadInventory


def payload_inventory(payload: Any, *, max_paths: int = 2000) -> PayloadInventory:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    paths: list[dict[str, Any]] = []
    counts = {"object": 0, "array": 0, "scalar": 0}

    def visit(value: Any, path: str) -> None:
        if len(paths) >= max_paths:
            return
        if isinstance(value, dict):
            counts["object"] += 1
            paths.append({"path": path or "$", "type": "object", "length": len(value)})
            for key in sorted(value, key=str):
                visit(value[key], f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            counts["array"] += 1
            paths.append({"path": path or "$", "type": "array", "length": len(value)})
            for index, item in enumerate(value):
                visit(item, f"{path}[]" if path else "[]")
                if index >= 99:
                    break
        else:
            counts["scalar"] += 1
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


def select_product_views(provider: str, request: str, detail: str) -> tuple[str, ...]:
    """Choose deterministic defaults when a plan did not request approved views."""
    text = request.casefold()
    comprehensive = any(word in text for word in ("comprehensive", "whole evidence", "full report", "all evidence"))
    analytical = any(word in text for word in ("analyze", "analyse", "investigate", "report", "assess", "evaluate", "abnormal", "anomal", "suspicious"))
    if comprehensive or detail == "deep":
        return approved_views(provider)
    if provider == "detection":
        if analytical or any(word in text for word in ("risk", "security", "conflict")):
            return ("anomaly_risk", "behavior", "overview")
        if any(word in text for word in ("identity", "role", "classif")):
            return ("identity_role", "overview")
        return ("overview",)
    if analytical:
        return ("identity_role", "security_posture", "overview")
    if any(word in text for word in ("service", "software", "port")):
        return ("services_software", "overview")
    return ("overview",)


def build_product_view(
    payload: Any,
    *,
    provider: str,
    views: Iterable[str],
    detail: str,
    max_context_tokens: int,
    purpose: str,
) -> ProductEvidenceView:
    allowed = approved_views(provider)
    selected = tuple(dict.fromkeys(str(view) for view in views))
    if not selected or len(selected) > 5 or any(view not in allowed for view in selected):
        raise ValueError("Product evidence views must use approved view names.")
    if detail not in DETAIL_LEVELS:
        raise ValueError("Product evidence detail must be brief, standard, or deep.")
    budget = max(1, min(MAX_PRODUCT_VIEW_TOKENS, int(max_context_tokens)))
    inventory = payload_inventory(payload)
    leaves = list(_iter_safe_leaves(payload))
    sections: dict[str, list[dict[str, Any]]] = {view: [] for view in selected}
    included: set[str] = set()
    omitted = 0
    limit_per_array = {"brief": 8, "standard": 30, "deep": 100}[detail]

    for view in selected:
        keywords = _KEYWORDS[view]
        candidates = leaves if view == "evidence_deep" else [
            item for item in leaves if _path_matches(item[0], keywords)
        ]
        for path, value in candidates:
            if path in included:
                continue
            safe_value = value[:limit_per_array] if isinstance(value, list) else value
            candidate = {"path": path, "value": safe_value}
            trial = {
                "provider": provider,
                "selected_views": list(selected),
                "detail": detail,
                "purpose": purpose,
                "sections": {**sections, view: [*sections[view], candidate]},
            }
            if approx_tokens(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) > budget:
                omitted += 1
                continue
            sections[view].append(candidate)
            included.add(path)

    omitted += max(0, len(leaves) - len(included) - omitted)
    projected = {
        "provider": provider,
        "selected_views": list(selected),
        "detail": detail,
        "purpose": purpose,
        "sections": sections,
        "projection": {
            "included_path_count": len(included),
            "omitted_path_count": omitted,
            "source_hash": inventory.source_hash,
            "raw_payload_preserved": True,
        },
    }
    token_estimate = approx_tokens(json.dumps(projected, ensure_ascii=False, separators=(",", ":")))
    return ProductEvidenceView(
        provider=provider,
        selected_views=selected,
        detail=detail,
        purpose=purpose,
        payload=projected,
        included_paths=tuple(sorted(included)),
        omitted_path_count=omitted,
        token_estimate=token_estimate,
        truncated=omitted > 0,
        inventory=inventory,
    )


def normalize_purpose(value: str, default: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.casefold().strip()).strip("_")
    return (normalized or default)[:64]


def _iter_safe_leaves(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key in sorted(value, key=str):
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if any(part in _SENSITIVE_PATH_PARTS for part in re.split(r"[^a-z0-9_]+", child_path.casefold())):
                continue
            yield from _iter_safe_leaves(value[key], child_path)
    elif isinstance(value, list):
        # Preserve bounded arrays as one semantic field rather than inventing JSONPath access.
        yield path or "$", _sanitize_value(value)
    else:
        yield path or "$", value


def _path_matches(path: str, keywords: set[str]) -> bool:
    words = set(re.split(r"[^a-z0-9]+", path.casefold()))
    return bool(words & keywords) or any(keyword in path.casefold() for keyword in keywords if len(keyword) > 4)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_value(item)
            for key, item in value.items()
            if not any(
                part in _SENSITIVE_PATH_PARTS
                for part in re.split(r"[^a-z0-9_]+", str(key).casefold())
            )
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value
