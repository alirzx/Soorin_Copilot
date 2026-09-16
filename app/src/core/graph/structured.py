"""Typed contracts for bounded structured Asset retrieval.

These models describe selectors and Asset sets. They are deliberately not
agent entities and expose no Cypher/property escape hatch.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_AGGREGATE_GROUP_MEMBER_IPS = 20
MAX_STRUCTURED_PREDICATE_DEPTH = 3
MAX_STRUCTURED_PREDICATE_LEAVES = 24
MAX_STRUCTURED_PREDICATE_VALUES = 20
MAX_AGGREGATE_GROUP_FIELDS = 3


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
    """Allow-listed grouping dimensions over the enriched Asset projection.

    The 15 Product-derived Exact Search properties are all groupable. The
    Graph-owned enrichment status remains available as one additional metadata
    dimension. Because this is an enum, group values can never become arbitrary
    Cypher/property syntax.
    """

    IP = "ip"
    ASSET_NAME = "asset_name"
    STATUS = "status"
    SUGGESTED_TYPE = "suggested_type"
    MODEL_CONFIDENCE = "model_confidence"
    MAPPING_CONFIDENCE = "mapping_confidence"
    UNKNOWN_SCORE = "unknown_score"
    CLASSIFICATION_SUMMARY = "classification_summary"
    VENDOR = "vendor"
    PRODUCT = "product"
    ROLE = "role"
    ROLES = "roles"
    TAG = "tag"
    SUB_TAG = "sub_tag"
    LAST_DETECTION_AT = "last_detection_at"
    ENRICHMENT_STATUS = "enrichment_status"


class AssetAggregateOperation(str, Enum):
    COUNT = "count"
    GROUP_COUNT = "group_count"


class StructuredQueryMode(str, Enum):
    SEARCH = "search"
    AGGREGATE = "aggregate"


class AssetOutputField(str, Enum):
    """Bounded presentation fields; these never participate in selection."""

    IP = "ip"
    ASSET_NAME = "asset_name"
    STATUS = "status"
    SUGGESTED_TYPE = "suggested_type"
    MODEL_CONFIDENCE = "model_confidence"
    MAPPING_CONFIDENCE = "mapping_confidence"
    UNKNOWN_SCORE = "unknown_score"
    CLASSIFICATION_SUMMARY = "classification_summary"
    VENDOR = "vendor"
    PRODUCT = "product"
    ROLE = "role"
    ROLES = "roles"
    TAG = "tag"
    SUB_TAG = "sub_tag"
    LAST_DETECTION_AT = "last_detection_at"
    ENRICHMENT_STATUS = "enrichment_status"
    COUNT = "count"
    PERCENTAGE = "percentage"
    MEMBER_IPS = "member_ips"


class AssetPredicateField(str, Enum):
    IP = "ip"
    ASSET_NAME = "asset_name"
    STATUS = "status"
    SUGGESTED_TYPE = "suggested_type"
    CLASSIFICATION_SUMMARY = "classification_summary"
    VENDOR = "vendor"
    PRODUCT = "product"
    ROLE = "role"
    ROLES = "roles"
    TAG = "tag"
    SUB_TAG = "sub_tag"
    MODEL_CONFIDENCE = "model_confidence"
    MAPPING_CONFIDENCE = "mapping_confidence"
    UNKNOWN_SCORE = "unknown_score"
    LAST_DETECTION_AT = "last_detection_at"
    ENRICHMENT_STATUS = "enrichment_status"


class AssetPredicateOperator(str, Enum):
    EQ = "eq"
    IN = "in"
    MEMBER_EQ = "member_eq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    BETWEEN = "between"


_PREDICATE_TEXT_FIELDS = frozenset({
    AssetPredicateField.ASSET_NAME,
    AssetPredicateField.STATUS,
    AssetPredicateField.SUGGESTED_TYPE,
    AssetPredicateField.CLASSIFICATION_SUMMARY,
    AssetPredicateField.VENDOR,
    AssetPredicateField.PRODUCT,
    AssetPredicateField.ROLE,
    AssetPredicateField.TAG,
    AssetPredicateField.SUB_TAG,
    AssetPredicateField.ENRICHMENT_STATUS,
})
_PREDICATE_SCORE_FIELDS = frozenset({
    AssetPredicateField.MODEL_CONFIDENCE,
    AssetPredicateField.MAPPING_CONFIDENCE,
    AssetPredicateField.UNKNOWN_SCORE,
})
_PREDICATE_RANGE_OPERATORS = frozenset({
    AssetPredicateOperator.GT,
    AssetPredicateOperator.GTE,
    AssetPredicateOperator.LT,
    AssetPredicateOperator.LTE,
    AssetPredicateOperator.BETWEEN,
})


class AssetPredicate(BaseModel):
    """One bounded Boolean node or one allow-listed Asset-property predicate."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    field: AssetPredicateField | None = None
    operator: AssetPredicateOperator | None = None
    value: Any = None
    values: tuple[Any, ...] = ()
    all: tuple["AssetPredicate", ...] = ()
    any: tuple["AssetPredicate", ...] = ()
    not_: "AssetPredicate | None" = Field(default=None, alias="not")

    @model_validator(mode="after")
    def validate_node(self) -> "AssetPredicate":
        boolean_kinds = sum(bool(item) for item in (self.all, self.any)) + int(
            self.not_ is not None
        )
        leaf = self.field is not None or self.operator is not None
        if boolean_kinds + int(leaf) != 1:
            raise ValueError("predicate must be exactly one leaf, all, any, or not node")
        if self.all or self.any:
            children = self.all or self.any
            if not 2 <= len(children) <= MAX_STRUCTURED_PREDICATE_LEAVES:
                raise ValueError("Boolean predicate nodes require 2..24 children")
            if self.value is not None or self.values:
                raise ValueError("Boolean predicate nodes do not accept values")
            return self
        if self.not_ is not None:
            if self.value is not None or self.values:
                raise ValueError("not predicate nodes do not accept values")
            if self.not_.all or self.not_.any or self.not_.not_ is not None:
                raise ValueError("not is limited to one leaf predicate")
            return self
        if self.field is None or self.operator is None:
            raise ValueError("predicate leaves require field and operator")
        normalized_value, normalized_values = self._normalize_leaf_values()
        object.__setattr__(self, "value", normalized_value)
        object.__setattr__(self, "values", normalized_values)
        return self

    def _normalize_leaf_values(self) -> tuple[Any, tuple[Any, ...]]:
        assert self.field is not None and self.operator is not None
        if self.operator in {AssetPredicateOperator.IN, AssetPredicateOperator.BETWEEN}:
            if self.value is not None:
                raise ValueError("in/between predicates use values")
            required = 2 if self.operator is AssetPredicateOperator.BETWEEN else None
            if required is not None and len(self.values) != required:
                raise ValueError("between requires exactly two values")
            if self.operator is AssetPredicateOperator.IN and not (
                1 <= len(self.values) <= MAX_STRUCTURED_PREDICATE_VALUES
            ):
                raise ValueError("in requires 1..20 values")
            values = tuple(self._normalize_scalar(item) for item in self.values)
            if self.operator is AssetPredicateOperator.BETWEEN and values[0] > values[1]:
                raise ValueError("between lower bound cannot exceed upper bound")
            return None, tuple(dict.fromkeys(values))
        if self.values:
            raise ValueError("this predicate operator does not accept values")
        if self.value is None:
            raise ValueError("predicate value is required")
        return self._normalize_scalar(self.value), ()

    def _normalize_scalar(self, value: Any) -> Any:
        assert self.field is not None and self.operator is not None
        if self.field is AssetPredicateField.ROLES:
            if self.operator not in {AssetPredicateOperator.MEMBER_EQ, AssetPredicateOperator.IN}:
                raise ValueError("roles supports member_eq or in membership only")
            return self._normalized_text(value)
        if self.operator is AssetPredicateOperator.MEMBER_EQ:
            raise ValueError("member_eq is reserved for roles")
        if self.field is AssetPredicateField.IP:
            if self.operator not in {AssetPredicateOperator.EQ, AssetPredicateOperator.IN}:
                raise ValueError("ip supports eq or in only")
            try:
                return str(ipaddress.ip_address(str(value).strip()))
            except ValueError as exc:
                raise ValueError("predicate IP is invalid") from exc
        if self.field in _PREDICATE_TEXT_FIELDS:
            if self.operator not in {AssetPredicateOperator.EQ, AssetPredicateOperator.IN}:
                raise ValueError("text predicates support eq or in only")
            return self._normalized_text(value)
        if self.field in _PREDICATE_SCORE_FIELDS:
            if self.operator not in _PREDICATE_RANGE_OPERATORS | {AssetPredicateOperator.EQ}:
                raise ValueError("score predicate operator is invalid")
            if isinstance(value, bool):
                raise ValueError("score predicate must be numeric")
            try:
                score = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("score predicate must be numeric") from exc
            if not 0.0 <= score <= 1.0:
                raise ValueError("score predicate must be between zero and one")
            return score
        if self.field is AssetPredicateField.LAST_DETECTION_AT:
            if self.operator not in _PREDICATE_RANGE_OPERATORS | {AssetPredicateOperator.EQ}:
                raise ValueError("timestamp predicate operator is invalid")
            try:
                parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("timestamp predicate must be ISO-8601") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timestamp predicate must include a timezone")
            return parsed.astimezone(timezone.utc).isoformat()
        raise ValueError("predicate field is unsupported")

    @staticmethod
    def _normalized_text(value: Any) -> str:
        text = str(value).strip()
        if not text or len(text) > 1024:
            raise ValueError("predicate text is blank or oversized")
        return text

    def shape(self) -> tuple[int, int]:
        """Return (depth, leaf count) for the validated bounded tree."""
        children = self.all or self.any
        if children:
            shapes = tuple(child.shape() for child in children)
            return 1 + max(item[0] for item in shapes), sum(item[1] for item in shapes)
        if self.not_ is not None:
            depth, leaves = self.not_.shape()
            return depth + 1, leaves
        return 1, 1


