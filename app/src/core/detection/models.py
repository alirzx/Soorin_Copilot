"""Raw and normalized asset-detection schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _RawBase(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class RawDetectionSection(_RawBase):
    primary_role: str | None = Field(default=None, alias="primaryRole")
    confidence: float | None = None
    top_roles: list[Any] | None = Field(default=None, alias="topRoles")
    vendor: str | None = None
    product: str | None = None
    inferred_device_type: str | None = Field(default=None, alias="inferredDeviceType")


class RawTaggingSection(_RawBase):
    tag: str | None = None
    sub_tag: str | None = Field(default=None, alias="subTag")
    confidence: float | None = None
    inferred_device_type: str | None = Field(default=None, alias="inferredDeviceType")
    vendor: str | None = None
    product: str | None = None


class RawDetectionRule(_RawBase):
    id: str | None = None
    code: str | None = None
    name: str | None = None
    confidence: float | None = None
    evidence: list[Any] | None = None

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence_list(cls, value: Any) -> list[Any] | None:
        if value is None:
            return None
        if isinstance(value, list):
            return value
        return [value]


class RawSignalsSection(_RawBase):
    extended: dict[str, Any] | None = None
    normalized: dict[str, Any] | None = None


class RawAssetDetectionResponse(_RawBase):
    ip: str
    asset_found: bool | None = Field(default=None, alias="assetFound")
    stored_tag: str | None = Field(default=None, alias="storedTag")
    stored_sub_tag: str | None = Field(default=None, alias="storedSubTag")
    detection: RawDetectionSection | None = None
    tagging: RawTaggingSection | None = None
    matched_rules: list[RawDetectionRule] = Field(default_factory=list, alias="matchedRules")
    signals: RawSignalsSection | None = None

    @field_validator("matched_rules", mode="before")
    @classmethod
    def _rules_list(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]


@dataclass(frozen=True)
class DetectionClassification:
    asset_found: bool | None
    primary_role: str | None = None
    inferred_device_type: str | None = None
    confidence: float | None = None
    top_roles: list[str] = field(default_factory=list)
    vendor: str | None = None
    product: str | None = None


@dataclass(frozen=True)
class DetectionTagging:
    stored_tag: str | None = None
    stored_sub_tag: str | None = None
    tag: str | None = None
    sub_tag: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class DetectionRule:
    id: str | None = None
    code: str | None = None
    name: str | None = None
    confidence: float | None = None
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DetectionSignals:
    extended: dict[str, Any] = field(default_factory=dict)
    normalized: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    missing_sections: list[str] = field(default_factory=list)
    null_sections: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DetectionConflict:
    code: str
    severity: str
    primary_value: Any
    conflicting_value: Any
    explanation: str


@dataclass(frozen=True)
class AssetDetectionEvidence:
    ip: str
    found: bool | None
    classification: DetectionClassification
    tagging: DetectionTagging
    matched_rules: list[DetectionRule]
    signals: DetectionSignals
    conflicts: list[DetectionConflict]
    limitations: list[str]
    fetched_at: datetime
    source: str
