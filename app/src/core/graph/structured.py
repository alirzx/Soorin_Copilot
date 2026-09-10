"""Typed contracts for bounded structured Asset retrieval.

These models describe selectors and Asset sets.  They are deliberately not
agent entities and expose no Cypher/property escape hatch.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AssetSortField(str, Enum):
    GRAPH_KEY = "graph_key"
    IP = "ip"
    ASSET_NAME = "asset_name"
    MODEL_CONFIDENCE = "model_confidence"
    MAPPING_CONFIDENCE = "mapping_confidence"
    UNKNOWN_SCORE = "unknown_score"
    LAST_DETECTION_AT = "last_detection_at"
    ENRICHMENT_NEXT_DUE_AT = "enrichment_next_due_at"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class AssetGroupField(str, Enum):
    STATUS = "status"
    SUGGESTED_TYPE = "suggested_type"
    ROLE = "role"
    VENDOR = "vendor"
    PRODUCT = "product"
    TAG = "tag"
    SUB_TAG = "sub_tag"
    ENRICHMENT_STATUS = "enrichment_status"


class AssetAggregateOperation(str, Enum):
    COUNT = "count"
    GROUP_COUNT = "group_count"


class AssetSearchFilters(BaseModel):
    """Allow-listed AND selectors over authoritative Asset properties."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ip: str | None = Field(default=None, max_length=45)
    asset_name: str | None = Field(default=None, max_length=256)
    status: str | None = Field(default=None, max_length=128)
    suggested_type: str | None = Field(default=None, max_length=256)
    role: str | None = Field(default=None, max_length=256)
    roles: str | None = Field(default=None, max_length=256)
    vendor: str | None = Field(default=None, max_length=256)
    product: str | None = Field(default=None, max_length=256)
    tag: str | None = Field(default=None, max_length=256)
    sub_tag: str | None = Field(default=None, max_length=256)
    enrichment_status: str | None = Field(default=None, max_length=64)
    model_confidence_min: float | None = Field(default=None, ge=0.0, le=1.0)
    model_confidence_max: float | None = Field(default=None, ge=0.0, le=1.0)
    mapping_confidence_min: float | None = Field(default=None, ge=0.0, le=1.0)
    mapping_confidence_max: float | None = Field(default=None, ge=0.0, le=1.0)
    unknown_score_min: float | None = Field(default=None, ge=0.0, le=1.0)
    unknown_score_max: float | None = Field(default=None, ge=0.0, le=1.0)
    last_detection_at_from: datetime | None = None
    last_detection_at_to: datetime | None = None

    @field_validator(
        "asset_name",
        "status",
        "suggested_type",
        "role",
        "roles",
        "vendor",
        "product",
        "tag",
        "sub_tag",
        "enrichment_status",
    )
    @classmethod
    def normalize_exact_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("exact string filters cannot be blank")
        return normalized

    @field_validator("ip")
    @classmethod
    def normalize_ip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as exc:
            raise ValueError("ip must be a canonical IPv4 or IPv6 address") from exc

    @field_validator("last_detection_at_from", "last_detection_at_to")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp filters must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_ranges(self) -> "AssetSearchFilters":
        for lower_name, upper_name in (
            ("model_confidence_min", "model_confidence_max"),
            ("mapping_confidence_min", "mapping_confidence_max"),
            ("unknown_score_min", "unknown_score_max"),
            ("last_detection_at_from", "last_detection_at_to"),
        ):
            lower = getattr(self, lower_name)
            upper = getattr(self, upper_name)
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"{lower_name} cannot exceed {upper_name}")
        return self

    def query_values(self) -> dict[str, Any]:
        values = self.model_dump(exclude_none=True)
        for key in ("last_detection_at_from", "last_detection_at_to"):
            if key in values:
                values[key] = values[key].isoformat()
        return values


class AssetSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    filters: AssetSearchFilters = Field(default_factory=AssetSearchFilters)
    sort: AssetSortField = AssetSortField.GRAPH_KEY
    direction: SortDirection = SortDirection.ASC
    limit: int | None = Field(default=None, ge=1)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)


class StructuredAssetRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_key: str
    graph_version: str
    ip: str
    asset_name: str | None = None
    status: str | None = None
    suggested_type: str | None = None
    model_confidence: float | None = None
    mapping_confidence: float | None = None
    unknown_score: float | None = None
    classification_summary: str | None = None
    vendor: str | None = None
    product: str | None = None
    role: str | None = None
    roles: tuple[str, ...] = ()
    tag: str | None = None
    sub_tag: str | None = None
    last_detection_at: str | None = None
    enrichment_status: str | None = None
    enrichment_updated_at: str | None = None
    enrichment_last_attempt_at: str | None = None
    enrichment_last_success_at: str | None = None
    enrichment_next_due_at: str | None = None
    enrichment_source: str | None = None
    enrichment_version: str | None = None

    @field_validator("roles", mode="before")
    @classmethod
    def normalize_roles(cls, value: Any) -> tuple[str, ...]:
        return tuple(value or ())


class AssetSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_graph_version: str | None
    filters: AssetSearchFilters
    rows: tuple[StructuredAssetRow, ...]
    returned_count: int
    matched_total: int
    truncated: bool
    next_cursor: str | None = None
    sort: AssetSortField
    direction: SortDirection
    retrieved_at: str
    limitations: tuple[str, ...] = ()


class AssetAggregateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    filters: AssetSearchFilters = Field(default_factory=AssetSearchFilters)
    operation: AssetAggregateOperation = AssetAggregateOperation.COUNT
    group_by: AssetGroupField | None = None
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_grouping(self) -> "AssetAggregateRequest":
        if self.operation is AssetAggregateOperation.GROUP_COUNT and self.group_by is None:
            raise ValueError("group_count requires group_by")
        if self.operation is AssetAggregateOperation.COUNT and self.group_by is not None:
            raise ValueError("count does not accept group_by")
        return self


class AssetAggregateGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str | None
    count: int


class AssetAggregateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_graph_version: str | None
    filters: AssetSearchFilters
    operation: AssetAggregateOperation
    count: int
    group_by: AssetGroupField | None = None
    groups: tuple[AssetAggregateGroup, ...] = ()
    truncated: bool = False
    retrieved_at: str
    limitations: tuple[str, ...] = ()
