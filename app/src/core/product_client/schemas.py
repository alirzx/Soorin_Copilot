"""Validated response envelopes for Soorin product API endpoints."""

from __future__ import annotations

import ipaddress
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger(__name__)


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _first_value(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value:
            return str(value).strip()
    return ""


@dataclass(frozen=True)
class TopologyConnectionRecord:
    """Validated unique communication pair from the product topology endpoint."""

    src_ip: str
    dst_ip: str
    weight: int = 1

    @classmethod
    def from_product_record(cls, record: dict[str, Any]) -> "TopologyConnectionRecord | None":
        src_ip = _first_value(record, ("src_ip", "source_ip", "src", "source"))
        dst_ip = _first_value(record, ("dst_ip", "destination_ip", "dst", "destination"))
        if not src_ip or not dst_ip:
            return None

        raw_weight = record.get("weight") or record.get("count") or 1
        try:
            weight = max(1, int(raw_weight))
        except (TypeError, ValueError):
            weight = 1

        return cls(src_ip=src_ip, dst_ip=dst_ip, weight=weight)


@dataclass(frozen=True)
class ProductTopologyResponse:
    """Validated topology response plus fetch metadata."""

    raw_payload: Any
    records: list[TopologyConnectionRecord]
    endpoint_path: str
    status_code: int
    elapsed_seconds: float

    @property
    def raw_record_count(self) -> int:
        return len(_extract_records(self.raw_payload))

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        endpoint_path: str,
        status_code: int,
        elapsed_seconds: float,
    ) -> "ProductTopologyResponse":
        raw_records = _extract_records(payload)
        records = [
            validated
            for raw_record in raw_records
            if (validated := TopologyConnectionRecord.from_product_record(raw_record)) is not None
        ]
        logger.info(
            "event=product_topology_records_validated endpoint_path=%s raw_records=%s valid_records=%s",
            endpoint_path,
            len(raw_records),
            len(records),
        )
        return cls(
            raw_payload=payload,
            records=records,
            endpoint_path=endpoint_path,
            status_code=status_code,
            elapsed_seconds=elapsed_seconds,
        )


@dataclass(frozen=True)
class ProductAssetResponse:
    """Lossless product JSON plus transport metadata for one asset endpoint."""

    target_ip: str
    raw_payload: dict[str, Any] | list[Any] | None
    endpoint_path: str
    status_code: int
    elapsed_seconds: float
    found: bool | None


class ProductAssetOverviewContractError(ValueError):
    """The Product overview payload does not satisfy the enrichment contract."""


@dataclass(frozen=True)
class ProductAssetDetectionOverview:
    """Validated, typed Product detection-overview payload."""

    asset_name: str | None
    ip: str
    status: str | None
    suggested_type: str | None
    model_confidence: float | None
    mapping_confidence: float | None
    unknown_score: float | None
    classification_summary: str | None
    vendor: str | None
    product: str | None
    role: str | None
    roles: tuple[str, ...] | None
    tag: str | None
    sub_tag: str | None
    last_detection_at: datetime | None

    _FIELDS = frozenset(
        {
            "assetName",
            "ip",
            "status",
            "suggestedType",
            "modelConfidence",
            "mappingConfidence",
            "unknownScore",
            "classificationSummary",
            "vendor",
            "product",
            "role",
            "roles",
            "tag",
            "subTag",
            "lastDetectionAt",
        }
    )

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        expected_ip: str | None = None,
    ) -> "ProductAssetDetectionOverview":
        if not isinstance(payload, dict):
            raise ProductAssetOverviewContractError(
                "Product detection overview must be a JSON object."
            )
        unexpected = sorted(set(payload).difference(cls._FIELDS))
        if unexpected:
            raise ProductAssetOverviewContractError(
                f"Product detection overview contains unsupported fields: {', '.join(unexpected)}."
            )
        raw_ip = payload.get("ip")
        if not isinstance(raw_ip, str) or not raw_ip.strip():
            raise ProductAssetOverviewContractError(
                "Product detection overview requires a valid ip field."
            )
        try:
            normalized_ip = str(ipaddress.ip_address(raw_ip.strip()))
        except ValueError as exc:
            raise ProductAssetOverviewContractError(
                "Product detection overview contains an invalid ip field."
            ) from exc
        if expected_ip is not None:
            try:
                normalized_expected = str(ipaddress.ip_address(expected_ip.strip()))
            except ValueError as exc:
                raise ProductAssetOverviewContractError(
                    "Expected enrichment IP is invalid."
                ) from exc
            if normalized_ip != normalized_expected:
                raise ProductAssetOverviewContractError(
                    "Product detection overview IP does not match the requested Asset."
                )
        return cls(
            asset_name=_optional_string(payload, "assetName"),
            ip=normalized_ip,
            status=_optional_string(payload, "status"),
            suggested_type=_optional_string(payload, "suggestedType"),
            model_confidence=_optional_score(payload, "modelConfidence"),
            mapping_confidence=_optional_score(payload, "mappingConfidence"),
            unknown_score=_optional_score(payload, "unknownScore"),
            classification_summary=_optional_string(payload, "classificationSummary"),
            vendor=_optional_string(payload, "vendor"),
            product=_optional_string(payload, "product"),
            role=_optional_string(payload, "role"),
            roles=_optional_roles(payload),
            tag=_optional_string(payload, "tag"),
            sub_tag=_optional_string(payload, "subTag"),
            last_detection_at=_optional_timestamp(payload, "lastDetectionAt"),
        )

    def property_values(self) -> dict[str, Any]:
        """Return canonical graph properties without inventing null defaults."""
        values: dict[str, Any] = {
            "asset_name": self.asset_name,
            "ip": self.ip,
            "status": self.status,
            "suggested_type": self.suggested_type,
            "model_confidence": self.model_confidence,
            "mapping_confidence": self.mapping_confidence,
            "unknown_score": self.unknown_score,
            "classification_summary": self.classification_summary,
            "vendor": self.vendor,
            "product": self.product,
            "role": self.role,
            "roles": list(self.roles) if self.roles is not None else None,
            "tag": self.tag,
            "sub_tag": self.sub_tag,
            "last_detection_at": (
                self.last_detection_at.isoformat()
                if self.last_detection_at is not None
                else None
            ),
        }
        return {key: value for key, value in values.items() if value is not None}


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProductAssetOverviewContractError(f"{key} must be a string or null.")
    return value.strip() or None


def _optional_score(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductAssetOverviewContractError(f"{key} must be numeric or null.")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ProductAssetOverviewContractError(f"{key} must be between 0 and 1.")
    return normalized


def _optional_roles(payload: dict[str, Any]) -> tuple[str, ...] | None:
    value = payload.get("roles")
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ProductAssetOverviewContractError(
            "roles must be an array of strings or null."
        )
    return tuple(item.strip() for item in value if item.strip())


def _optional_timestamp(payload: dict[str, Any], key: str) -> datetime | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProductAssetOverviewContractError(
            f"{key} must be an ISO-8601 timestamp or null."
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductAssetOverviewContractError(
            f"{key} must be an ISO-8601 timestamp or null."
        ) from exc
    if parsed.tzinfo is None:
        raise ProductAssetOverviewContractError(f"{key} must include a timezone.")
    return parsed.astimezone(timezone.utc)