class AssetSearchFilters(BaseModel):
    """Allow-listed AND selectors over authoritative Asset properties."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ip: str | None = Field(default=None, max_length=45)
    asset_name: str | None = Field(default=None, max_length=256)
    status: str | None = Field(default=None, max_length=128)
    suggested_type: str | None = Field(default=None, max_length=256)
    model_confidence_min: float | None = Field(default=None, ge=0.0, le=1.0)
    model_confidence_max: float | None = Field(default=None, ge=0.0, le=1.0)
    mapping_confidence_min: float | None = Field(default=None, ge=0.0, le=1.0)
    mapping_confidence_max: float | None = Field(default=None, ge=0.0, le=1.0)
    unknown_score_min: float | None = Field(default=None, ge=0.0, le=1.0)
    unknown_score_max: float | None = Field(default=None, ge=0.0, le=1.0)
    classification_summary: str | None = Field(default=None, max_length=1024)
    vendor: str | None = Field(default=None, max_length=256)
    product: str | None = Field(default=None, max_length=256)
    role: str | None = Field(default=None, max_length=256)
    roles: str | None = Field(default=None, max_length=256)
    tag: str | None = Field(default=None, max_length=256)
    sub_tag: str | None = Field(default=None, max_length=256)
    last_detection_at_from: datetime | None = None
    last_detection_at_to: datetime | None = None
    enrichment_status: str | None = Field(default=None, max_length=64)
    predicate: AssetPredicate | None = None

    @field_validator(
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
        if self.predicate is not None:
            depth, leaves = self.predicate.shape()
            if depth > MAX_STRUCTURED_PREDICATE_DEPTH:
                raise ValueError("structured predicate nesting is too deep")
            if leaves > MAX_STRUCTURED_PREDICATE_LEAVES:
                raise ValueError("structured predicate has too many leaves")
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


class AssetSearchCapabilityInput(BaseModel):
    """Planner-safe search arguments; cursor replay remains service-internal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filters: AssetSearchFilters = Field(default_factory=AssetSearchFilters)
    sort: AssetSortField = AssetSortField.GRAPH_KEY
    direction: SortDirection = SortDirection.ASC
    limit: int | None = Field(default=None, ge=1)
    semantic_query_id: str | None = Field(default=None, max_length=160)

    def to_request(self) -> AssetSearchRequest:
        return AssetSearchRequest(
            filters=self.filters,
            sort=self.sort,
            direction=self.direction,
            limit=self.limit,
        )


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
    group_by_fields: tuple[AssetGroupField, ...] = ()
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_grouping(self) -> "AssetAggregateRequest":
        fields = self.group_fields
        if self.operation is AssetAggregateOperation.GROUP_COUNT and not fields:
            raise ValueError("group_count requires group_by")
        if self.operation is AssetAggregateOperation.COUNT and fields:
            raise ValueError("count does not accept group_by")
        if len(fields) > MAX_AGGREGATE_GROUP_FIELDS:
            raise ValueError("group_count supports at most three dimensions")
        if len(set(fields)) != len(fields):
            raise ValueError("aggregate grouping dimensions must be unique")
        if self.group_by is not None and self.group_by_fields and self.group_by != self.group_by_fields[0]:
            raise ValueError("group_by must match the first group_by_fields dimension")
        return self

    @property
    def group_fields(self) -> tuple[AssetGroupField, ...]:
        return self.group_by_fields or ((self.group_by,) if self.group_by is not None else ())


