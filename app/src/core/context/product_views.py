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
MIN_PRODUCT_VIEW_TOKENS = 128

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
    if int(max_context_tokens) < MIN_PRODUCT_VIEW_TOKENS:
        raise ValueError(f"Product evidence budget must be at least {MIN_PRODUCT_VIEW_TOKENS} tokens.")
    inventory = payload_inventory(payload)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    included_paths = tuple(
        str(item["path"])
        for item in inventory.paths
        if item.get("type") not in {"object", "array"}
    )
    return ProductEvidenceView(
        provider=provider,
        selected_views=selected,
        detail=detail,
        purpose=purpose,
        payload=payload,
        included_paths=included_paths,
        omitted_path_count=0,
        token_estimate=approx_tokens(serialized),
        truncated=False,
        inventory=inventory,
        source_payload_complete=True,
        projection_usable=True,
        usable_fact_count=inventory.scalar_count,
        projection_omitted_count=0,
    )


def normalize_purpose(value: str, default: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.casefold().strip()).strip("_")
    return (normalized or default)[:64]
