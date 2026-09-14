"""Bounded result-set continuity metadata for structured Asset queries."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from src.core.graph.structured import (
    StructuredQueryMode,
    StructuredQuerySpec,
    structured_query_identity,
)


STRUCTURED_QUERY_CONTEXT_SCHEMA_VERSION = "structured-query-context-v1"
MAX_STRUCTURED_QUERY_RESULT_REFS = 8
MAX_STRUCTURED_QUERY_GROUP_REFS = 8
_QUERY_IDENTITY_RE = re.compile(r"^structured-asset-set:v1:[0-9a-f]{64}$")
_RESULT_FINGERPRINT_RE = re.compile(r"^structured-query-context:v1:[0-9a-f]{64}$")


def _bounded_optional(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("structured context text must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError("structured context text is blank or oversized")
    return normalized


@dataclass(frozen=True)
class StructuredAssetRef:
    """Identity-only reference retained from one ordered search result."""

    ip: str
    graph_key: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        try:
            ip = ipaddress.ip_address(str(self.ip).strip())
        except ValueError as exc:
            raise ValueError("structured Asset ref has an invalid IP") from exc
        if ip.version != 4:
            raise ValueError("structured Asset refs currently require IPv4")
        object.__setattr__(self, "ip", str(ip))
        object.__setattr__(self, "graph_key", _bounded_optional(self.graph_key, maximum=128))
        object.__setattr__(self, "display_name", _bounded_optional(self.display_name, maximum=128))

    def to_payload(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "graph_key": self.graph_key,
            "display_name": self.display_name,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "StructuredAssetRef":
        if not isinstance(payload, dict) or set(payload) - {"ip", "graph_key", "display_name"}:
            raise ValueError("invalid structured Asset ref")
        return cls(
            ip=payload.get("ip"),
            graph_key=payload.get("graph_key"),
            display_name=payload.get("display_name"),
        )


@dataclass(frozen=True)
class StructuredAggregateGroupRef:
    """Bounded label/count continuity for one aggregate group."""

    value: str | None
    count: int
    group_values: tuple[tuple[str, str | None], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _bounded_optional(self.value, maximum=128))
        if not isinstance(self.count, int) or not 0 <= self.count <= 1_000_000_000_000:
            raise ValueError("invalid structured aggregate group count")
        allowed = {
            "status", "suggested_type", "role", "vendor", "product", "tag",
            "sub_tag", "enrichment_status",
        }
        normalized: list[tuple[str, str | None]] = []
        for key, value in self.group_values:
            if key not in allowed or any(existing == key for existing, _ in normalized):
                raise ValueError("invalid structured aggregate group dimension")
            normalized.append((key, _bounded_optional(value, maximum=128)))
        if len(normalized) > 3:
            raise ValueError("too many structured aggregate group dimensions")
        if len(normalized) == 1:
            only = normalized[0][1]
            if self.value is not None and self.value != only:
                raise ValueError("structured aggregate legacy value mismatch")
            if self.value is None:
                object.__setattr__(self, "value", only)
        object.__setattr__(self, "group_values", tuple(normalized))

    def to_payload(self) -> dict[str, Any]:
        payload = {"value": self.value, "count": self.count}
        if self.group_values:
            payload["group_values"] = dict(self.group_values)
        return payload

    @classmethod
    def from_payload(cls, payload: Any) -> "StructuredAggregateGroupRef":
        if not isinstance(payload, dict) or set(payload) - {"value", "count", "group_values"}:
            raise ValueError("invalid structured aggregate group ref")
        group_values = payload.get("group_values") or {}
        if not isinstance(group_values, dict):
            raise ValueError("invalid structured aggregate group values")
        return cls(
            value=payload.get("value"),
            count=payload.get("count"),
            group_values=tuple((str(key), value) for key, value in group_values.items()),
        )


@dataclass(frozen=True)
class StructuredQueryContext:
    """Latest bounded structured-result reference, never operational evidence."""

    mode: Literal["search", "aggregate"]
    query_identity: str
    result_fingerprint: str
    active_graph_version: str | None
    query: StructuredQuerySpec
    matched_total: int | None
    returned_count: int | None
    count: int | None
    retrieval_truncated: bool
    continuity_truncated: bool
    result_refs: tuple[StructuredAssetRef, ...]
    aggregate_groups: tuple[StructuredAggregateGroupRef, ...]
    source_request_id: str
    retrieved_at: str
    created_at: str
    schema_version: str = STRUCTURED_QUERY_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURED_QUERY_CONTEXT_SCHEMA_VERSION:
            raise ValueError("unsupported structured context schema")
        if self.mode not in {"search", "aggregate"}:
            raise ValueError("invalid structured context mode")
        if self.query.mode.value != self.mode:
            raise ValueError("structured context query mode mismatch")
        if not _QUERY_IDENTITY_RE.fullmatch(self.query_identity):
            raise ValueError("invalid structured query identity")
        if not _RESULT_FINGERPRINT_RE.fullmatch(self.result_fingerprint):
            raise ValueError("invalid structured result fingerprint")
        object.__setattr__(
            self,
            "active_graph_version",
            _bounded_optional(self.active_graph_version, maximum=128),
        )
        if self.query_identity != structured_query_identity(
            self.query,
            active_graph_version=self.active_graph_version,
        ):
            raise ValueError("structured query identity mismatch")
        for name in ("matched_total", "returned_count", "count"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or not 0 <= value <= 1_000_000_000_000
            ):
                raise ValueError(f"invalid structured context {name}")
        if not isinstance(self.retrieval_truncated, bool) or not isinstance(self.continuity_truncated, bool):
            raise ValueError("invalid structured context truncation state")
        if len(self.result_refs) > MAX_STRUCTURED_QUERY_RESULT_REFS:
            raise ValueError("too many structured Asset refs")
        if len(self.aggregate_groups) > MAX_STRUCTURED_QUERY_GROUP_REFS:
            raise ValueError("too many structured aggregate group refs")
        if self.mode == "search":
            if self.matched_total is None or self.returned_count is None or self.count is not None:
                raise ValueError("invalid structured search counts")
            if self.aggregate_groups:
                raise ValueError("structured search cannot retain aggregate groups")
        else:
            if self.count is None or self.matched_total is not None or self.returned_count is not None:
                raise ValueError("invalid structured aggregate counts")
            if self.result_refs:
                raise ValueError("structured aggregate cannot retain Asset refs")
        object.__setattr__(self, "source_request_id", _bounded_optional(self.source_request_id, maximum=128) or "")
        object.__setattr__(self, "retrieved_at", _bounded_optional(self.retrieved_at, maximum=64) or "")
        object.__setattr__(self, "created_at", _bounded_optional(self.created_at, maximum=64) or "")
        if self.result_fingerprint != self.compute_fingerprint():
            raise ValueError("structured result fingerprint mismatch")

    @classmethod
    def create(
        cls,
        *,
        query_identity: str,
        active_graph_version: str | None,
        query: StructuredQuerySpec | dict[str, Any],
        matched_total: int | None = None,
        returned_count: int | None = None,
        count: int | None = None,
        retrieval_truncated: bool = False,
        continuity_truncated: bool = False,
        result_refs: tuple[StructuredAssetRef, ...] = (),
        aggregate_groups: tuple[StructuredAggregateGroupRef, ...] = (),
        source_request_id: str,
        retrieved_at: str,
        created_at: str,
    ) -> "StructuredQueryContext":
        normalized_query = StructuredQuerySpec.model_validate(query)
        mode = normalized_query.mode.value
        active_graph_version = _bounded_optional(active_graph_version, maximum=128)
        source_request_id = _bounded_optional(source_request_id, maximum=128) or ""
        retrieved_at = _bounded_optional(retrieved_at, maximum=64) or ""
        created_at = _bounded_optional(created_at, maximum=64) or ""
        original_ref_count = len(result_refs)
        original_group_count = len(aggregate_groups)
        refs = tuple(result_refs[:MAX_STRUCTURED_QUERY_RESULT_REFS])
        groups = tuple(aggregate_groups[:MAX_STRUCTURED_QUERY_GROUP_REFS])
        truncated = bool(
            continuity_truncated
            or original_ref_count > len(refs)
            or original_group_count > len(groups)
        )
        provisional = cls.__new__(cls)
        values = {
            "mode": mode,
            "query_identity": query_identity,
            "result_fingerprint": "",
            "active_graph_version": active_graph_version,
            "query": normalized_query,
            "matched_total": matched_total,
            "returned_count": returned_count,
            "count": count,
            "retrieval_truncated": bool(retrieval_truncated),
            "continuity_truncated": truncated,
            "result_refs": refs,
            "aggregate_groups": groups,
            "source_request_id": source_request_id,
            "retrieved_at": retrieved_at,
            "created_at": created_at,
            "schema_version": STRUCTURED_QUERY_CONTEXT_SCHEMA_VERSION,
        }
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        values["result_fingerprint"] = provisional.compute_fingerprint()
        return cls(**values)

    def _fingerprint_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "query_identity": self.query_identity,
            "active_graph_version": self.active_graph_version,
            "matched_total": self.matched_total,
            "returned_count": self.returned_count,
            "count": self.count,
            "retrieval_truncated": self.retrieval_truncated,
            "continuity_truncated": self.continuity_truncated,
            "result_refs": [item.to_payload() for item in self.result_refs],
            "aggregate_groups": [item.to_payload() for item in self.aggregate_groups],
        }

    def compute_fingerprint(self) -> str:
        canonical = json.dumps(
            self._fingerprint_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "structured-query-context:v1:" + hashlib.sha256(canonical).hexdigest()

    def bounded(self, maximum_items: int) -> "StructuredQueryContext":
        """Return a smaller valid snapshot for thread-state size pressure."""
        maximum_items = max(0, maximum_items)
        refs = self.result_refs[:maximum_items]
        groups = self.aggregate_groups[:maximum_items]
        if refs == self.result_refs and groups == self.aggregate_groups:
            return self
        return self.create(
            query_identity=self.query_identity,
            active_graph_version=self.active_graph_version,
            query=self.query,
            matched_total=self.matched_total,
            returned_count=self.returned_count,
            count=self.count,
            retrieval_truncated=self.retrieval_truncated,
            continuity_truncated=True,
            result_refs=refs,
            aggregate_groups=groups,
            source_request_id=self.source_request_id,
            retrieved_at=self.retrieved_at,
            created_at=self.created_at,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "query_identity": self.query_identity,
            "result_fingerprint": self.result_fingerprint,
            "active_graph_version": self.active_graph_version,
            "query": self.query.model_dump(mode="json", exclude_none=True),
            "matched_total": self.matched_total,
            "returned_count": self.returned_count,
            "count": self.count,
            "retrieval_truncated": self.retrieval_truncated,
            "continuity_truncated": self.continuity_truncated,
            "result_refs": [item.to_payload() for item in self.result_refs],
            "aggregate_groups": [item.to_payload() for item in self.aggregate_groups],
            "source_request_id": self.source_request_id,
            "retrieved_at": self.retrieved_at,
            "created_at": self.created_at,
        }

    def routing_summary(self) -> dict[str, Any]:
        """Return only the bounded semantics exposed to the semantic Router."""
        return {
            "available": True,
            "mode": self.mode,
            "query": self.query.model_dump(mode="json", exclude_none=True),
            "matched_total": self.matched_total,
            "returned_count": self.returned_count,
            "count": self.count,
            "retrieval_truncated": self.retrieval_truncated,
            "continuity_truncated": self.continuity_truncated,
            "bounded_ref_count": len(self.result_refs),
            "ordered_refs": [item.to_payload() for item in self.result_refs],
            "bounded_groups": [item.to_payload() for item in self.aggregate_groups],
            "active_graph_version": self.active_graph_version,
            "retrieved_at": self.retrieved_at,
        }

    def historical_context_payload(self) -> dict[str, Any]:
        """Bounded model context explicitly labelled as historical continuity."""
        return {
            "authority": (
                "Historical structured-query continuity only; not current operational "
                "evidence. Omitted refs cannot be reconstructed."
            ),
            "query_identity": self.query_identity,
            "result_fingerprint": self.result_fingerprint,
            "source_request_id": self.source_request_id,
            **self.routing_summary(),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "StructuredQueryContext":
        allowed = {
            "schema_version", "mode", "query_identity", "result_fingerprint",
            "active_graph_version", "query", "matched_total", "returned_count",
            "count", "retrieval_truncated", "continuity_truncated", "result_refs",
            "aggregate_groups", "source_request_id", "retrieved_at", "created_at",
        }
        if not isinstance(payload, dict) or set(payload) != allowed:
            raise ValueError("invalid structured query context payload")
        refs = payload.get("result_refs")
        groups = payload.get("aggregate_groups")
        if not isinstance(refs, list) or not isinstance(groups, list):
            raise ValueError("invalid structured query context references")
        return cls(
            schema_version=payload.get("schema_version"),
            mode=payload.get("mode"),
            query_identity=payload.get("query_identity"),
            result_fingerprint=payload.get("result_fingerprint"),
            active_graph_version=payload.get("active_graph_version"),
            query=StructuredQuerySpec.model_validate(payload.get("query")),
            matched_total=payload.get("matched_total"),
            returned_count=payload.get("returned_count"),
            count=payload.get("count"),
            retrieval_truncated=payload.get("retrieval_truncated"),
            continuity_truncated=payload.get("continuity_truncated"),
            result_refs=tuple(StructuredAssetRef.from_payload(item) for item in refs),
            aggregate_groups=tuple(StructuredAggregateGroupRef.from_payload(item) for item in groups),
            source_request_id=payload.get("source_request_id"),
            retrieved_at=payload.get("retrieved_at"),
            created_at=payload.get("created_at"),
        )