class AssetAggregateCapabilityInput(BaseModel):
    """Planner-safe bounded aggregate arguments over the same selector schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filters: AssetSearchFilters = Field(default_factory=AssetSearchFilters)
    operation: AssetAggregateOperation = AssetAggregateOperation.COUNT
    group_by: AssetGroupField | None = None
    group_by_fields: tuple[AssetGroupField, ...] = ()
    limit: int | None = Field(default=None, ge=1)
    semantic_query_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_grouping(self) -> "AssetAggregateCapabilityInput":
        self.to_request()
        return self

    def to_request(self) -> AssetAggregateRequest:
        return AssetAggregateRequest(
            filters=self.filters,
            operation=self.operation,
            group_by=self.group_by,
            group_by_fields=self.group_by_fields,
            limit=self.limit,
        )


def _normalized_group_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))[:1024]
    return str(value)[:1024]


class AssetAggregateGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str | None = None
    group_values: dict[str, str | None] = Field(default_factory=dict)
    count: int = Field(ge=0)
    member_ips: tuple[str, ...] = ()
    member_ips_truncated: bool = False

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: Any) -> str | None:
        return _normalized_group_value(value)

    @field_validator("member_ips", mode="before")
    @classmethod
    def normalize_member_ips(cls, value: Any) -> tuple[str, ...]:
        raw = tuple(value or ())
        if len(raw) > MAX_AGGREGATE_GROUP_MEMBER_IPS:
            raise ValueError("aggregate group member IPs exceed the bounded cap")
        normalized: list[str] = []
        for item in raw:
            try:
                normalized.append(str(ipaddress.ip_address(str(item).strip())))
            except ValueError as exc:
                raise ValueError("aggregate group member IP is invalid") from exc
        return tuple(normalized)

    @field_validator("group_values", mode="before")
    @classmethod
    def normalize_group_values(cls, value: Any) -> dict[str, str | None]:
        if value is None:
            return {}
        if not isinstance(value, dict) or not 1 <= len(value) <= MAX_AGGREGATE_GROUP_FIELDS:
            raise ValueError("aggregate group_values must contain 1..3 dimensions")
        allowed = {item.value for item in AssetGroupField}
        normalized: dict[str, str | None] = {}
        for key, raw in value.items():
            if key not in allowed or key in normalized:
                raise ValueError("aggregate group_values contains an invalid dimension")
            normalized[key] = _normalized_group_value(raw)
        return normalized

    @model_validator(mode="after")
    def validate_legacy_value(self) -> "AssetAggregateGroup":
        if len(self.group_values) == 1:
            only = next(iter(self.group_values.values()))
            if self.value is not None and self.value != only:
                raise ValueError("aggregate legacy value disagrees with group_values")
            if self.value is None:
                object.__setattr__(self, "value", only)
        elif len(self.group_values) > 1 and self.value is not None:
            raise ValueError("multi-dimensional aggregate groups do not expose legacy value")
        return self


class AssetAggregateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_graph_version: str | None
    filters: AssetSearchFilters
    operation: AssetAggregateOperation
    count: int
    group_by: AssetGroupField | None = None
    group_by_fields: tuple[AssetGroupField, ...] = ()
    groups: tuple[AssetAggregateGroup, ...] = ()
    truncated: bool = False
    retrieved_at: str
    limitations: tuple[str, ...] = ()

    @property
    def group_fields(self) -> tuple[AssetGroupField, ...]:
        return self.group_by_fields or ((self.group_by,) if self.group_by is not None else ())


class StructuredQuerySpec(BaseModel):
    """Router-owned semantic contract for one bounded Asset-set query.

    It reuses the repository allow-lists but intentionally excludes cursors and
    arbitrary Cypher. Asset/IP conversational identity remains outside this
    model; these fields are selectors over an Asset set.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: StructuredQueryMode
    filters: AssetSearchFilters = Field(default_factory=AssetSearchFilters)
    sort: AssetSortField | None = None
    direction: SortDirection | None = None
    limit: int | None = Field(default=None, ge=1)
    operation: AssetAggregateOperation | None = None
    group_by: AssetGroupField | None = None
    group_by_fields: tuple[AssetGroupField, ...] = ()
    requested_output_fields: tuple[AssetOutputField, ...] = ()
    router_supplied_limit: int | None = Field(default=None, ge=1)
    user_explicit_limit: bool = False
    fresh_rerun: bool = False
    semantic_class: str | None = Field(default=None, max_length=256)
    class_mapping_mode: str | None = Field(default=None, max_length=64)
    class_selector_fields: tuple[AssetPredicateField, ...] = ()

    @model_validator(mode="after")
    def validate_mode_contract(self) -> "StructuredQuerySpec":
        if len(set(self.requested_output_fields)) != len(self.requested_output_fields):
            raise ValueError("requested output fields must be unique")
        if len(self.requested_output_fields) > len(AssetOutputField):
            raise ValueError("too many requested output fields")
        if len(set(self.class_selector_fields)) != len(self.class_selector_fields):
            raise ValueError("class selector fields must be unique")
        if self.mode is StructuredQueryMode.SEARCH:
            if self.operation is not None or self.group_by is not None or self.group_by_fields:
                raise ValueError("search structured queries do not accept aggregate fields")
            return self
        if self.sort is not None or self.direction is not None:
            raise ValueError("aggregate structured queries do not accept sort or direction")
        if self.operation is None:
            raise ValueError("aggregate structured queries require operation")
        fields = self.group_by_fields or ((self.group_by,) if self.group_by is not None else ())
        if self.operation is AssetAggregateOperation.GROUP_COUNT and not fields:
            raise ValueError("group_count requires group_by")
        if self.operation is AssetAggregateOperation.COUNT and fields:
            raise ValueError("count does not accept group_by")
        AssetAggregateRequest(
            filters=self.filters,
            operation=self.operation,
            group_by=self.group_by,
            group_by_fields=self.group_by_fields,
            limit=self.limit,
        )
        return self

    def to_search_request(self) -> AssetSearchRequest:
        if self.mode is not StructuredQueryMode.SEARCH:
            raise ValueError("structured query is not a search")
        return AssetSearchRequest(
            filters=self.filters,
            sort=self.sort or AssetSortField.GRAPH_KEY,
            direction=self.direction or SortDirection.ASC,
            limit=self.limit,
        )

    def to_aggregate_request(self) -> AssetAggregateRequest:
        if self.mode is not StructuredQueryMode.AGGREGATE or self.operation is None:
            raise ValueError("structured query is not an aggregate")
        return AssetAggregateRequest(
            filters=self.filters,
            operation=self.operation,
            group_by=self.group_by,
            group_by_fields=self.group_by_fields,
            limit=self.limit,
        )


def structured_query_identity(
    query: StructuredQuerySpec | dict[str, Any],
    *,
    active_graph_version: str | None,
) -> str:
    """Hash normalized semantic query fields and the active projection version."""
    payload = canonical_structured_query_payload(query)
    canonical = json.dumps(
        {"active_graph_version": active_graph_version, "query": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "structured-asset-set:v1:" + hashlib.sha256(canonical).hexdigest()


def canonical_structured_query_payload(
    query: StructuredQuerySpec | dict[str, Any],
) -> dict[str, Any]:
    """Return the one execution-independent representation used for identity.

    Re-validating provider dictionaries is intentional: it removes model-dump
    differences such as explicit null fields and ``not_`` versus the ``not``
    alias before hashing.
    """

    parsed = query if isinstance(query, StructuredQuerySpec) else StructuredQuerySpec.model_validate(query)
    filters = parsed.filters.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude_defaults=True,
    )
    payload: dict[str, Any] = {
        "mode": parsed.mode.value,
        "filters": filters,
    }
    if parsed.mode is StructuredQueryMode.SEARCH:
        payload.update(
            sort=(parsed.sort or AssetSortField.GRAPH_KEY).value,
            direction=(parsed.direction or SortDirection.ASC).value,
        )
    else:
        group_fields = parsed.group_by_fields or (
            (parsed.group_by,) if parsed.group_by is not None else ()
        )
        payload.update(
            operation=(parsed.operation or AssetAggregateOperation.COUNT).value,
            group_by=(group_fields[0].value if len(group_fields) == 1 else None),
            group_by_fields=[item.value for item in group_fields],
        )
    return payload


def semantic_query_identity(query: StructuredQuerySpec | dict[str, Any]) -> str:
    """Hash only analytical semantics, excluding projection and execution bounds."""

    canonical = json.dumps(
        canonical_structured_query_payload(query),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "structured-semantic-query:v1:" + hashlib.sha256(canonical).hexdigest()


AssetPredicate.model_rebuild()
